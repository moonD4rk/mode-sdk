from __future__ import annotations

import ast
import dataclasses
import inspect
from dataclasses import FrozenInstanceError
from datetime import UTC
from datetime import datetime
from typing import get_args
from typing import get_type_hints

import pytest

from mode_sdk import DatasetRun
from mode_sdk import DatasetSchedule
from mode_sdk import Query
from mode_sdk import QueryRun
from mode_sdk import Report
from mode_sdk import ReportRun
from mode_sdk import ReportSchedule
from mode_sdk import Space
from mode_sdk import models
from mode_sdk.models import SUCCESS_RUN_STATES
from mode_sdk.models import TERMINAL_RUN_STATES
from mode_sdk.models import AuditLogEntry
from mode_sdk.models import Chart
from mode_sdk.models import RunState
from mode_sdk.models import link_token


def _datetime_field_names(cls):
    """The fields Model.from_payload will try to coerce -- empty means 'carries no timestamp'."""
    return {n for n, hint in get_type_hints(cls).items() if datetime in get_args(hint)}


# Mode's own published example row from the audit-logs reference.
AUDIT_ENTRY = {
    "id": "3f1c9a72-0b4d-4e18-9c37-2ab5d6e0f741",
    "action": "connection_group_added_with_role",
    "actor": {
        "ip": "157.52.123.42",
        "user_agent": "Mozilla/5.0",
        "user_id": "f8091a2b3c4d",
        "username": "alice",
    },
    "entity": {
        "entity_name": "Primary Warehouse",
        "entity_type": "workspace_connection",
        "entity_id": "091a2b3c4d5e",
    },
    "workspace_username": "acme",
    "timestamp": "2025-02-18T10:47:37.000000Z",
}

# A run as POST answers it: non-terminal, with python_state the string "none". The purge and
# is_latest_* keys are spelled exactly as Mode spells them -- see WIRE_KEY_SPELLINGS.
REPORT_RUN = {
    "token": "4d5e6f708192",
    "state": "enqueued",
    "python_state": "none",
    "parameters": {},
    "is_latest_report_run": True,
    "is_latest_successful_report_run": False,
    "has_more_recent_successful_run_available": False,
    "purge_started_at": None,
    "purge_completed_at": None,
    "created_at": "2026-08-07T07:47:06.501Z",
    "updated_at": "2026-08-07T07:47:06.501Z",
    "completed_at": None,
    "form_fields": [],
    "_links": {
        "self": {"href": "/api/acme/reports/2b3c4d5e6f70/runs/4d5e6f708192"},
        "report": {"href": "/api/acme/reports/2b3c4d5e6f70"},
    },
}

# A report as GET answers it, _links/_forms elided. Every id is a JSON string, and last_run_at
# precedes last_successfully_run_at because one is a start and one is a finish.
REPORT = {
    "token": "1a2b3c4d5e6f",
    "id": "2000002",
    "name": "Monthly Revenue",
    "type": "Report",
    "space_token": "f0e1d2c3b4a5",
    "account_id": "1234567",
    "account_username": "acme",
    "public": False,
    "archived": False,
    "query_count": 1,
    "max_query_count": 160,
    "chart_count": 0,
    "runs_count": 1,
    "schedules_count": 0,
    "view_count": 0,
    "expected_runtime": 14.59947,
    "github_link": "https://github.com/acme/mode-reports/blob/main/Mode/acme/x.1a2b3c4d5e6f",
    "last_successful_run_token": "5e6f708192a3",
    "created_at": "2026-07-28T08:54:03.753Z",
    "updated_at": "2026-07-28T08:54:21.883Z",
    "edited_at": "2026-07-28T08:54:06.066Z",
    "last_saved_at": "2026-07-28T08:54:06.078Z",
    "last_run_at": "2026-07-28T08:54:07.201Z",
    "last_successfully_run_at": "2026-07-28T08:54:21.865Z",
}

# A Collection's complete key set -- no timestamp of any kind.
SPACE = {
    "token": "a1b2c3d4e5f6",
    "id": "2000001",
    "space_type": "custom",
    "name": "Analytics",
    "description": None,
    "state": "active",
    "schema_name": "analytics",
    "restricted": False,
    "free_default": True,
    "viewable?": True,
    "default_access_level": "view",
}

# A failed query run. error_type is the useless constant; error_code is the classification.
QUERY_RUN = {
    "id": "6f708192a3b4",
    "token": "6f708192a3b4",
    "state": "failed",
    "created_at": "2026-06-30T09:26:04.317Z",
    "completed_at": "2026-06-30T09:26:06.573Z",
    "data_source_id": "10001",
    "data_source_token": "9f8e7d6c5b4a",
    "limit": False,
    "query_token": "708192a3b4c5",
    "query_name": "Query 5",
    "max_result_bytes": 5000000000,
    "help_url": "https://mode.com/help",
    "error_code": "SqlUnknownFunction",
    "error_type": "generic",
    "error_message": "ERROR: function date_diff(...) does not exist",
}

# A weekly schedule, whose hour/minute come back as prose and cron_* as integers.
REPORT_SCHEDULE = {
    "token": "8192a3b4c5d6",
    "name": "Untitled schedule",
    "frequency": "weekly",
    "hour": "2:00 am",
    "cron_hour": 2,
    "minute": "the top of the hour",
    "cron_minute": 0,
    "day_of_week": "Monday,Tuesday,Wednesday,Thursday,Friday",
    "time_zone": "UTC",
    "parameters": {},
    "subscribed": False,
}

# Every field Mode sends as a decimal STRING rather than a JSON number.
STRING_ID_FIELDS = [
    (models.Workspace, "id", "1234567"),
    (models.User, "id", "2000004"),
    (models.Space, "id", "2000001"),
    (models.Report, "id", "2000003"),
    (models.Report, "account_id", "1234567"),
    (models.Query, "id", "2000005"),
    (models.Query, "data_source_id", "10001"),
    (models.QueryRun, "data_source_id", "10001"),
    (models.Definition, "id", "30001"),
    (models.Definition, "data_source_id", "10001"),
    (models.DataSource, "id", "10001"),
    (models.DataSource, "account_id", "1234567"),
    (models.Dataset, "id", "2000006"),
]

# Left column: a plausible field name that would never populate. Right column: the key Mode
# actually sends, or None where it sends nothing at all on any endpoint.
WIRE_KEY_SPELLINGS = [
    (models.ReportRun, "purged", "purge_started_at"),
    (models.ReportRun, "is_latest_run", "is_latest_report_run"),
    (models.Space, "created_at", None),
    (models.SpaceMembership, "created_at", None),
    (models.QueryRun, "updated_at", None),
    (models.DataSource, "default", None),
    (models.DatasetField, "description", None),
    (models.Dataset, "archived", None),
    (models.User, "email", None),
]


def test_declared_fields_are_lifted_and_the_rest_stays_in_raw():
    report = Report.from_payload({"token": "r1", "name": "Revenue", "some_future_field": 42})
    assert (report.token, report.name) == ("r1", "Revenue")
    assert report.raw["some_future_field"] == 42


def test_absent_keys_are_none_rather_than_a_key_error():
    assert Report.from_payload({"token": "r1"}).description is None


def test_from_payload_round_trips_a_captured_payload():
    """Feeding a model's own raw back through the parser has to reproduce the model."""
    run = ReportRun.from_payload(REPORT_RUN)
    assert (run.token, run.state, run.python_state) == ("4d5e6f708192", "enqueued", "none")
    assert run.created_at == datetime(2026, 8, 7, 7, 47, 6, 501000, tzinfo=UTC)
    assert run.completed_at is None
    assert run.raw["form_fields"] == []
    assert ReportRun.from_payload(run.raw) == run


def test_the_internal_alias_builds_the_same_model_as_the_public_name():
    """``_from`` keeps the resource layer working until its call sites move to from_payload."""
    payload = {"token": "r1", "name": "Revenue"}
    assert Report._from(payload) == Report.from_payload(payload)


def test_timestamps_are_parsed_including_the_z_suffix():
    report = Report.from_payload({"created_at": "2026-07-27T10:30:00.000Z"})
    assert report.created_at == datetime(2026, 7, 27, 10, 30, tzinfo=UTC)


def test_an_unparseable_timestamp_leaves_the_attribute_none_but_keeps_the_string():
    report = Report.from_payload({"created_at": "whenever"})
    assert report.created_at is None
    assert report.raw["created_at"] == "whenever"


def test_models_are_frozen():
    report = Report.from_payload({"token": "r1"})
    with pytest.raises(FrozenInstanceError):
        report.token = "r2"


def test_models_have_no_instance_dict():
    assert not hasattr(Report.from_payload({}), "__dict__")


def test_an_empty_payload_still_produces_a_model():
    assert Query.from_payload(None).token is None


def test_completed_counts_as_terminal_and_successful():
    run = ReportRun.from_payload({"state": "completed"})
    assert run.is_terminal
    assert run.succeeded


def test_the_non_terminal_state_a_new_run_carries_is_enqueued():
    """A new run answers ``enqueued``; the SDK must not treat that as done."""
    assert not ReportRun.from_payload(REPORT_RUN).is_terminal


@pytest.mark.parametrize("state", ["pending", "running", "running_notebook", "vaporised"])
def test_a_state_outside_the_known_vocabulary_parses_instead_of_raising(state):
    """Fields are ``str | None``, so a state Mode adds tomorrow costs nobody a page of runs."""
    run = ReportRun.from_payload({"state": state})
    assert run.state == state
    assert not run.is_terminal
    assert not run.succeeded


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_finished_but_unsuccessful_states_are_terminal_only(state):
    run = ReportRun.from_payload({"state": state})
    assert run.is_terminal
    assert not run.succeeded


def test_run_state_holds_the_states_mode_emits_and_no_others():
    """``pending`` and ``running`` read plausibly and are not states this API emits."""
    assert set(get_args(RunState)) == {"enqueued", "succeeded", "completed", "failed", "cancelled"}
    assert TERMINAL_RUN_STATES < set(get_args(RunState))
    assert SUCCESS_RUN_STATES < TERMINAL_RUN_STATES


def test_python_state_is_the_string_none_rather_than_an_absent_key():
    """A truthiness test on python_state reads backwards: the string "none" is truthy."""
    run = ReportRun.from_payload(REPORT_RUN)
    assert run.python_state == "none"
    assert run.python_state


def test_a_query_run_recovers_its_query_token_from_the_hal_link():
    run = QueryRun.from_payload(
        {"token": "qr1", "_links": {"query": {"href": "/api/acme/reports/r1/queries/q9"}}}
    )
    assert run.query_token == "q9"


def test_a_query_run_prefers_the_top_level_query_token_over_hal_parsing():
    """Mode hands the token over directly; the HAL link is only the fallback."""
    run = QueryRun.from_payload(QUERY_RUN)
    assert run.query_token == "708192a3b4c5"
    assert "_links" not in run.raw


# --- the wire types, pinned -------------------------------------------------------------------
#
# An id field annotated `int | None` would hold a `str` at runtime, making `report.id == 2000003`
# silently False. A fixture using ints hides that, so these tests pin the wire types directly.


@pytest.mark.parametrize(("model", "name", "value"), STRING_ID_FIELDS)
def test_every_mode_id_is_declared_and_parsed_as_a_string(model, name, value):
    parsed = getattr(model.from_payload({name: value}), name)
    assert parsed == value
    assert isinstance(parsed, str)


@pytest.mark.parametrize(("model", "name", "value"), STRING_ID_FIELDS)
def test_an_id_annotation_says_str_so_a_type_checker_agrees_with_the_wire(model, name, value):
    assert get_type_hints(model)[name] == (str | None)


def test_ids_are_not_coerced_so_the_attribute_and_raw_never_disagree():
    """Only timestamps are coerced. Everything else is the object Mode sent, identically."""
    report = Report.from_payload(REPORT)
    assert report.id is report.raw["id"]
    assert report.account_id is report.raw["account_id"]
    assert report.id != 2000002


@pytest.mark.parametrize(("model", "gone", "replacement"), WIRE_KEY_SPELLINGS)
def test_a_field_mode_never_sends_is_not_declared(model, gone, replacement):
    """A field that can never be non-None is worse than no field: it reads as a real answer."""
    assert gone not in {f.name for f in dataclasses.fields(model)}
    if replacement is not None:
        assert replacement in {f.name for f in dataclasses.fields(model)}


def test_a_report_lifts_the_fields_that_were_only_reachable_through_raw():
    report = Report.from_payload(REPORT)
    assert (report.view_count, report.schedules_count, report.max_query_count) == (0, 0, 160)
    assert report.expected_runtime == 14.59947
    assert report.github_link.endswith("1a2b3c4d5e6f")


def test_last_run_at_is_the_start_and_last_successfully_run_at_is_the_finish():
    """Reading last_run_at as 'when did this finish' is off by the length of the run."""
    report = Report.from_payload(REPORT)
    assert report.last_run_at < report.last_successfully_run_at


def test_a_space_carries_no_timestamp_and_keeps_its_odd_keys_in_raw():
    space = Space.from_payload(SPACE)
    assert (space.id, space.schema_name, space.default_access_level) == (
        "2000001",
        "analytics",
        "view",
    )
    assert space.raw["viewable?"] is True  # not a legal identifier; raw is the only route
    assert not _datetime_field_names(Space)


def test_purge_state_is_two_timestamps_and_purged_is_derived_from_them():
    """Mode ships no `purged` key; results for a purged run answer 404 'run results not found'."""
    clean = ReportRun.from_payload(REPORT_RUN)
    assert clean.purge_started_at is None
    assert not clean.purged

    purged = ReportRun.from_payload(
        {
            "token": "c5d6e7f8091a",
            "state": "failed",
            "purge_started_at": "2026-05-27T07:33:01.514Z",
            "purge_completed_at": "2026-05-27T08:14:03.705Z",
        }
    )
    assert purged.purged
    assert purged.purge_completed_at == datetime(2026, 5, 27, 8, 14, 3, 705000, tzinfo=UTC)


def test_the_two_latest_run_flags_are_distinct_because_they_diverge_live():
    """A failed run can be the latest run without being the latest successful one."""
    run = ReportRun.from_payload(
        {"state": "failed", "is_latest_report_run": True, "is_latest_successful_report_run": False}
    )
    assert run.is_latest_report_run
    assert not run.is_latest_successful_report_run


def test_a_dataset_run_shares_the_report_run_purge_shape():
    run = DatasetRun.from_payload({"state": "succeeded", "purge_started_at": None})
    assert run.is_terminal
    assert not run.purged


def test_a_failed_query_run_exposes_the_error_code_not_just_the_constant_error_type():
    run = QueryRun.from_payload(QUERY_RUN)
    assert run.error_type == "generic"  # the same string on every failure Mode has produced
    assert run.error_code == "SqlUnknownFunction"
    assert run.data_source_token == "9f8e7d6c5b4a"


def test_a_schedule_hour_is_prose_and_the_number_lives_in_cron_hour():
    schedule = ReportSchedule.from_payload(REPORT_SCHEDULE)
    assert (schedule.hour, schedule.minute) == ("2:00 am", "the top of the hour")
    assert (schedule.cron_hour, schedule.cron_minute) == (2, 0)
    assert schedule.day_of_week == "Monday,Tuesday,Wednesday,Thursday,Friday"


def test_a_dataset_schedule_has_the_same_prose_problem():
    schedule = DatasetSchedule.from_payload(
        {"hour": "2:00 am", "cron_hour": 2, "minute": "the top of the hour", "cron_minute": 0}
    )
    assert isinstance(schedule.hour, str)
    assert schedule.cron_hour == 2


def test_link_token_is_none_when_the_relation_is_absent():
    assert link_token({}, "query") is None


def test_link_returns_the_href_itself():
    report = Report.from_payload({"_links": {"web_self": {"href": "/acme/reports/r1"}}})
    assert report.link("web_self") == "/acme/reports/r1"
    assert report.link("missing") is None


def test_an_audit_entry_reads_the_response_object_not_the_filter_names():
    """entity_* and ip/username are query params on the way in and nested on the way back."""
    entry = AuditLogEntry.from_payload(AUDIT_ENTRY)
    assert entry.entity_type == "workspace_connection"
    assert entry.entity_id == "091a2b3c4d5e"
    assert entry.entity_name == "Primary Warehouse"
    assert entry.username == "alice"
    assert entry.ip == "157.52.123.42"
    assert entry.workspace_username == "acme"
    assert entry.timestamp == datetime(2025, 2, 18, 10, 47, 37, tzinfo=UTC)


def test_an_audit_entry_with_no_nested_objects_answers_none_rather_than_raising():
    entry = AuditLogEntry.from_payload({"id": "a1"})
    assert (entry.entity_type, entry.username, entry.ip) == (None, None, None)


def test_a_chart_reads_its_type_and_title_out_of_view_vegas():
    """Mode returns neither at the top level; `view` is present but empty on every chart."""
    chart = Chart.from_payload(
        {
            "token": "c1",
            "view": {},
            "view_version": 2,
            "view_vegas": {"chartType": "line", "title": "Revenue by region"},
            "color_palette_token": "b4c5d6e7f809",
            "switch_view_token": "e7f8091a2b3c",
        }
    )
    assert chart.chart_type == "line"
    assert chart.chart_title == "Revenue by region"
    assert chart.color_palette_token == "b4c5d6e7f809"
    assert chart.view_version == 2


def test_a_chart_without_view_vegas_answers_none():
    assert Chart.from_payload({"token": "c1"}).chart_type is None


# ANN401 fires only on a parameter annotated bare `Any`, never on `dict[str, Any]` and never
# on a dataclass field, so the tests below are the enforcement ruff cannot give.
SOURCE = inspect.getsource(models)

ANY_BUDGET = frozenset(
    {
        "Model.raw",
        "Model.from_payload(payload)",
        "ReportRun.parameters",
        "Chart.view",
        "Chart.view_vegas",
        "ReportFilter.options",
        "FormField.options",
        "AuditLogEntry.context",
        "AuditLogEntry.actor",
        "AuditLogEntry.entity",
        "ReportSchedule.parameters",
        "DatasetSchedule.parameters",
    }
)


def _parameters(fn, prefix):
    for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]:
        if arg.annotation is not None:
            yield f"{prefix}{fn.name}({arg.arg})", ast.unparse(arg.annotation), arg.lineno


def _annotated_positions():
    """Every annotated field and parameter in models.py, as (name, annotation source, line)."""
    for node in ast.parse(SOURCE).body:
        if isinstance(node, ast.FunctionDef):
            yield from _parameters(node, "")
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    name = f"{node.name}.{member.target.id}"
                    yield name, ast.unparse(member.annotation), member.lineno
                if isinstance(member, ast.FunctionDef):
                    yield from _parameters(member, f"{node.name}.")


def test_no_model_field_says_any_outside_the_budget():
    """A blob field Mode does not schema is allowed `Any` -- but only after it is listed."""
    blobs = {n for n, source, _ in _annotated_positions() if "Any" in source and "(" not in n}
    assert blobs == ANY_BUDGET - {"Model.from_payload(payload)"}


def test_no_helper_takes_a_bare_any():
    """R6: the parse boundary takes a payload dict, not `Any` -- which is what ANN401 forbids."""
    assert not {n for n, source, _ in _annotated_positions() if "(" in n and source == "Any"}
