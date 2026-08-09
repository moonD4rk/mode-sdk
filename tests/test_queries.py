"""Queries, charts and query runs: the three collections nested under a report.

The paths matter more than usual here because Mode does not validate them. A query or
chart is resolved by its own token and the enclosing report is ignored, so a wrongly
assembled path answers 200 with somebody else's data. These tests pin the assembly.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mode_sdk import Mode
from mode_sdk.models import Chart
from mode_sdk.models import Query
from mode_sdk.models import QueryRun
from mode_sdk.models import Report
from mode_sdk.models import ReportRun

API = "https://app.mode.com/api"


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


def sent(route) -> dict:
    return json.loads(route.calls[0].request.read())


def collection(key: str, *items: dict) -> httpx.Response:
    return httpx.Response(200, json={"_embedded": {key: list(items)}})


@respx.mock
def test_a_query_is_created_with_the_integer_mode_declares_in_its_own_form():
    route = respx.post(f"{API}/acme/reports/r1/queries").mock(
        return_value=httpx.Response(200, json={"token": "q1"})
    )
    with client() as mode:
        mode.queries.create("r1", "select 1", 10001, name="Orders")

    assert sent(route) == {
        "query": {"raw_query": "select 1", "data_source_id": 10001, "name": "Orders"}
    }


@respx.mock
def test_a_query_is_created_just_as_happily_with_the_string_mode_reads_back():
    """Mode reads the id as int or str and always writes it back as str.

    So ``queries.create(report, sql, other.data_source_id)`` -- the id straight off a
    model -- has to work without a cast. Verified live: both spellings bound the query to
    data source 10001 and both read back as ``"10001"``.
    """
    route = respx.post(f"{API}/acme/reports/r1/queries").mock(
        return_value=httpx.Response(200, json={"token": "q1"})
    )
    existing = Query.from_payload({"token": "q0", "data_source_id": "10001"})
    with client() as mode:
        mode.queries.create("r1", "select 1", existing.data_source_id or "")

    assert sent(route)["query"]["data_source_id"] == "10001"


@respx.mock
def test_an_update_sends_only_the_fields_it_was_given():
    """``body()`` strips None, so an update never clears a field by accident."""
    route = respx.patch(f"{API}/acme/reports/r1/queries/q1").mock(
        return_value=httpx.Response(200, json={"token": "q1"})
    )
    with client() as mode:
        mode.queries.update("r1", "q1", raw_query="select 2")

    assert sent(route) == {"query": {"raw_query": "select 2"}}


@respx.mock
def test_extra_body_reaches_inside_the_query_wrapper():
    route = respx.patch(f"{API}/acme/reports/r1/queries/q1").mock(
        return_value=httpx.Response(200, json={"token": "q1"})
    )
    with client() as mode:
        mode.queries.update("r1", "q1", name="N", extra_body={"dbt_metric_id": None})

    assert sent(route) == {"query": {"name": "N", "dbt_metric_id": None}}


@respx.mock
def test_models_stand_in_for_tokens_everywhere_a_path_segment_is_built():
    """A wrong segment here is not an error -- Mode answers 200 with the wrong data."""
    charts = respx.get(f"{API}/acme/reports/r1/queries/q1/charts").mock(
        return_value=collection("charts", {"token": "c1"})
    )
    chart = respx.get(f"{API}/acme/reports/r1/queries/q1/charts/c1").mock(
        return_value=httpx.Response(200, json={"token": "c1"})
    )
    query_runs = respx.get(f"{API}/acme/reports/r1/runs/run1/query_runs").mock(
        return_value=collection("query_runs", {"token": "qr1"})
    )
    report = Report.from_payload({"token": "r1"})
    with client() as mode:
        assert [c.token for c in mode.charts.list(report, Query.from_payload({"token": "q1"}))] == [
            "c1"
        ]
        mode.charts.get(report, "q1", Chart.from_payload({"token": "c1"}))
        assert [
            qr.token
            for qr in mode.query_runs.list(report, ReportRun.from_payload({"token": "run1"}))
        ] == ["qr1"]

    assert charts.called and chart.called and query_runs.called


def test_a_query_model_carrying_no_token_is_refused_by_name():
    with client() as mode, pytest.raises(ValueError, match="query= was passed a Query"):
        mode.queries.get("r1", Query.from_payload({"name": "unsaved"}))


@respx.mock
def test_a_query_runs_results_stay_text_because_one_query_is_one_table():
    """Only the report-level export can arrive as an archive; this one never does."""
    respx.get(f"{API}/acme/reports/r1/runs/run1/query_runs/qr1/results/content.csv").mock(
        return_value=httpx.Response(200, text="a,b\n1,2\n", headers={"content-type": "text/csv"})
    )
    with client() as mode:
        results = mode.query_runs.results("r1", "run1", QueryRun.from_payload({"token": "qr1"}))

    assert results == "a,b\n1,2\n"


@respx.mock
def test_an_unpaginated_collection_costs_exactly_one_proving_request():
    """Mode returns every query for every ``page`` and ``per_page``, so the duplicate
    guard is the terminator: the walk asks twice and stops, and the second request is not
    avoidable from here.
    """
    route = respx.get(f"{API}/acme/reports/r1/queries").mock(
        return_value=collection("queries", {"token": "q1"}, {"token": "q2"})
    )
    with client() as mode:
        assert [q.token for q in mode.queries.list("r1")] == ["q1", "q2"]

    assert route.call_count == 2
