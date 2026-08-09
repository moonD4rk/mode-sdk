"""The Mode client: one HTTP session, one namespace per resource family."""

from __future__ import annotations

import copy
import ssl
from collections.abc import Mapping
from functools import cached_property
from typing import Any

import httpx

from ._config import SECRET_VAR
from ._config import TOKEN_VAR
from ._config import WORKSPACE_VAR
from ._config import refuse_ignored_options
from ._config import required
from ._config import resolve
from ._constants import BASE_URL
from ._constants import WEB_URL
from ._transport import DEFAULT_TIMEOUT
from ._transport import Transport
from ._types import QueryValue
from .models import Verification
from .resources._args import ReportRef
from .resources._args import token_of
from .resources.admin import AuditLogsResource
from .resources.admin import GroupsResource
from .resources.admin import InvitesResource
from .resources.admin import MembershipsResource
from .resources.data_sources import DataSourcesResource
from .resources.datasets import DatasetRunsResource
from .resources.datasets import DatasetsResource
from .resources.datasets import FieldDescriptionsResource
from .resources.definitions import DefinitionsResource
from .resources.distribution import DatasetSchedulesResource
from .resources.distribution import ExportsResource
from .resources.distribution import ReportSchedulesResource
from .resources.distribution import ReportSubscriptionsResource
from .resources.queries import ChartsResource
from .resources.queries import QueriesResource
from .resources.queries import QueryRunsResource
from .resources.reports import ReportFiltersResource
from .resources.reports import ReportRunsResource
from .resources.reports import ReportsResource
from .resources.workspace import SpaceMembershipsResource
from .resources.workspace import SpacesResource
from .resources.workspace import WorkspaceResource


class Mode:
    """A Mode workspace.

    Credentials are an API token and secret used as HTTP basic auth, taken from the
    arguments or from MODE_WORKSPACE, MODE_API_TOKEN and MODE_API_SECRET.

        with Mode("acme", token=token, secret=secret) as mode:
            for report in mode.reports.list(space=space_token):
                print(report.name)

    Build one client and share it across threads; ``Page`` objects are the exception,
    each belonging to the thread walking it. Use ``with_options()`` where one call site
    needs a different deadline or retry budget.
    """

    def __init__(
        self,
        workspace: str | None = None,
        *,
        token: str | None = None,
        secret: str | None = None,
        base_url: str = BASE_URL,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        default_headers: Mapping[str, str] | None = None,
        limits: httpx.Limits | None = None,
        http2: bool = False,
        verify: bool | ssl.SSLContext = True,
        proxy: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        owned = http_client is None
        if not owned:
            refuse_ignored_options(
                [
                    ("timeout", timeout is not DEFAULT_TIMEOUT),
                    ("default_headers", default_headers is not None),
                    ("limits", limits is not None),
                    ("http2", bool(http2)),
                    ("verify", verify is not True),
                    ("proxy", proxy is not None),
                ]
            )

        # An injected client may carry its own auth, so credentials are optional then --
        # but half a pair never means that, and the workspace is path scoping, not policy.
        self.workspace_name = required(
            resolve(workspace, WORKSPACE_VAR), "workspace", WORKSPACE_VAR
        )
        token = resolve(token, TOKEN_VAR)
        secret = resolve(secret, SECRET_VAR)
        if owned or token or secret:
            token = required(token, "token", TOKEN_VAR)
            secret = required(secret, "secret", SECRET_VAR)

        self.transport = Transport(
            self.workspace_name,
            token,
            secret,
            base_url=base_url,
            timeout=timeout if owned else None,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            headers=default_headers,
            limits=limits,
            http2=http2 if owned else None,
            verify=verify if owned else None,
            proxy=proxy,
            http_client=http_client,
        )

    def with_options(
        self,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        default_headers: Mapping[str, str] | None = None,
    ) -> Mode:
        """This client with different options, sharing its connection pool.

        ``close()`` on the copy is a no-op; closing the original closes every view.
        """
        clone = copy.copy(self)
        # Built namespaces hold the transport they were built from, so drop them and let
        # them rebuild. The lookup goes through the class: a subclass's dict holds none.
        for name in list(clone.__dict__):
            if isinstance(getattr(type(self), name, None), cached_property):
                del clone.__dict__[name]
        clone.transport = self.transport.with_options(
            timeout=timeout, max_retries=max_retries, headers=default_headers
        )
        return clone

    @cached_property
    def exports(self) -> ExportsResource:
        return ExportsResource(self.transport)

    @cached_property
    def query_runs(self) -> QueryRunsResource:
        return QueryRunsResource(self.transport, self.exports)

    @cached_property
    def report_runs(self) -> ReportRunsResource:
        return ReportRunsResource(self.transport, self.query_runs, self.exports)

    @cached_property
    def queries(self) -> QueriesResource:
        return QueriesResource(self.transport)

    @cached_property
    def reports(self) -> ReportsResource:
        return ReportsResource(self.transport, self.report_runs, self.queries)

    @cached_property
    def workspace(self) -> WorkspaceResource:
        return WorkspaceResource(self.transport)

    @cached_property
    def spaces(self) -> SpacesResource:
        return SpacesResource(self.transport)

    @cached_property
    def space_memberships(self) -> SpaceMembershipsResource:
        return SpaceMembershipsResource(self.transport)

    @cached_property
    def report_filters(self) -> ReportFiltersResource:
        return ReportFiltersResource(self.transport)

    @cached_property
    def charts(self) -> ChartsResource:
        return ChartsResource(self.transport)

    @cached_property
    def definitions(self) -> DefinitionsResource:
        return DefinitionsResource(self.transport)

    @cached_property
    def data_sources(self) -> DataSourcesResource:
        return DataSourcesResource(self.transport)

    @cached_property
    def datasets(self) -> DatasetsResource:
        return DatasetsResource(self.transport)

    @cached_property
    def dataset_runs(self) -> DatasetRunsResource:
        return DatasetRunsResource(self.transport)

    @cached_property
    def dataset_fields(self) -> FieldDescriptionsResource:
        return FieldDescriptionsResource(self.transport)

    @cached_property
    def memberships(self) -> MembershipsResource:
        return MembershipsResource(self.transport)

    @cached_property
    def invites(self) -> InvitesResource:
        return InvitesResource(self.transport)

    @cached_property
    def groups(self) -> GroupsResource:
        return GroupsResource(self.transport)

    @cached_property
    def audit_logs(self) -> AuditLogsResource:
        return AuditLogsResource(self.transport)

    @cached_property
    def report_schedules(self) -> ReportSchedulesResource:
        return ReportSchedulesResource(self.transport)

    @cached_property
    def report_subscriptions(self) -> ReportSubscriptionsResource:
        return ReportSubscriptionsResource(self.transport)

    @cached_property
    def dataset_schedules(self) -> DatasetSchedulesResource:
        return DatasetSchedulesResource(self.transport)

    def verify(self) -> Verification:
        """Confirm the credentials and report which workspace and scope they carry."""
        return self.workspace.verify()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        json: object | None = None,
        workspace: bool = True,
    ) -> dict[str, Any]:
        """Call an endpoint this package does not model. Paths are workspace-relative.

        JSON that is not an object arrives under ``_value``, an empty body as ``{}``. For
        a status line, a header or raw bytes, use ``mode.transport``.
        """
        return self.transport.payload(method, path, params=params, json=json, workspace=workspace)

    def report_url(self, report: ReportRef) -> str:
        """The browser URL for a report, from the ``Report`` or from its token."""
        return f"{WEB_URL}/{self.workspace_name}/reports/{token_of(report, 'report')}"

    @property
    def is_closed(self) -> bool:
        return self.transport.is_closed

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> Mode:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<Mode workspace={self.workspace_name!r}>"
