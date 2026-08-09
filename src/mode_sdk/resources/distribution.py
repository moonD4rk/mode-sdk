"""Getting results and reports out of Mode: exports, schedules, subscriptions.

The shape of an export is Mode's decision, not a property of the report, so never
predict it: ``RunResults`` carries the bytes with the Content-Type that explains them,
and ``tables()`` answers alike for one CSV or a zip of them.

Two export paths are traps, both handled here: a report-level ``content.json`` silently
returns only the first query's rows, and a PDF is a two-step asynchronous render.
"""

from __future__ import annotations

import io
import logging
import os
import pathlib
import re
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from typing import TypedDict

import httpx

from .._types import QueryValue
from ..errors import RunTimeoutError
from ..models import DatasetSchedule
from ..models import PdfExport
from ..models import ReportSchedule
from ..models import ReportSubscription
from ..pagination import Page
from ._args import DatasetRef
from ._args import QueryRunRef
from ._args import ReportRef
from ._args import RunRef
from ._args import ScheduleRef
from ._args import SubscriptionRef
from ._args import token_of
from ._args import write_body
from ._base import Resource

_log = logging.getLogger("mode_sdk.exports")

Format = Literal["csv", "json"]
"""Which extension an export path is asked for, not a promise about what comes back:
``content.csv`` on a multi-query report answers ``application/zip``. Branch on
``RunResults.media_type``.
"""

_ARCHIVE_MEDIA_TYPE = "application/zip"

_TEXT_MEDIA_TYPES = frozenset({"application/json", "application/xml", "application/javascript"})

_FILENAME = re.compile(r'filename="?([^";]+)"?')

#: A HAL href that already ends in ``/content.csv`` (or ``.json``) must not get a second one.
_CONTENT_SUFFIX = re.compile(r"/content\.[A-Za-z0-9]+$")


def _decode(payload: bytes) -> str:
    # utf-8-sig: a BOM left on the first column name breaks every header match.
    return payload.decode("utf-8-sig")


@dataclass(frozen=True, slots=True)
class RunResults:
    """An export as Mode answered it: the bytes, what they are, and what to call them.

    ``content`` is the *decoded* body: Mode serves exports gzip-encoded even for
    ``text/csv``, and httpx has already undone that.
    """

    content: bytes
    media_type: str
    filename: str | None

    @property
    def is_archive(self) -> bool:
        return self.media_type == _ARCHIVE_MEDIA_TYPE

    @property
    def is_text(self) -> bool:
        """Whether ``text`` will answer rather than raise.

        An empty media type counts as text: a missing Content-Type is Mode declining to
        say, not saying "binary".
        """
        kind = self.media_type
        return (
            not kind
            or kind.startswith("text/")
            or kind.endswith("+json")
            or (kind in _TEXT_MEDIA_TYPES)
        )

    @property
    def text(self) -> str:
        """Raises on binary rather than answering with mojibake; use ``tables()`` for the
        members of an archive.
        """
        if not self.is_text:
            raise ValueError(f"this export is {self.media_type}, not text; use tables() or save()")
        return _decode(self.content)

    def tables(self) -> dict[str, str]:
        """Every query's CSV keyed by file name, whether or not Mode sent an archive."""
        if not self.is_archive:
            return {self.filename or "results": self.text}
        with zipfile.ZipFile(io.BytesIO(self.content)) as archive:
            return {name: _decode(archive.read(name)) for name in archive.namelist()}

    def save(self, path: str | os.PathLike[str]) -> pathlib.Path:
        """Write bytes, never text. Pointing at a directory keeps Mode's own file name."""
        target = pathlib.Path(path)
        if target.is_dir():
            target = target / (self.filename or "results")
        target.write_bytes(self.content)
        return target

    def __repr__(self) -> str:
        # The generated repr would put an entire export in every traceback.
        named = f" {self.filename}" if self.filename else ""
        return f"<RunResults {self.media_type}{named} {len(self.content)} bytes>"


class CronSpec(TypedDict, total=False):
    """The nested ``cron`` object a report schedule is written with.

    ``hour`` is 0-23, ``minute`` in five-minute steps, ``day_of_week`` 1-7 and
    ``day_of_month`` 1-31. A ``ReportSchedule`` reads these back as prose and keeps the
    integers under ``cron_hour`` / ``cron_minute``.
    """

    freq: Literal["every_15_minutes", "every_30_minutes", "hourly", "daily", "weekly", "monthly"]
    hour: int
    minute: int
    time_zone: str
    day_of_week: list[int]
    day_of_month: int


def _results(response: httpx.Response) -> RunResults:
    match = _FILENAME.search(response.headers.get("content-disposition", ""))
    return RunResults(
        content=response.content,
        media_type=response.headers.get("content-type", "").split(";")[0].strip(),
        filename=match.group(1).strip() if match else None,
    )


def _href_path(href: str) -> str:
    """A HAL href reduced to a path this transport can request.

    Mode mixes relative and absolute hrefs inside one document, and carries the ``/api``
    prefix on some and not others, so both are normalised here.
    """
    path = httpx.URL(href).path if "://" in href else href
    return path.removeprefix("/api")


class ExportsResource(Resource):
    """Result payloads. Every path here returns data, not a HAL document."""

    def report_run(self, report: ReportRef, run: RunRef, *, format: Format = "csv") -> RunResults:
        """One run's results, in whichever shape Mode chose to send them.

        **``format="json"`` silently returns only the first query's rows** on a
        multi-query report, with no marker and no error, which is why it logs a WARNING.
        Use CSV and ``tables()`` for every query.

        Whether the CSV arrives as ``text/csv`` or ``application/zip`` does not follow
        from the query count -- branch on ``media_type``, or call ``tables()``.

        A large export can exceed the 60s default read timeout, and the resulting 504 is
        retryable, so the caller pays several gateway timeouts before it raises::

            mode.with_options(timeout=httpx.Timeout(600.0, connect=5.0)).exports.report_run(r, run)
        """
        report_token = token_of(report, "report")
        run_token = token_of(run, "run")
        if format == "json":
            _log.warning(
                "report-level content.json returns only the first query's rows; any other "
                "query on report %s is dropped with no marker. Use format='csv' or "
                "report_run_tables() for all of them.",
                report_token,
            )
        path = f"/reports/{report_token}/runs/{run_token}/results/content.{format}"
        return _results(self._t.request("GET", path))

    def report_run_tables(self, report: ReportRef, run: RunRef) -> dict[str, str]:
        """Every query's CSV from a report run, keyed by file name, zip or not.

        CSV only: the JSON export cannot deliver every query (see ``report_run``).
        """
        return self.report_run(report, run).tables()

    def query_run(
        self, report: ReportRef, run: RunRef, query_run: QueryRunRef, *, format: Format = "csv"
    ) -> str:
        """Always one query's own result, so always text -- never an archive.

        Also the way to get JSON out of a multi-query report: one call per query run.
        """
        path = (
            f"/reports/{token_of(report, 'report')}"
            f"/runs/{token_of(run, 'run')}"
            f"/query_runs/{token_of(query_run, 'query_run')}/results/content.{format}"
        )
        return self._t.text("GET", path)

    def pdf_export(self, report: ReportRef, run: RunRef) -> PdfExport:
        """The PDF render job for a run -- and the call that starts one.

        A GET with a side effect: Mode enqueues the render the first time it is asked.
        Later GETs return the same document rather than re-queueing, so polling is safe.
        Use ``pdf()`` unless you want the job document itself.
        """
        path = f"/reports/{token_of(report, 'report')}/exports/runs/{token_of(run, 'run')}/pdf.pdf"
        return PdfExport.from_payload(self._t.payload("GET", path))

    def pdf(
        self, report: ReportRef, run: RunRef, *, timeout: float = 300.0, interval: float = 2.0
    ) -> RunResults:
        """A run's PDF, rendering it first if Mode has never rendered one.

        A PDF export is two steps: the download link answers
        ``404 export not yet generated`` until a render has been requested, which is the
        normal state for most runs. ``pdf_export()`` starts it, this polls it, and the
        bytes come back from the job's ``download`` link. ``timeout`` is wall-clock and
        raises ``RunTimeoutError`` carrying the last state seen.
        """
        deadline = time.monotonic() + timeout
        while True:
            export = self.pdf_export(report, run)
            href = export.download_href
            if href:
                return _results(self._t.request("GET", _href_path(href), workspace=False))
            if time.monotonic() >= deadline:
                token = token_of(run, "run")
                raise RunTimeoutError(
                    f"the PDF export for run {token} was still {export.state} after {timeout:.0f}s",
                    token=token,
                    state=export.state,
                )
            time.sleep(interval)

    def from_href(self, href: str, *, format: Format = "csv") -> RunResults:
        """Follow a HAL link to a result, absolute or relative, with or without the suffix.

        Mode advertises both spellings -- ``_links.result`` needs ``/content.csv``
        appended, ``_links.content`` already carries it -- so the suffix is added only
        when missing.
        """
        path = _href_path(href)
        if not _CONTENT_SUFFIX.search(path):
            path = f"{path.rstrip('/')}/content.{format}"
        return _results(self._t.request("GET", path, workspace=False))


class ReportSchedulesResource(Resource):
    """Scheduled runs of a report.

    ``create`` and ``update`` send the four inputs Mode declares -- ``name``, ``cron``
    (``CronSpec``), ``params`` and ``timeout`` -- wrapped in ``report_schedule``. An
    omitted keyword is left alone rather than nulled.
    """

    def list(
        self,
        report: ReportRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[ReportSchedule]:
        return self._many(
            ReportSchedule,
            f"/reports/{token_of(report, 'report')}/schedules",
            "report_schedules",
            per_page=per_page,
            extra_params=extra_params,
        )

    def get(self, report: ReportRef, schedule: ScheduleRef) -> ReportSchedule:
        return self._one(
            ReportSchedule,
            "GET",
            f"/reports/{token_of(report, 'report')}/schedules/{token_of(schedule, 'schedule')}",
        )

    def create(
        self,
        report: ReportRef,
        *,
        name: str | None = None,
        cron: CronSpec | None = None,
        params: Mapping[str, object] | None = None,
        timeout: int | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> ReportSchedule:
        payload = write_body(
            "report_schedule", extra_body, name=name, cron=cron, params=params, timeout=timeout
        )
        return self._one(
            ReportSchedule,
            "POST",
            f"/reports/{token_of(report, 'report')}/schedules",
            json=payload,
        )

    def update(
        self,
        report: ReportRef,
        schedule: ScheduleRef,
        *,
        name: str | None = None,
        cron: CronSpec | None = None,
        params: Mapping[str, object] | None = None,
        timeout: int | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> ReportSchedule:
        payload = write_body(
            "report_schedule", extra_body, name=name, cron=cron, params=params, timeout=timeout
        )
        return self._one(
            ReportSchedule,
            "PATCH",
            f"/reports/{token_of(report, 'report')}/schedules/{token_of(schedule, 'schedule')}",
            json=payload,
        )

    def delete(self, report: ReportRef, schedule: ScheduleRef) -> None:
        self._t.request(
            "DELETE",
            f"/reports/{token_of(report, 'report')}/schedules/{token_of(schedule, 'schedule')}",
        )


class ReportSubscriptionsResource(Resource):
    """Who gets a scheduled report, and what is attached to it.

    Writes are wrapped in ``report_subscription``; an omitted keyword leaves that setting
    as it was.
    """

    def list(
        self,
        report: ReportRef,
        *,
        schedule: ScheduleRef | None = None,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[ReportSubscription]:
        """Every subscription on the report, or only those of one schedule."""
        report_token = token_of(report, "report")
        path = (
            f"/reports/{report_token}/schedules/{token_of(schedule, 'schedule')}/subscriptions"
            if schedule is not None
            else f"/reports/{report_token}/subscriptions"
        )
        return self._many(
            ReportSubscription,
            path,
            "report_subscriptions",
            per_page=per_page,
            extra_params=extra_params,
        )

    def get(self, report: ReportRef, subscription: SubscriptionRef) -> ReportSubscription:
        return self._one(
            ReportSubscription,
            "GET",
            f"/reports/{token_of(report, 'report')}"
            f"/subscriptions/{token_of(subscription, 'subscription')}",
        )

    def create(
        self,
        report: ReportRef,
        *,
        csv_attachments_enabled: bool | None = None,
        pdf_attachments_enabled: bool | None = None,
        data_previews_enabled: bool | None = None,
        data_tables_enabled: bool | None = None,
        report_links_enabled: bool | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> ReportSubscription:
        payload = write_body(
            "report_subscription",
            extra_body,
            csv_attachments_enabled=csv_attachments_enabled,
            pdf_attachments_enabled=pdf_attachments_enabled,
            data_previews_enabled=data_previews_enabled,
            data_tables_enabled=data_tables_enabled,
            report_links_enabled=report_links_enabled,
        )
        return self._one(
            ReportSubscription,
            "POST",
            f"/reports/{token_of(report, 'report')}/subscriptions",
            json=payload,
        )

    def update(
        self,
        report: ReportRef,
        subscription: SubscriptionRef,
        *,
        csv_attachments_enabled: bool | None = None,
        pdf_attachments_enabled: bool | None = None,
        data_previews_enabled: bool | None = None,
        data_tables_enabled: bool | None = None,
        report_links_enabled: bool | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> ReportSubscription:
        payload = write_body(
            "report_subscription",
            extra_body,
            csv_attachments_enabled=csv_attachments_enabled,
            pdf_attachments_enabled=pdf_attachments_enabled,
            data_previews_enabled=data_previews_enabled,
            data_tables_enabled=data_tables_enabled,
            report_links_enabled=report_links_enabled,
        )
        return self._one(
            ReportSubscription,
            "PATCH",
            f"/reports/{token_of(report, 'report')}"
            f"/subscriptions/{token_of(subscription, 'subscription')}",
            json=payload,
        )

    def delete(self, report: ReportRef, subscription: SubscriptionRef) -> None:
        self._t.request(
            "DELETE",
            f"/reports/{token_of(report, 'report')}"
            f"/subscriptions/{token_of(subscription, 'subscription')}",
        )


class DatasetSchedulesResource(Resource):
    """A dataset's schedules, which are report schedules wearing a dataset's URL.

    Read-only on purpose: Mode publishes no per-schedule route here, so get, update and
    delete go through ``mode.report_schedules`` with the schedule's ``report_token``.
    """

    def list(
        self,
        dataset: DatasetRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[DatasetSchedule]:
        return self._many(
            DatasetSchedule,
            f"/datasets/{token_of(dataset, 'dataset')}/schedules",
            "report_schedules",
            per_page=per_page,
            extra_params=extra_params,
        )
