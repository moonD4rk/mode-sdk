"""Reports, the runs they produce, and the filters attached to them.

Mode documents no create-report endpoint: ``POST /reports`` mints an empty, unnamed
report in the caller's personal Collection, so ``create`` is three requests and deletes
what it minted when a later step fails. A failed run says as little -- the warehouse's
error lives on the query run underneath it, which ``failure_detail`` fetches.

``ReportsResource.list`` hides two different listings. Per Collection it is one row per
report; per data source it is one row per *(report x query)*, which is de-duplicated
here rather than handed back as a Page that repeats itself.
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Any
from typing import NotRequired
from typing import TypedDict

from .._policy import embedded
from .._transport import Transport
from .._types import QueryValue
from ..errors import ModeAPIError
from ..errors import ModeError
from ..errors import RunTimeoutError
from ..models import FormField
from ..models import Report
from ..models import ReportFilter
from ..models import ReportRun
from ..pagination import Batch
from ..pagination import Page
from ._args import DataSourceRef
from ._args import FilterRef
from ._args import ReportRef
from ._args import RunRef
from ._args import SpaceRef
from ._args import data_source_token
from ._args import token_of
from ._args import write_body
from ._base import Resource
from .distribution import ExportsResource
from .distribution import Format
from .distribution import RunResults

if TYPE_CHECKING:  # queries.py imports this module for its refs; keep the cycle type-only.
    from .queries import QueriesResource
    from .queries import QueryRunsResource

#: Mode's ceiling on a report name; a longer one is refused, not trimmed.
NAME_LIMIT = 64

#: Adaptive polling: first wait, growth factor, ceiling, and the jitter band on each wait.
POLL_FIRST_INTERVAL = 2.0
POLL_GROWTH = 1.5
POLL_MAX_INTERVAL = 30.0
POLL_JITTER = 0.2

#: Consecutive pages of nothing-new the data-source walk tolerates. Mode caps a report at
#: 160 queries and pages at 30 rows, so one report spans at most five barren pages; past
#: this bound the endpoint is ignoring ``page`` and the walk must stop.
_MAX_BARREN_PAGES = 7


class QuerySpec(TypedDict):
    """One query for a report being created.

    ``data_source_id`` takes the JSON integer or the decimal string, so a value read off
    ``Query.data_source_id`` needs no cast.
    """

    raw_query: str
    data_source_id: int | str
    name: NotRequired[str]


def query_spec(raw_query: str, data_source_id: int | str, name: str = "Query 1") -> QuerySpec:
    """One entry for the ``queries`` list a new report is created with."""
    return {"name": name, "raw_query": raw_query, "data_source_id": data_source_id}


def poll_intervals(interval: float | None) -> Iterator[float]:
    """The wait between polls: a fixed cadence when asked for one, adaptive otherwise.

    Adaptive grows from ``POLL_FIRST_INTERVAL`` to ``POLL_MAX_INTERVAL``, each wait
    jittered so a fleet of pollers does not converge on one cadence.
    """
    if interval is not None:
        while True:
            yield interval
    delay = POLL_FIRST_INTERVAL
    while True:
        yield delay * random.uniform(1 - POLL_JITTER, 1 + POLL_JITTER)
        delay = min(delay * POLL_GROWTH, POLL_MAX_INTERVAL)


def _checked_spec(index: int, spec: QuerySpec) -> QuerySpec:
    """A ``QuerySpec`` refused here rather than three requests into ``create``.

    A hand-built dict missing a key would raise ``KeyError`` mid-``create``, past the
    point the rollback covers. Checking the batch up front keeps ``create`` atomic.
    """
    missing = [key for key in ("raw_query", "data_source_id") if spec.get(key) is None]
    if missing:
        raise ValueError(
            f"queries[{index}] is missing {' and '.join(missing)}; "
            f"build entries with query_spec(raw_query, data_source_id)"
        )
    return spec


def _fresh(row: dict[str, Any], seen: set[str]) -> bool:
    """Whether this row is a report not yielded yet. An untokened row is always kept."""
    token = row.get("token")
    if not isinstance(token, str) or not token:
        return True
    if token in seen:
        return False
    seen.add(token)
    return True


def _distinct_reports(
    transport: Transport,
    path: str,
    *,
    params: Mapping[str, QueryValue] | None = None,
    per_page: int | None = None,
) -> Iterator[Batch[Report]]:
    """Walk ``/data_sources/{token}/reports``, which repeats a report once per query.

    ``offset_pages`` is not used here: its duplicate-page terminator would truncate this
    walk, because two consecutive pages legitimately hold the same token once a report
    has 60 rows. This ends on an empty page instead, with ``_MAX_BARREN_PAGES`` as the
    circuit-breaker should Mode stop honouring ``page=``.
    """
    base: dict[str, QueryValue] = dict(params or {})
    if per_page:
        base["per_page"] = per_page
    seen: set[str] = set()
    barren = 0
    page = 1
    while True:
        payload = transport.payload("GET", path, params=base | {"page": page})
        rows = embedded(payload, "reports")
        if not rows:
            return
        fresh = [row for row in rows if _fresh(row, seen)]
        if fresh:
            barren = 0
            yield Batch([Report.from_payload(row) for row in fresh])
        else:
            barren += 1
            if barren >= _MAX_BARREN_PAGES:
                return
        page += 1


class ReportRunsResource(Resource):
    def __init__(
        self, transport: Transport, query_runs: QueryRunsResource, exports: ExportsResource
    ) -> None:
        super().__init__(transport)
        self.query_runs = query_runs
        self._exports = exports

    def list(
        self,
        report: ReportRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[ReportRun]:
        """Every run of a report, newest first. Select by state client-side.

        There is no server-side filtering: ``?filter=`` is a deterministic 500 and
        ``?state=`` is accepted and ignored, so neither is offered as a keyword.

        The one report collection that states totals, so ``page.total_count`` and
        ``page.has_more`` are real here.
        """
        return self._many(
            ReportRun,
            f"/reports/{token_of(report, 'report')}/runs",
            "report_runs",
            extra_params=extra_params,
            per_page=per_page,
        )

    def get(self, report: ReportRef, run: RunRef) -> ReportRun:
        return self._one(
            ReportRun,
            "GET",
            f"/reports/{token_of(report, 'report')}/runs/{token_of(run, 'run')}",
        )

    def create(
        self, report: ReportRef, *, parameters: Mapping[str, object] | None = None
    ) -> ReportRun:
        """Start a run. The body is flat -- ``{"parameters": {...}}``, with no wrapper.

        Answers immediately with a non-terminal run, so the result is a receipt rather
        than a result; ``create_and_wait`` polls it out. Not retried by default: a
        replayed POST starts a second run and bills the warehouse twice.
        """
        return self._one(
            ReportRun,
            "POST",
            f"/reports/{token_of(report, 'report')}/runs",
            json={"parameters": dict(parameters or {})},
        )

    def clone(self, report: ReportRef, run: RunRef) -> Report:
        """Copy a run into a new *Report* named "Copy of ...", not into another run.

        The copy lands in the caller's personal Collection; follow with
        ``reports.update(token, space_token=...)`` to file it elsewhere.

        **A read timeout here does not mean the clone failed** -- it can complete
        server-side minutes later. Re-check the personal Collection before deciding, and
        never retry blindly; a replayed clone makes a second copy. Give it room with
        ``mode.with_options(timeout=...)``.
        """
        return self._one(
            Report,
            "POST",
            f"/reports/{token_of(report, 'report')}/runs/{token_of(run, 'run')}/clone",
        )

    def duplicate(self, report: ReportRef, run: RunRef) -> ReportRun:
        return self._one(
            ReportRun,
            "POST",
            f"/reports/{token_of(report, 'report')}/runs/{token_of(run, 'run')}/duplicates",
        )

    def duplication_status(self, report: ReportRef, run: RunRef, duplicate: str) -> ReportRun:
        path = (
            f"/reports/{token_of(report, 'report')}/runs/{token_of(run, 'run')}"
            f"/duplicates/{duplicate}"
        )
        return self._one(ReportRun, "GET", path)

    def form_fields(self, report: ReportRef, run: RunRef) -> Page[FormField]:
        """The run's parameter form, four keys per field. Not paginated by Mode.

        ``ReportRun.raw["form_fields"]`` is the same form unprojected, and carries more.
        """
        return self._many(
            FormField,
            f"/reports/{token_of(report, 'report')}/runs/{token_of(run, 'run')}/options",
            "form_fields",
        )

    def results(self, report: ReportRef, run: RunRef, *, format: Format = "csv") -> RunResults:
        """This run's results, in whichever shape Mode chose to send them.

        The shape does not follow from the query count. ``RunResults`` carries the
        Content-Type that explains the bytes; ``results_tables`` skips the question.
        """
        return self._exports.report_run(
            token_of(report, "report"), token_of(run, "run"), format=format
        )

    def results_tables(self, report: ReportRef, run: RunRef) -> dict[str, str]:
        """Every query's CSV from this run, keyed by file name, zip or not."""
        return self._exports.report_run_tables(token_of(report, "report"), token_of(run, "run"))

    def wait(
        self,
        report: ReportRef,
        run: RunRef,
        *,
        timeout: float = 900.0,
        interval: float | None = None,
    ) -> ReportRun:
        """Poll until the run reaches a terminal state.

        ``interval=None`` is adaptive; pass a number to pin the cadence. ``completed``
        counts as terminal alongside ``succeeded``, so a notebook report terminates. On
        expiry ``RunTimeoutError`` carries the last snapshot in ``.run``, and the run
        itself keeps going in Mode.
        """
        report_token, run_token = token_of(report, "report"), token_of(run, "run")
        return self._wait(
            report_token,
            run_token,
            self.get(report_token, run_token),
            timeout=timeout,
            interval=interval,
        )

    def create_and_wait(
        self,
        report: ReportRef,
        *,
        parameters: Mapping[str, object] | None = None,
        timeout: float = 900.0,
        interval: float | None = None,
    ) -> ReportRun:
        """Start a run and poll it to a terminal state."""
        report_token = token_of(report, "report")
        run = self.create(report_token, parameters=parameters)
        return self._wait(
            report_token,
            token_of(run, "run"),
            run,
            timeout=timeout,
            interval=interval,
        )

    def _wait(
        self,
        report: str,
        run: str,
        current: ReportRun,
        *,
        timeout: float,
        interval: float | None,
    ) -> ReportRun:
        deadline = time.monotonic() + timeout
        intervals = poll_intervals(interval)
        while not current.is_terminal:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RunTimeoutError(
                    f"report run {run} was still {current.state} after {timeout:.0f}s",
                    run=current,
                )
            time.sleep(min(next(intervals), remaining))
            current = self.get(report, run)
        return current

    def failure_detail(self, report: ReportRef, run: RunRef) -> str:
        """Why a run failed. The message is on the query run, not the report run."""
        try:
            query_runs = list(self.query_runs.list(report, run))
        except ModeAPIError as exc:
            return f"(could not read query runs: {exc})"
        parts = [qr.error_message or qr.error_type or qr.state or "" for qr in query_runs]
        return " | ".join(part for part in parts if part) or "(no error message returned)"


class ReportsResource(Resource):
    def __init__(
        self, transport: Transport, runs: ReportRunsResource, queries: QueriesResource
    ) -> None:
        super().__init__(transport)
        self.runs = runs
        self._queries = queries

    def get(self, report: ReportRef) -> Report:
        return self._one(Report, "GET", f"/reports/{token_of(report, 'report')}")

    def list(
        self,
        *,
        space: SpaceRef | None = None,
        data_source: DataSourceRef | None = None,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[Report]:
        """Reports in one Collection, or reports on one data source. Never both, never all.

        Mode has no workspace-wide report list, and neither listing filters, sorts or
        searches -- archived reports are always included -- so there are no filter
        keywords here to promise something the API does not do.

        ``data_source`` routes by **token**, never by the numeric ``DataSource.id``, which
        404s. That listing yields one row per *(report x query)* and is de-duplicated
        here, so page boundaries do not line up with Mode's.
        """
        if space is not None and data_source is not None:
            raise ValueError("pass space= or data_source=, not both")
        if space is not None:
            return self._many(
                Report,
                f"/spaces/{token_of(space, 'space')}/reports",
                "reports",
                extra_params=extra_params,
                per_page=per_page,
            )
        if data_source is None:
            raise ValueError("pass space= or data_source=; Mode cannot list all reports at once")
        path = f"/data_sources/{data_source_token(data_source)}/reports"
        params = dict(extra_params or {})
        return Page(
            lambda: _distinct_reports(self._t, path, params=params, per_page=per_page), path
        )

    def create(
        self,
        space: SpaceRef,
        name: str,
        queries: Sequence[QuerySpec],
        *,
        description: str | None = None,
    ) -> Report:
        """Create a report in ``space``. Three requests, because Mode has no create call.

        ``POST /reports`` mints an unnamed report in the caller's personal Collection and
        ignores everything sent with it; the PATCH names and files it, and each query is
        its own POST.

        Every argument is validated before anything is minted, so a bad name or spec
        fails without leaving a report behind. A failure past the first step deletes the
        new token; the rollback catches ``Exception`` rather than ``ModeError`` because
        the mint is equally minted whatever raised.
        """
        if len(name) > NAME_LIMIT:
            raise ValueError(f"report name is {len(name)} characters; Mode allows {NAME_LIMIT}")
        space_token = token_of(space, "space")
        specs = [_checked_spec(index, spec) for index, spec in enumerate(queries)]
        token = self._one(Report, "POST", "/reports").token
        if not token:
            raise ModeAPIError(201, "Mode created a report without returning its token")
        try:
            self.update(token, name=name, description=description, space_token=space_token)
            for spec in specs:
                self._queries.create(
                    token,
                    spec["raw_query"],
                    spec["data_source_id"],
                    name=spec.get("name"),
                )
            return self.get(token)
        except Exception:
            with suppress(ModeError):
                self.delete(token)
            raise

    def update(
        self,
        report: ReportRef,
        *,
        name: str | None = None,
        description: str | None = None,
        space_token: SpaceRef | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> Report:
        """Rename, re-describe or re-file a report. Answered with ``202``, not ``200``.

        A ``None`` is dropped rather than sent, so this cannot clear a description; pass
        ``extra_body={"description": None}`` for the explicit null. A name over
        ``NAME_LIMIT`` is a 400 from Mode rather than a local error.
        """
        payload = write_body(
            "report",
            extra_body,
            name=name,
            description=description,
            space_token=None if space_token is None else token_of(space_token, "space_token"),
        )
        return self._one(Report, "PATCH", f"/reports/{token_of(report, 'report')}", json=payload)

    def delete(self, report: ReportRef) -> None:
        self._t.request("DELETE", f"/reports/{token_of(report, 'report')}")

    def archive(self, report: ReportRef) -> Report:
        return self._one(Report, "PATCH", f"/reports/{token_of(report, 'report')}/archive")

    def unarchive(self, report: ReportRef) -> Report:
        return self._one(Report, "PATCH", f"/reports/{token_of(report, 'report')}/unarchive")

    def purge(
        self, time: str, *, end_time: str | None = None, in_progress: bool | None = None
    ) -> dict[str, Any]:
        """Purge stored results workspace-wide. ``time`` is YYYY-MM-DD, at least a day past.

        Flat body, no wrapper. Mode documents no stable response shape, so it arrives raw.
        """
        payload: dict[str, object] = {"time": time}
        if end_time is not None:
            payload["end_time"] = end_time
        if in_progress is not None:
            payload["in_progress"] = in_progress
        return self._t.payload("POST", "/reports/purge", json=payload)

    def run(
        self, report: ReportRef, *, parameters: Mapping[str, object] | None = None
    ) -> ReportRun:
        return self.runs.create(report, parameters=parameters)

    def run_and_wait(
        self,
        report: ReportRef,
        *,
        parameters: Mapping[str, object] | None = None,
        timeout: float = 900.0,
        interval: float | None = None,
    ) -> ReportRun:
        return self.runs.create_and_wait(
            report, parameters=parameters, timeout=timeout, interval=interval
        )


class ReportFiltersResource(Resource):
    """Report filters, whose keywords come from Mode's own ``_forms`` block.

    ``filter_type``, ``control_type`` and ``variable_type`` are validated server-side
    against closed lists, but are typed ``str`` rather than ``Literal`` because Mode
    publishes only part of each. ``data_type`` is not validated at all.
    """

    def list(
        self,
        report: ReportRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[ReportFilter]:
        return self._many(
            ReportFilter,
            f"/reports/{token_of(report, 'report')}/filters",
            "report_filters",
            extra_params=extra_params,
            per_page=per_page,
        )

    def get(self, report: ReportRef, filter: FilterRef) -> ReportFilter:
        return self._one(
            ReportFilter,
            "GET",
            f"/reports/{token_of(report, 'report')}/filters/{token_of(filter, 'filter')}",
        )

    def create(
        self,
        report: ReportRef,
        *,
        name: str,
        formula: str,
        data_type: str,
        filter_type: str,
        control_type: str,
        variable_type: str,
        formula_type: str | None = None,
        options: Mapping[str, object] | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> ReportFilter:
        """Add a filter. The six required keywords are the six Mode refuses to default,
        so omitting one is a TypeError here rather than a 400 round trip.

        ``formula`` is unique per report: a second filter on the same one answers
        ``400 Formula has already been taken``.
        """
        payload = write_body(
            "report_filter",
            extra_body,
            name=name,
            formula=formula,
            data_type=data_type,
            filter_type=filter_type,
            control_type=control_type,
            variable_type=variable_type,
            formula_type=formula_type,
            options=dict(options) if options is not None else None,
        )
        return self._one(
            ReportFilter, "POST", f"/reports/{token_of(report, 'report')}/filters", json=payload
        )

    def update(
        self,
        report: ReportRef,
        filter: FilterRef,
        *,
        name: str | None = None,
        formula: str | None = None,
        data_type: str | None = None,
        filter_type: str | None = None,
        control_type: str | None = None,
        variable_type: str | None = None,
        formula_type: str | None = None,
        options: Mapping[str, object] | None = None,
        hide_select_all: bool | None = None,
        show_relevant_values_only: bool | None = None,
        next_filter_token: str | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> ReportFilter:
        """Change one filter. Every ``None`` is dropped, so this only ever sets fields.

        ``next_filter_token`` belongs to Mode's separate ``_forms.edit_position`` form on
        the same PATCH action: it reorders rather than edits, and its companion ``all``
        flag sits outside the wrapper, where ``extra_body`` cannot reach it.
        """
        payload = write_body(
            "report_filter",
            extra_body,
            name=name,
            formula=formula,
            data_type=data_type,
            filter_type=filter_type,
            control_type=control_type,
            variable_type=variable_type,
            formula_type=formula_type,
            options=dict(options) if options is not None else None,
            hide_select_all=hide_select_all,
            show_relevant_values_only=show_relevant_values_only,
            next_filter_token=next_filter_token,
        )
        return self._one(
            ReportFilter,
            "PATCH",
            f"/reports/{token_of(report, 'report')}/filters/{token_of(filter, 'filter')}",
            json=payload,
        )

    def delete(self, report: ReportRef, filter: FilterRef) -> None:
        self._t.request(
            "DELETE",
            f"/reports/{token_of(report, 'report')}/filters/{token_of(filter, 'filter')}",
        )
