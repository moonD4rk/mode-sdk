"""Every write's body shape, in one table.

Mode wraps most write payloads in a resource-named object and answers an unwrapped
body with 400 ``param_missing``. Four methods once shipped without their wrapper, all
of them 100% broken and none of them covered by a test, so the shape of every write
is asserted here rather than trusted to the method that builds it.

The flat table is as load-bearing as the wrapped one: those four endpoints document a
flat body, and "wrap everything" would break them just as surely.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
import respx

from mode_sdk import Mode

API = "https://app.mode.com/api"


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


Call = Callable[[Mode], object]

WRAPPED: list[tuple[str, str, str, Call]] = [
    ("space", "POST", "/acme/spaces", lambda m: m.spaces.create("N")),
    ("space", "PATCH", "/acme/spaces/sp1", lambda m: m.spaces.update("sp1", name="N")),
    (
        "membership",
        "POST",
        "/acme/spaces/sp1/memberships",
        lambda m: m.space_memberships.add("sp1", "u1"),
    ),
    ("report", "PATCH", "/acme/reports/r1", lambda m: m.reports.update("r1", name="N")),
    (
        "report_filter",
        "POST",
        "/acme/reports/r1/filters",
        # Mode refuses a create missing any of these six: "Formula can't be blank, Data
        # type can't be blank, Filter type is not included in the list, ...".
        lambda m: m.report_filters.create(
            "r1",
            name="N",
            formula="[region]",
            data_type="STRING",
            filter_type="MULTI",
            control_type="DROPDOWN",
            variable_type="DISCRETE",
        ),
    ),
    (
        "report_filter",
        "PATCH",
        "/acme/reports/r1/filters/f1",
        lambda m: m.report_filters.update("r1", "f1", name="N"),
    ),
    (
        "query",
        "POST",
        "/acme/reports/r1/queries",
        lambda m: m.queries.create("r1", "select 1", 1),
    ),
    (
        "query",
        "PATCH",
        "/acme/reports/r1/queries/q1",
        lambda m: m.queries.update("r1", "q1", raw_query="select 2"),
    ),
    ("definition", "POST", "/acme/definitions", lambda m: m.definitions.create("N", "select 1")),
    (
        "definition",
        "PATCH",
        "/acme/definitions/d1",
        lambda m: m.definitions.update("d1", name="N"),
    ),
    (
        "data_source",
        "PATCH",
        "/acme/data_sources/ds1",
        lambda m: m.data_sources.update("ds1", name="N"),
    ),
    ("user_group", "POST", "/acme/groups", lambda m: m.groups.create("N")),
    ("user_group", "PATCH", "/acme/groups/g1", lambda m: m.groups.update("g1", "N")),
    (
        "membership",
        "POST",
        "/acme/groups/g1/memberships",
        lambda m: m.groups.add_member("g1", "u1"),
    ),
    ("invite", "POST", "/acme/invites", lambda m: m.invites.create("a@b.c", "hi")),
    (
        "report_schedule",
        "POST",
        "/acme/reports/r1/schedules",
        lambda m: m.report_schedules.create("r1", name="N"),
    ),
    (
        "report_schedule",
        "PATCH",
        "/acme/reports/r1/schedules/s1",
        lambda m: m.report_schedules.update("r1", "s1", name="N"),
    ),
    (
        "report_subscription",
        "POST",
        "/acme/reports/r1/subscriptions",
        lambda m: m.report_subscriptions.create("r1", csv_attachments_enabled=True),
    ),
    (
        "report_subscription",
        "PATCH",
        "/acme/reports/r1/subscriptions/s1",
        lambda m: m.report_subscriptions.update("r1", "s1", csv_attachments_enabled=True),
    ),
    ("report", "PATCH", "/acme/datasets/d1", lambda m: m.datasets.update("d1", name="N")),
    (
        "report",
        "POST",
        "/acme/reports/r1/runs",
        lambda m: m.datasets.refresh_in_report("r1", ["ds1"]),
    ),
]

FLAT: list[tuple[str, str, str, Call, set[str]]] = [
    (
        "report_runs.create",
        "POST",
        "/acme/reports/r1/runs",
        lambda m: m.report_runs.create("r1", parameters={"region": "apac"}),
        {"parameters"},
    ),
    (
        "reports.purge",
        "POST",
        "/acme/reports/purge",
        lambda m: m.reports.purge("2020-01-01"),
        {"time"},
    ),
    (
        "dataset_fields.create",
        "POST",
        "/acme/datasets/d1/field_descriptions",
        lambda m: m.dataset_fields.create("d1", "Region", "<p>Kanto</p>"),
        {"name", "desc"},
    ),
    (
        "dataset_fields.update",
        "PATCH",
        "/acme/datasets/d1/field_descriptions/fd1",
        lambda m: m.dataset_fields.update("d1", "fd1", "<p>Kanto</p>"),
        {"desc"},
    ),
]


@pytest.mark.parametrize(
    ("wrapper", "method", "path", "call"),
    WRAPPED,
    ids=[f"{w}:{m}{p}" for w, m, p, _ in WRAPPED],
)
@respx.mock
def test_a_wrapped_write_sends_exactly_one_top_level_key(wrapper, method, path, call):
    route = respx.route(method=method, url=f"{API}{path}").mock(
        return_value=httpx.Response(200, json={"token": "x"})
    )
    with client() as mode:
        call(mode)

    body = json.loads(route.calls[0].request.read())
    assert list(body) == [wrapper], f"expected a lone {wrapper!r} wrapper, got {list(body)}"
    assert isinstance(body[wrapper], dict) and body[wrapper]


@pytest.mark.parametrize(
    ("label", "method", "path", "call", "keys"),
    FLAT,
    ids=[label for label, *_ in FLAT],
)
@respx.mock
def test_a_flat_write_stays_flat(label, method, path, call, keys):
    """These four document a flat body; wrapping them would break them."""
    route = respx.route(method=method, url=f"{API}{path}").mock(
        return_value=httpx.Response(200, json={"token": "x"})
    )
    with client() as mode:
        call(mode)

    assert set(json.loads(route.calls[0].request.read())) == keys


@respx.mock
def test_none_is_stripped_rather_than_sent_as_null():
    route = respx.patch(f"{API}/acme/data_sources/ds1").mock(
        return_value=httpx.Response(200, json={"token": "ds1"})
    )
    with client() as mode:
        mode.data_sources.update("ds1", name="N", description=None)

    assert json.loads(route.calls[0].request.read()) == {"data_source": {"name": "N"}}
