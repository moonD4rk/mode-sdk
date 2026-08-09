"""Typed views over Mode's HAL payloads.

Every model keeps the dict it was built from in ``raw``, and every field is optional
because Mode's key set varies by object type and by projection endpoint. Timestamps are
the one coerced type; one that will not parse leaves the attribute None and keeps the
original string in ``raw``.

**Every numeric-looking id Mode sends is a JSON string, and these models say so.** An id
is an identifier and never an operand -- Mode's routes take the 12-character token, and
``GET /data_sources/10001`` 404s where the token answers 200. Nothing is coerced; a
caller who wants a number writes ``int(report.id)``. Write parameters accept
``int | str``, because Mode's ``_forms.edit`` declares these as JSON integers.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from dataclasses import fields
from datetime import datetime
from functools import cache
from typing import Any
from typing import Literal
from typing import Self
from typing import get_args
from typing import get_type_hints


@cache
def _datetime_fields(cls: type) -> frozenset[str]:
    hints = get_type_hints(cls)
    return frozenset(n for n, h in hints.items() if h is datetime or datetime in get_args(h))


def _to_datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def link_token(payload: dict[str, Any], rel: str) -> str | None:
    """Mode identifies a related resource only by its href; the token is its last segment."""
    href = (payload.get("_links") or {}).get(rel, {}).get("href", "")
    return href.rstrip("/").rsplit("/", 1)[-1] or None


@dataclass(frozen=True, slots=True)
class Model:
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> Self:
        """Build a model from a payload -- the way to construct one offline.

        Every field is defaulted, so a model built by calling the class directly loses
        ``raw`` and every undeclared key with it.
        """
        payload = payload or {}
        stamps = _datetime_fields(cls)
        values: dict[str, Any] = {"raw": payload}
        for f in fields(cls):
            if f.name == "raw" or f.name not in payload:
                continue
            values[f.name] = _to_datetime(payload[f.name]) if f.name in stamps else payload[f.name]
        return cls(**values)

    _from = from_payload

    def link(self, rel: str) -> str | None:
        """The raw href for a relation. HAL's ``templated`` flag is not honoured, so a
        templated rel comes back containing a literal ``{token}`` and cannot be requested
        as-is.
        """
        return (self.raw.get("_links") or {}).get(rel, {}).get("href")


@dataclass(frozen=True, slots=True)
class Verification(Model):
    """What ``GET /verify`` answers: which workspace a key opens and which API it may use.

    ``api_key_scope`` constrains the API surface, not the person, and does not track the
    owner's ``membership_type``. A ``member`` key is refused by the endpoints Mode
    reserves for Workspace tokens whatever the human's role.
    """

    workspace: str | None = None
    workspace_token: str | None = None
    api_key_scope: str | None = None


@dataclass(frozen=True, slots=True)
class Workspace(Model):
    """``raw["space_count"]`` is unmodelled because it reads 0 on a populated workspace;
    count Collections with ``len(mode.spaces.list(filter="all").list())``.
    """

    token: str | None = None
    id: str | None = None
    username: str | None = None
    name: str | None = None
    plan_code: str | None = None
    trial_state: str | None = None
    membership_type: str | None = None
    data_source_count: int | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Space(Model):
    """Collections carry no timestamp of any kind -- no ``created_at``, no ``updated_at``.

    ``raw["viewable?"]`` is not a Python identifier and describes the response rather
    than the Collection: the same one reads false in a listing and true when fetched.
    """

    token: str | None = None
    id: str | None = None
    name: str | None = None
    description: str | None = None
    space_type: str | None = None
    state: str | None = None
    default_access_level: str | None = None
    schema_name: str | None = None
    restricted: bool | None = None


@dataclass(frozen=True, slots=True)
class SpaceMembership(Model):
    """``member_id`` is the member's Mode username, not a number. No endpoint returns a
    timestamp on this object.
    """

    token: str | None = None
    member_type: str | None = None
    member_token: str | None = None
    member_id: str | None = None
    email: str | None = None


@dataclass(frozen=True, slots=True)
class Report(Model):
    """``last_run_at`` is when the last run *started*; ``last_successfully_run_at`` is when
    it finished. Reading the first as "when did this last finish" is off by a whole run.

    ``name`` is an explicit null on an untitled report. ``archived``, ``chart_count`` and
    ``expected_runtime`` are absent on ``DatasetReport`` rows, which share this envelope.
    """

    token: str | None = None
    id: str | None = None
    name: str | None = None
    description: str | None = None
    type: str | None = None
    space_token: str | None = None
    account_id: str | None = None
    account_username: str | None = None
    public: bool | None = None
    archived: bool | None = None
    query_count: int | None = None
    max_query_count: int | None = None
    chart_count: int | None = None
    runs_count: int | None = None
    schedules_count: int | None = None
    view_count: int | None = None
    expected_runtime: float | None = None
    github_link: str | None = None
    last_successful_run_token: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    edited_at: datetime | None = None
    last_saved_at: datetime | None = None
    last_run_at: datetime | None = None
    last_successfully_run_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReportRun(Model):
    """``python_state`` arrives as the literal string ``"none"``, never as null, so
    ``if run.python_state:`` is true for every run and means the opposite of how it reads.
    Compare it against a state name instead.

    Purge state is two nullable timestamps rather than a boolean; ``purged`` derives from
    them. Fetching results for a purged run answers 404 ``run results not found``.

    ``raw["form_fields"]`` holds a richer per-run record of the parameter form than the
    ``/options`` endpoint returns.
    """

    token: str | None = None
    state: str | None = None
    python_state: str | None = None
    parameters: dict[str, Any] | None = None
    is_latest_report_run: bool | None = None
    is_latest_successful_report_run: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    purge_started_at: datetime | None = None
    purge_completed_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_RUN_STATES

    @property
    def succeeded(self) -> bool:
        return self.state in SUCCESS_RUN_STATES

    @property
    def purged(self) -> bool:
        """True once a purge has *begun*: from that point the results may already be gone."""
        return self.purge_started_at is not None


@dataclass(frozen=True, slots=True)
class PdfExport(Model):
    """Mode's render job for one run's PDF -- a job document, not a PDF.

    ``state`` walks ``new`` -> ``requested`` -> ``completed`` and the ``download`` link
    exists only at the end. ``mode.exports.pdf()`` runs the whole flow and returns bytes.
    """

    state: str | None = None
    filename: str | None = None
    stale: bool | None = None
    created_at: datetime | None = None

    @property
    def download_href(self) -> str | None:
        """Where the finished PDF is, or ``None`` while the render is still running."""
        return self.link("download")

    @property
    def is_ready(self) -> bool:
        return self.download_href is not None


@dataclass(frozen=True, slots=True)
class Query(Model):
    token: str | None = None
    id: str | None = None
    name: str | None = None
    raw_query: str | None = None
    data_source_id: str | None = None
    explorations_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class QueryRun(Model):
    """``error_type`` is the constant string ``"generic"``; branch on ``error_code``
    (``SqlUnknownColumn``, ``ResourceFailure``, ...) instead. Both are absent on a run
    that did not fail.

    ``data_source_token`` is the identifier that routes; ``data_source_id`` 404s. Query
    runs carry no ``updated_at``.
    """

    token: str | None = None
    state: str | None = None
    data_source_id: str | None = None
    data_source_token: str | None = None
    raw_source: str | None = None
    rendered_source: str | None = None
    limit: bool | None = None
    error_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def query_token(self) -> str | None:
        """Mode sends this as a plain top-level string; the HAL link is the fallback."""
        return self.raw.get("query_token") or link_token(self.raw, "query")


@dataclass(frozen=True, slots=True)
class Chart(Model):
    """A chart's type and title live inside ``view_vegas``, not at the top level.
    ``view`` is documented but arrives as ``{}``, so the accessors below ignore it.

    ``chart_type`` is the *renderer* name and does not identify the visualization; the
    mark type is nested under ``view_vegas["encoding"]["marks"]``. ``chart_title`` is
    HTML-entity-encoded and is ``""`` rather than None when untitled.
    """

    token: str | None = None
    view: dict[str, Any] | None = None
    view_version: int | None = None
    view_vegas: dict[str, Any] | None = None
    color_palette_token: str | None = None
    switch_view_token: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def chart_type(self) -> str | None:
        return (self.view_vegas or {}).get("chartType")

    @property
    def chart_title(self) -> str | None:
        return (self.view_vegas or {}).get("title")


@dataclass(frozen=True, slots=True)
class ReportFilter(Model):
    token: str | None = None
    name: str | None = None
    formula: str | None = None
    data_type: str | None = None
    formula_type: str | None = None
    filter_type: str | None = None
    control_type: str | None = None
    variable_type: str | None = None
    options: Any = None
    next_filter_token: str | None = None


@dataclass(frozen=True, slots=True)
class FormField(Model):
    """The ``/options`` projection of a run's parameter form.
    ``ReportRun.raw["form_fields"]`` is the same form unprojected and carries more.
    """

    name: str | None = None
    options: Any = None
    options_error: str | None = None
    source_size: str | None = None


@dataclass(frozen=True, slots=True)
class Definition(Model):
    token: str | None = None
    id: str | None = None
    name: str | None = None
    description: str | None = None
    source: str | None = None
    data_source_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DataSource(Model):
    """``soft_deleted`` and ``asleep`` gate whether a source is usable; ``queryable`` and
    ``default_access_level`` describe what may be done with it. ``id`` does not route --
    every path segment wants ``token``.
    """

    token: str | None = None
    id: str | None = None
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    adapter: str | None = None
    vendor: str | None = None
    database: str | None = None
    host: str | None = None
    port: int | None = None
    username: str | None = None
    provider: str | None = None
    public: bool | None = None
    default_access_level: str | None = None
    ssl: bool | None = None
    queryable: bool | None = None
    asleep: bool | None = None
    soft_deleted: bool | None = None
    account_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Dataset(Model):
    """A dataset is a report of ``type="DatasetReport"``, arriving under the same
    ``_embedded.reports`` key.

    ``description`` is present on listings and absent from the single-dataset GET, so
    re-fetching to enrich a listing loses it.
    """

    token: str | None = None
    id: str | None = None
    name: str | None = None
    description: str | None = None
    type: str | None = None
    space_token: str | None = None
    query_count: int | None = None
    runs_count: int | None = None
    last_successful_run_token: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_run_at: datetime | None = None
    last_successfully_run_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DatasetRun(Model):
    """Report runs under ``_embedded.report_runs``, minus ``python_state``,
    ``parameters`` and ``form_fields``.
    """

    token: str | None = None
    state: str | None = None
    is_latest_report_run: bool | None = None
    is_latest_successful_report_run: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    purge_started_at: datetime | None = None
    purge_completed_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_RUN_STATES

    @property
    def purged(self) -> bool:
        """True once a purge has *begun*: from that point the results may already be gone."""
        return self.purge_started_at is not None


@dataclass(frozen=True, slots=True)
class DatasetField(Model):
    """``/datasets/{d}/fields`` returns these two keys and nothing else."""

    name: str | None = None
    type: str | None = None


@dataclass(frozen=True, slots=True)
class FieldDescription(Model):
    token: str | None = None
    name: str | None = None
    desc: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class User(Model):
    """Mode publishes no email on a user record; it lives on a workspace membership."""

    token: str | None = None
    id: str | None = None
    username: str | None = None
    name: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Membership(Model):
    """``email``, ``name``, ``username``, ``admin``, ``state`` and ``member_token`` are
    what the reachable listing carries; the rest belong to the single-membership endpoint.
    """

    token: str | None = None
    admin: bool | None = None
    invited: bool | None = None
    state: str | None = None
    email: str | None = None
    name: str | None = None
    username: str | None = None
    member_username: str | None = None
    member_token: str | None = None
    managed_by_scim: bool | None = None
    activated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Invite(Model):
    token: str | None = None
    email: str | None = None
    message: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Group(Model):
    """``id`` and both timestamps are frequently absent; expect None."""

    token: str | None = None
    id: str | None = None
    name: str | None = None
    state: str | None = None
    group_type: str | None = None
    member_count: int | None = None
    spaces_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GroupMembership(Model):
    token: str | None = None
    member_token: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuditLogEntry(Model):
    """The response object, which is not shaped like the filters that select it.

    ``entity_id``, ``entity_type``, ``entity_name``, ``username`` and ``ip`` go up as
    query parameters and come back nested inside ``entity`` and ``actor``; the accessors
    below flatten them. ``event_source`` has no counterpart in the response.
    """

    id: str | None = None
    action: str | None = None
    description: str | None = None
    context: dict[str, Any] | None = None
    actor: dict[str, Any] | None = None
    entity: dict[str, Any] | None = None
    workspace_username: str | None = None
    timestamp: datetime | None = None

    @property
    def entity_id(self) -> str | None:
        return (self.entity or {}).get("entity_id")

    @property
    def entity_type(self) -> str | None:
        return (self.entity or {}).get("entity_type")

    @property
    def entity_name(self) -> str | None:
        return (self.entity or {}).get("entity_name")

    @property
    def username(self) -> str | None:
        return (self.actor or {}).get("username")

    @property
    def ip(self) -> str | None:
        return (self.actor or {}).get("ip")


@dataclass(frozen=True, slots=True)
class ReportSchedule(Model):
    """``hour``, ``minute`` and ``day_of_week`` are display strings, not numbers.

    Mode reads integers and writes back prose (``hour="9:45 am"``,
    ``minute="45 minutes past the hour"``). Build cron from ``cron_hour`` /
    ``cron_minute``. ``day_of_week`` is present only on weekly schedules.
    """

    token: str | None = None
    name: str | None = None
    frequency: str | None = None
    hour: str | None = None
    minute: str | None = None
    cron_hour: int | None = None
    cron_minute: int | None = None
    day_of_week: str | None = None
    day_of_month: str | None = None
    time_zone: str | None = None
    parameters: dict[str, Any] | None = None
    timeout: int | None = None
    subscribed: bool | None = None
    last_run_at: datetime | None = None
    next_scheduled_run: datetime | None = None


@dataclass(frozen=True, slots=True)
class DatasetSchedule(Model):
    """Report schedules under ``_embedded.report_schedules``, ``hour``/``minute`` prose
    included. Build cron from ``cron_hour`` / ``cron_minute``.
    """

    token: str | None = None
    name: str | None = None
    frequency: str | None = None
    hour: str | None = None
    minute: str | None = None
    cron_hour: int | None = None
    cron_minute: int | None = None
    time_zone: str | None = None
    parameters: dict[str, Any] | None = None
    retry_state: str | None = None
    retry_count: int | None = None
    retry_delay: int | None = None
    timeout: int | None = None
    subscribed: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_run_at: datetime | None = None
    last_succeeded_at: datetime | None = None
    next_scheduled_run: datetime | None = None
    last_scheduled_run: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReportSubscription(Model):
    token: str | None = None
    data_previews_enabled: bool | None = None
    data_tables_enabled: bool | None = None
    csv_attachments_enabled: bool | None = None
    pdf_attachments_enabled: bool | None = None
    report_links_enabled: bool | None = None
    email_subscriber_count: int | None = None


@dataclass(frozen=True, slots=True)
class SignatureToken(Model):
    token: str | None = None
    name: str | None = None
    access_key: str | None = None
    access_secret: str | None = None
    scopes: list[str] | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryObject(Model):
    """A row from the Discovery batch API, which returns flat JSON rather than HAL.

    Only the identifiers common to every batch resource are lifted out; Mode documents
    the rest per endpoint, so it stays in ``raw``.
    """

    token: str | None = None
    id: str | None = None
    name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# "completed" is the terminal state of a notebook report; a poller that drops it hangs
# forever on one. The model fields stay ``str | None`` so an unlisted state still parses.
RunState = Literal["enqueued", "succeeded", "completed", "failed", "cancelled"]

TERMINAL_RUN_STATES = frozenset({"succeeded", "completed", "failed", "cancelled"})
SUCCESS_RUN_STATES = frozenset({"succeeded", "completed"})

PdfExportState = Literal["new", "requested", "completed"]
