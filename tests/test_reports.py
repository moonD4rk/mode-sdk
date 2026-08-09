from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest
import respx

from mode_sdk import Mode
from mode_sdk import query_spec
from mode_sdk.errors import BadRequestError
from mode_sdk.errors import RunTimeoutError
from mode_sdk.models import DataSource
from mode_sdk.models import Report
from mode_sdk.models import ReportRun
from mode_sdk.models import Space
from mode_sdk.resources import reports as reports_module

API = "https://app.mode.com/api"


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


def sent(route) -> dict:
    return json.loads(route.calls[0].request.read())


def run(state: str) -> httpx.Response:
    return httpx.Response(200, json={"token": "run1", "state": state})


def creation_routes(name: str = "Revenue") -> tuple:
    """Mode has no create-report call: POST mints it unnamed, PATCH names and files it."""
    minted = respx.post(f"{API}/acme/reports").mock(
        return_value=httpx.Response(201, json={"token": "r1", "name": None})
    )
    named = respx.patch(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(202, json={"token": "r1", "name": name})
    )
    attached = respx.post(f"{API}/acme/reports/r1/queries").mock(
        return_value=httpx.Response(200, json={"token": "q1"})
    )
    fetched = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(200, json={"token": "r1", "name": name, "space_token": "sp1"})
    )
    return minted, named, attached, fetched


@respx.mock
def test_creating_a_report_mints_then_names_and_files_it():
    minted, named, attached, fetched = creation_routes()
    with client() as mode:
        report = mode.reports.create("sp1", "Revenue", [query_spec("select 1", 1234)])

    assert report.token == "r1"
    assert report.name == "Revenue"
    assert report.space_token == "sp1"
    assert minted.called and fetched.called
    assert sent(named) == {"report": {"name": "Revenue", "space_token": "sp1"}}
    assert sent(attached) == {
        "query": {"raw_query": "select 1", "data_source_id": 1234, "name": "Query 1"}
    }


@respx.mock
def test_creating_a_report_never_posts_to_the_collections_route():
    """That path answers 404 'request path not recognized' -- it is not a create route."""
    dead = respx.post(f"{API}/acme/collections/sp1/reports")
    creation_routes()
    with client() as mode:
        mode.reports.create("sp1", "Revenue", [query_spec("select 1", 1234)])
    assert not dead.called


@respx.mock
def test_an_over_long_report_name_is_refused_before_anything_is_minted():
    """Mode caps names at 64 and rejects longer ones at the PATCH, which is step two.

    Validating late would mint a report and immediately roll it back, so the caller would
    watch an "atomic" create leave nothing behind and report a server error for a mistake
    the SDK could see in the argument.
    """
    minted, named, _, _ = creation_routes()
    with client() as mode, pytest.raises(ValueError, match="64"):
        mode.reports.create("sp1", "x" * 65, [query_spec("select 1", 1)])
    assert not minted.called
    assert not named.called


@respx.mock
def test_a_name_at_the_limit_is_sent_unchanged():
    _, named, _, _ = creation_routes()
    with client() as mode:
        mode.reports.create("sp1", "x" * 64, [query_spec("select 1", 1)])
    assert sent(named)["report"]["name"] == "x" * 64


@respx.mock
def test_a_failure_after_minting_deletes_the_orphan_rather_than_stranding_it():
    """Step one leaves an unnamed report in the caller's personal Collection."""
    respx.post(f"{API}/acme/reports").mock(return_value=httpx.Response(201, json={"token": "r1"}))
    respx.patch(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(400, json={"id": "bad_request", "message": "nope"})
    )
    deleted = respx.delete(f"{API}/acme/reports/r1").mock(return_value=httpx.Response(204))
    with client() as mode, pytest.raises(BadRequestError):
        mode.reports.create("sp1", "Revenue", [query_spec("select 1", 1)])
    assert deleted.called


@respx.mock
def test_a_tokenless_space_is_refused_before_anything_is_minted():
    """``Space.from_payload({"name": ...})`` is a projection Mode really returns -- and the
    docstring on ``token_of`` calls passing the model the spelling that cannot go wrong.

    ``space`` used to be resolved inside the PATCH, one request after the POST that mints
    the report, and the ValueError it raises is not a ModeError, so the rollback did not
    fire either. The caller saw a validation error for a call they believed was atomic and
    never learned an unnamed report was now sitting in their personal Collection.
    """
    minted, _, _, _ = creation_routes()
    with client() as mode, pytest.raises(ValueError, match="space="):
        mode.reports.create(Space.from_payload({"name": "Analytics"}), "Revenue", [])
    assert not minted.called


@respx.mock
def test_an_empty_space_token_is_refused_before_anything_is_minted():
    minted, _, _, _ = creation_routes()
    with client() as mode, pytest.raises(ValueError, match="space="):
        mode.reports.create("", "Revenue", [query_spec("select 1", 1)])
    assert not minted.called


@respx.mock
def test_a_query_spec_missing_a_required_key_is_refused_before_anything_is_minted():
    """``spec["raw_query"]`` raises KeyError on a hand-built dict, three requests in and
    after the PATCH has already named and filed the report -- and KeyError escaped the
    rollback exactly as ValueError did.
    """
    minted, _, _, _ = creation_routes()
    with client() as mode, pytest.raises(ValueError, match=r"queries\[1\] is missing raw_query"):
        mode.reports.create("sp1", "Revenue", [query_spec("select 1", 1), {"data_source_id": 1}])
    assert not minted.called


@respx.mock
def test_a_local_failure_after_minting_still_deletes_the_orphan(monkeypatch):
    """The rollback catches Exception, not ModeError. What has to be undone is the mint,
    and the mint is equally minted whatever raised on the way to the final get().
    """
    creation_routes()
    deleted = respx.delete(f"{API}/acme/reports/r1").mock(return_value=httpx.Response(204))
    with client() as mode:
        monkeypatch.setattr(
            mode.reports,
            "get",
            lambda report: (_ for _ in ()).throw(RuntimeError("parsing blew up")),
        )
        with pytest.raises(RuntimeError, match="parsing blew up"):
            mode.reports.create("sp1", "Revenue", [query_spec("select 1", 1)])
    assert deleted.called


@respx.mock
def test_cloning_a_run_yields_a_report_not_a_run():
    respx.post(f"{API}/acme/reports/r1/runs/run1/clone").mock(
        return_value=httpx.Response(
            202, json={"token": "r2", "name": "Copy of Revenue", "type": "Report"}
        )
    )
    with client() as mode:
        cloned = mode.report_runs.clone("r1", "run1")
    assert isinstance(cloned, Report)
    assert cloned.name == "Copy of Revenue"


@respx.mock
def test_waiting_stops_on_completed():
    """A poller that only watches for 'succeeded' spins here until its deadline."""
    respx.get(f"{API}/acme/reports/r1/runs/run1").side_effect = [
        run("pending"),
        run("running_notebook"),
        run("completed"),
    ]
    with client() as mode:
        finished = mode.report_runs.wait("r1", "run1", interval=0)
    assert finished.state == "completed"
    assert finished.succeeded


@respx.mock
def test_waiting_stops_on_failed_without_pretending_it_worked():
    respx.get(f"{API}/acme/reports/r1/runs/run1").mock(return_value=run("failed"))
    with client() as mode:
        finished = mode.report_runs.wait("r1", "run1", interval=0)
    assert finished.is_terminal
    assert not finished.succeeded


@respx.mock
def test_a_run_that_never_finishes_raises_rather_than_reporting_a_fake_state():
    respx.get(f"{API}/acme/reports/r1/runs/run1").mock(return_value=run("enqueued"))
    with client() as mode, pytest.raises(RunTimeoutError) as caught:
        mode.report_runs.wait("r1", "run1", timeout=0, interval=0)
    assert caught.value.token == "run1"
    assert caught.value.state == "enqueued"


@respx.mock
def test_run_and_wait_creates_then_polls():
    created = respx.post(f"{API}/acme/reports/r1/runs").mock(return_value=run("pending"))
    polled = respx.get(f"{API}/acme/reports/r1/runs/run1").mock(return_value=run("succeeded"))
    with client() as mode:
        finished = mode.reports.run_and_wait("r1", parameters={"region": "apac"}, interval=0)
    assert finished.succeeded
    assert sent(created) == {"parameters": {"region": "apac"}}
    assert polled.called


@respx.mock
def test_failure_detail_reads_the_query_run_where_the_warehouse_error_lives():
    respx.get(f"{API}/acme/reports/r1/runs/run1/query_runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {
                    "query_runs": [
                        {"token": "qr1", "state": "failed", "error_message": "column ds required"}
                    ]
                }
            },
        )
    )
    with client() as mode:
        assert mode.report_runs.failure_detail("r1", "run1") == "column ds required"


@respx.mock
def test_failure_detail_says_so_when_it_cannot_read_the_query_runs():
    respx.get(f"{API}/acme/reports/r1/runs/run1/query_runs").mock(
        return_value=httpx.Response(404, json={"id": "not_found", "message": "gone"})
    )
    with client() as mode:
        assert "could not read query runs" in mode.report_runs.failure_detail("r1", "run1")


@respx.mock
def test_results_come_back_as_csv_text():
    respx.get(f"{API}/acme/reports/r1/runs/run1/results/content.csv").mock(
        return_value=httpx.Response(200, text="a,b\n1,2\n", headers={"content-type": "text/csv"})
    )
    with client() as mode:
        assert mode.report_runs.results("r1", "run1").text == "a,b\n1,2\n"


def zipped(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buffer.getvalue()


@respx.mock
def test_a_multi_query_result_is_returned_as_bytes_not_mangled_into_text():
    """Mode may answer with application/zip; decoding those bytes as text destroys them."""
    payload = zipped({"one.csv": "a\n1\n", "two.csv": "b\n2\n"})
    respx.get(f"{API}/acme/reports/r1/runs/run1/results/content.csv").mock(
        return_value=httpx.Response(
            200, content=payload, headers={"content-type": "application/zip"}
        )
    )
    with client() as mode:
        results = mode.report_runs.results("r1", "run1")

    assert results.is_archive
    assert results.content == payload


@respx.mock
def test_result_tables_unpack_the_archive_and_keep_one_csv_per_query():
    respx.get(f"{API}/acme/reports/r1/runs/run1/results/content.csv").mock(
        return_value=httpx.Response(
            200,
            content=zipped({"one.csv": "a\n1\n", "two.csv": "b\n2\n"}),
            headers={"content-type": "application/zip"},
        )
    )
    with client() as mode:
        assert mode.report_runs.results_tables("r1", "run1") == {
            "one.csv": "a\n1\n",
            "two.csv": "b\n2\n",
        }


@respx.mock
def test_result_tables_give_a_single_query_report_the_same_shape():
    respx.get(f"{API}/acme/reports/r1/runs/run1/results/content.csv").mock(
        return_value=httpx.Response(
            200,
            text="a,b\n1,2\n",
            headers={
                "content-type": "text/csv",
                "content-disposition": 'attachment; filename="revenue.csv"',
            },
        )
    )
    with client() as mode:
        assert mode.report_runs.results_tables("r1", "run1") == {"revenue.csv": "a,b\n1,2\n"}


@respx.mock
def test_a_hal_result_href_is_followed_without_doubling_the_api_prefix():
    route = respx.get(f"{API}/acme/reports/r1/runs/run1/query_runs/qr1/results/content.csv").mock(
        return_value=httpx.Response(200, text="ok")
    )
    with client() as mode:
        results = mode.exports.from_href("/api/acme/reports/r1/runs/run1/query_runs/qr1/results")
    assert results.text == "ok"
    assert route.called


@respx.mock
def test_listing_reports_needs_a_space_or_a_data_source():
    with client() as mode, pytest.raises(ValueError, match="Mode cannot list all reports"):
        mode.reports.list()


# --- typed keywords, model refs, and the parameters Mode punishes ---


# R1 -- "no public method takes untyped kwargs" -- lives in test_public_api.py, which walks
# every resource module rather than a hand-written tuple these six were once listed in.


@respx.mock
def test_a_run_listing_cannot_be_asked_to_filter():
    """``?filter=`` on this path is a reproducible 500, and 500 is retryable -- so the

    old ``**params`` passthrough turned one plausible guess into four requests and a
    backoff. There is no keyword to spell it with any more.
    """
    route = respx.get(f"{API}/acme/reports/r1/runs").mock(
        return_value=httpx.Response(200, json={"_embedded": {"report_runs": []}})
    )
    with client() as mode:
        with pytest.raises(TypeError):
            mode.report_runs.list("r1", filter="failed")
        mode.report_runs.list("r1")

    assert "filter" not in route.calls[0].request.url.params


@respx.mock
def test_a_report_may_be_passed_as_the_model_a_previous_call_returned():
    respx.get(f"{API}/acme/reports/r1/runs/run1").mock(return_value=run("succeeded"))
    report = Report.from_payload({"token": "r1"})
    with client() as mode:
        finished = mode.report_runs.get(report, ReportRun.from_payload({"token": "run1"}))
    assert finished.succeeded


def test_a_model_with_no_token_is_refused_before_any_request_is_sent():
    """Every token is ``str | None``, so callers used to write ``report.token or ""``."""
    with client() as mode, pytest.raises(ValueError, match="report= was passed a Report"):
        mode.reports.get(Report.from_payload({"name": "no token here"}))


# --- /data_sources/{token}/reports: one row per (report x query) ---


def data_source_pages(*pages: list[dict]):
    """Serve one page per ``page=`` value, then empty pages forever."""

    def respond(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params.get("page", 1)) - 1
        rows = pages[index] if index < len(pages) else []
        return httpx.Response(200, json={"_embedded": {"reports": rows}})

    return respx.get(f"{API}/acme/data_sources/ds_token/reports").mock(side_effect=respond)


@respx.mock
def test_a_data_source_listing_yields_each_report_once():
    """Mode returns one row per query bound to the source: 1200 rows for 170 reports."""
    route = data_source_pages(
        [{"token": "a"}] * 20 + [{"token": "b"}] * 10,
        [{"token": "b"}] * 5 + [{"token": "c"}],
    )
    with client() as mode:
        reports = mode.reports.list(data_source="ds_token").list()

    assert [r.token for r in reports] == ["a", "b", "c"]
    assert route.call_count == 3  # two pages of rows, then the empty one that ends it


@respx.mock
def test_one_report_filling_two_whole_pages_does_not_end_the_walk():
    """A report with 60 queries on one data source produces two consecutive identical
    pages. ``offset_pages`` reads that as circling and stops, silently dropping every
    report after it -- and Mode allows 160 queries on a report, so it is reachable.
    """
    data_source_pages(
        [{"token": "big"}] * 30,
        [{"token": "big"}] * 30,
        [{"token": "small"}],
    )
    with client() as mode:
        reports = mode.reports.list(data_source="ds_token").list()

    assert [r.token for r in reports] == ["big", "small"]


@respx.mock
def test_a_collection_that_ignores_page_still_terminates():
    """The circuit-breaker that replaces the fingerprint guard on this path."""
    route = respx.get(f"{API}/acme/data_sources/ds_token/reports").mock(
        return_value=httpx.Response(200, json={"_embedded": {"reports": [{"token": "a"}]}})
    )
    with client() as mode:
        reports = mode.reports.list(data_source="ds_token").list()

    assert [r.token for r in reports] == ["a"]
    assert route.call_count == 1 + reports_module._MAX_BARREN_PAGES


@respx.mock
def test_a_data_source_may_be_passed_as_a_model_and_travels_by_token():
    """``DataSource.id`` is the numeric id and 404s here; only the token routes."""
    route = data_source_pages([{"token": "a"}])
    source = DataSource.from_payload({"id": "10001", "token": "ds_token"})
    with client() as mode:
        mode.reports.list(data_source=source)
    assert route.called


def test_a_numeric_data_source_id_is_refused_with_the_reason():
    with client() as mode, pytest.raises(ValueError, match="numeric id"):
        mode.reports.list(data_source=10001)


def test_listing_reports_refuses_a_space_and_a_data_source_at_once():
    with client() as mode, pytest.raises(ValueError, match="not both"):
        mode.reports.list(space="sp1", data_source="ds_token")


# --- adaptive polling ---


def test_an_adaptive_schedule_grows_by_half_and_stops_at_the_ceiling():
    schedule = reports_module.poll_intervals(None)
    delays = [next(schedule) for _ in range(20)]
    unjittered = [2.0 * 1.5**n for n in range(20)]
    for delay, base in zip(delays, unjittered, strict=True):
        expected = min(base, reports_module.POLL_MAX_INTERVAL)
        assert 0.8 * expected <= delay <= 1.2 * expected
    assert delays[-1] <= reports_module.POLL_MAX_INTERVAL * 1.2


def test_an_explicit_interval_pins_the_cadence():
    schedule = reports_module.poll_intervals(5.0)
    assert [next(schedule) for _ in range(4)] == [5.0, 5.0, 5.0, 5.0]


def test_the_schedule_matches_the_poll_count_its_constants_are_justified_by():
    """The constants' comment quotes a poll count, and a wrong one is what a future
    retune would be argued from -- it claimed ~25 polls for 30 minutes where the schedule
    it describes spends ~65. Counted here without jitter, which only moves each wait by a
    fifth and the totals by less.
    """
    delay, elapsed, to_ceiling = reports_module.POLL_FIRST_INTERVAL, 0.0, 0
    while delay < reports_module.POLL_MAX_INTERVAL:
        elapsed += delay
        to_ceiling += 1
        delay = min(delay * reports_module.POLL_GROWTH, reports_module.POLL_MAX_INTERVAL)
    assert (to_ceiling, round(elapsed)) == (7, 64)

    def polls(budget: float) -> int:
        remaining = budget - elapsed
        return to_ceiling + int(-(-remaining // reports_module.POLL_MAX_INTERVAL))

    assert (polls(900), polls(1800)) == (35, 65)


@respx.mock
def test_polling_starts_quickly_and_backs_off_rather_than_holding_one_cadence(monkeypatch):
    """A trivial run finishes in seconds, which a fixed 5s cadence misses by a whole
    interval, while a fifteen-minute run would cost 180 polls at that cadence.
    """
    slept: list[float] = []
    monkeypatch.setattr(reports_module.time, "sleep", slept.append)
    respx.get(f"{API}/acme/reports/r1/runs/run1").side_effect = [
        run("enqueued"),
        run("enqueued"),
        run("enqueued"),
        run("succeeded"),
    ]
    with client() as mode:
        assert mode.report_runs.wait("r1", "run1").succeeded

    assert len(slept) == 3
    for delay, base in zip(slept, (2.0, 3.0, 4.5), strict=True):
        assert 0.8 * base <= delay <= 1.2 * base


@respx.mock
def test_create_and_wait_does_not_re_read_the_run_it_was_just_handed(monkeypatch):
    """``POST /runs`` answers with a run of its own -- always non-terminal, but a snapshot."""
    monkeypatch.setattr(reports_module.time, "sleep", lambda _: None)
    respx.post(f"{API}/acme/reports/r1/runs").mock(return_value=run("enqueued"))
    polled = respx.get(f"{API}/acme/reports/r1/runs/run1").mock(return_value=run("succeeded"))
    with client() as mode:
        assert mode.reports.run_and_wait("r1").succeeded
    assert polled.call_count == 1


@respx.mock
def test_a_poll_that_runs_out_of_time_hands_back_the_last_run_it_saw():
    respx.get(f"{API}/acme/reports/r1/runs/run1").mock(return_value=run("enqueued"))
    with client() as mode, pytest.raises(RunTimeoutError) as caught:
        mode.report_runs.wait("r1", "run1", timeout=0)

    assert caught.value.run is not None
    assert caught.value.run.state == "enqueued"
    assert caught.value.token == "run1"


# --- writes: wrappers, escape hatches, and the strip-None rule ---


@respx.mock
def test_a_space_model_may_stand_in_for_the_space_token_on_an_update():
    route = respx.patch(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(202, json={"token": "r1"})
    )
    with client() as mode:
        mode.reports.update("r1", space_token=Space.from_payload({"token": "sp1"}))
    assert sent(route) == {"report": {"space_token": "sp1"}}


@respx.mock
def test_extra_body_merges_inside_modes_wrapper_and_can_send_the_null_none_cannot():
    """``body()`` strips None so no write can clear a field; this is the way to mean it."""
    route = respx.patch(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(202, json={"token": "r1"})
    )
    with client() as mode:
        mode.reports.update("r1", name="N", extra_body={"description": None})
    assert sent(route) == {"report": {"name": "N", "description": None}}


@respx.mock
def test_a_filter_create_sends_the_six_fields_mode_refuses_to_default():
    route = respx.post(f"{API}/acme/reports/r1/filters").mock(
        return_value=httpx.Response(200, json={"token": "f1"})
    )
    with client() as mode:
        mode.report_filters.create(
            "r1",
            name="region",
            formula="[region]",
            data_type="STRING",
            filter_type="MULTI",
            control_type="DROPDOWN",
            variable_type="DISCRETE",
        )
    assert sent(route)["report_filter"] == {
        "name": "region",
        "formula": "[region]",
        "data_type": "STRING",
        "filter_type": "MULTI",
        "control_type": "DROPDOWN",
        "variable_type": "DISCRETE",
    }
