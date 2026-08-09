"""The SQL inside a report: its queries, their charts, and the runs they produce.

**The enclosing report is decoration.** Mode resolves ``{query}`` and ``{chart}`` by
their own workspace-wide tokens, so a mismatched pair answers 200 with another report's
data rather than 404, and only ``_links.self`` names the true owner. Pass the model you
were handed, not a token you assembled.

**None of these collections paginate.** They return their full contents for every
``page`` and ``per_page``, so the duplicate-page guard ends the walk at the cost of one
extra request, and ``per_page`` cannot trim a large payload.
"""

from __future__ import annotations

from collections.abc import Mapping

from .._transport import Transport
from .._types import QueryValue
from ..models import Chart
from ..models import Query
from ..models import QueryRun
from ..pagination import Page
from ._args import ChartRef
from ._args import QueryRef
from ._args import QueryRunRef
from ._args import ReportRef
from ._args import RunRef
from ._args import token_of
from ._args import write_body
from ._base import Resource
from .distribution import ExportsResource
from .distribution import Format


class QueriesResource(Resource):
    def list(
        self,
        report: ReportRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[Query]:
        """Every query in a report, in report order. Mode ignores ``page`` and ``per_page``."""
        return self._many(
            Query,
            f"/reports/{token_of(report, 'report')}/queries",
            "queries",
            extra_params=extra_params,
            per_page=per_page,
        )

    def get(self, report: ReportRef, query: QueryRef) -> Query:
        """One query. The report token is not checked against the query's real owner, so a
        mismatched pair reads as success; ``Query.link("report")`` names the owner.
        """
        return self._one(
            Query,
            "GET",
            f"/reports/{token_of(report, 'report')}/queries/{token_of(query, 'query')}",
        )

    def create(
        self,
        report: ReportRef,
        raw_query: str,
        data_source_id: int | str,
        *,
        name: str | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> Query:
        """Add a query to a report. Mode wraps the payload in ``query``.

        ``data_source_id`` takes either JSON type, so a value read off a model needs no
        cast. An id naming no reachable data source answers ``404 data source not found``,
        and being listed by ``GET /data_sources`` does not make one reachable.

        A ``None`` is dropped rather than sent; ``extra_body`` sends one deliberately.
        """
        payload = write_body(
            "query", extra_body, raw_query=raw_query, data_source_id=data_source_id, name=name
        )
        return self._one(
            Query, "POST", f"/reports/{token_of(report, 'report')}/queries", json=payload
        )

    def update(
        self,
        report: ReportRef,
        query: QueryRef,
        *,
        raw_query: str | None = None,
        data_source_id: int | str | None = None,
        name: str | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> Query:
        """Change a query's SQL, name or data source. Only what is passed is sent."""
        payload = write_body(
            "query", extra_body, raw_query=raw_query, data_source_id=data_source_id, name=name
        )
        return self._one(
            Query,
            "PATCH",
            f"/reports/{token_of(report, 'report')}/queries/{token_of(query, 'query')}",
            json=payload,
        )

    def delete(self, report: ReportRef, query: QueryRef) -> None:
        self._t.request(
            "DELETE",
            f"/reports/{token_of(report, 'report')}/queries/{token_of(query, 'query')}",
        )


class ChartsResource(Resource):
    def list(
        self,
        report: ReportRef,
        query: QueryRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[Chart]:
        """A query's charts. Unpaginated: Mode honours neither ``page`` nor ``per_page``."""
        return self._many(
            Chart,
            f"/reports/{token_of(report, 'report')}/queries/{token_of(query, 'query')}/charts",
            "charts",
            extra_params=extra_params,
            per_page=per_page,
        )

    def get(self, report: ReportRef, query: QueryRef, chart: ChartRef) -> Chart:
        """One chart, resolved by its own token alone: a chart fetched under the wrong
        query answers 200, and ``_links.self`` names the query that owns it.
        """
        path = (
            f"/reports/{token_of(report, 'report')}"
            f"/queries/{token_of(query, 'query')}"
            f"/charts/{token_of(chart, 'chart')}"
        )
        return self._one(Chart, "GET", path)


class QueryRunsResource(Resource):
    def __init__(self, transport: Transport, exports: ExportsResource) -> None:
        super().__init__(transport)
        self._exports = exports

    def list(
        self,
        report: ReportRef,
        run: RunRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[QueryRun]:
        """One entry per query in the report run -- where a failure's real message lives.

        Unpaginated, so it can exceed the 30-item cap ``/reports/{r}/runs`` enforces.
        """
        return self._many(
            QueryRun,
            f"/reports/{token_of(report, 'report')}/runs/{token_of(run, 'run')}/query_runs",
            "query_runs",
            extra_params=extra_params,
            per_page=per_page,
        )

    def get(self, report: ReportRef, run: RunRef, query_run: QueryRunRef) -> QueryRun:
        path = (
            f"/reports/{token_of(report, 'report')}"
            f"/runs/{token_of(run, 'run')}"
            f"/query_runs/{token_of(query_run, 'query_run')}"
        )
        return self._one(QueryRun, "GET", path)

    def results(
        self,
        report: ReportRef,
        run: RunRef,
        query_run: QueryRunRef,
        *,
        format: Format = "csv",
    ) -> str:
        """One query's results, always a single text body -- never an archive."""
        return self._exports.query_run(
            token_of(report, "report"),
            token_of(run, "run"),
            token_of(query_run, "query_run"),
            format=format,
        )
