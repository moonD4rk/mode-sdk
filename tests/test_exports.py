"""What comes back from an export, and what a caller is allowed to assume about it.

Mode picks the shape -- the same path answers ``text/csv`` for one report and
``application/zip`` for another, and query count does not predict which -- so every test
here drives the branch off the Content-Type, exactly as the code must.

Two export paths are traps rather than shapes and are covered too: a report-level
``content.json`` that returns only the first query's rows, and a PDF "download" that is
really the second half of an asynchronous render.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile

import httpx
import pytest
import respx

from mode_sdk import Mode
from mode_sdk import PdfExport
from mode_sdk import Report
from mode_sdk import ReportRun
from mode_sdk import RunTimeoutError
from mode_sdk.resources.distribution import RunResults

API = "https://app.mode.com/api"
RESULTS = f"{API}/acme/reports/r1/runs/run1/results/content.csv"
PDF_JOB = f"{API}/acme/reports/r1/exports/runs/run1/pdf.pdf"
PDF_DOWNLOAD = f"{API}/acme/reports/r1/exports/runs/run1/pdf/download.pdf"


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


def sent(route) -> dict:
    return json.loads(route.calls[0].request.read())


def zipped(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def archive_response(members: dict[str, str], filename: str = "export.zip") -> httpx.Response:
    return httpx.Response(
        200,
        content=zipped(members),
        headers={
            "content-type": "application/zip",
            "content-disposition": f'attachment; filename="{filename}"',
        },
    )


def csv_response(text: str = "a,b\n1,2\n", filename: str | None = "revenue.csv") -> httpx.Response:
    headers = {"content-type": "text/csv; charset=utf-8"}
    if filename is not None:
        headers["content-disposition"] = f'attachment; filename="{filename}"'
    return httpx.Response(200, text=text, headers=headers)


@respx.mock
def test_an_archive_export_says_so_and_refuses_to_pose_as_text():
    respx.get(RESULTS).mock(return_value=archive_response({"one.csv": "a\n1\n"}))
    with client() as mode:
        results = mode.exports.report_run("r1", "run1")

    assert results.is_archive
    assert results.media_type == "application/zip"
    assert results.content.startswith(b"PK\x03\x04")
    with pytest.raises(ValueError, match="not text"):
        _ = results.text


@respx.mock
def test_an_archive_unpacks_into_one_csv_per_member():
    respx.get(RESULTS).mock(
        return_value=archive_response({"one.csv": "a\n1\n", "two.csv": "b\n2\n"})
    )
    with client() as mode:
        assert mode.exports.report_run("r1", "run1").tables() == {
            "one.csv": "a\n1\n",
            "two.csv": "b\n2\n",
        }


@respx.mock
def test_a_csv_export_is_text_and_keeps_the_media_type_without_its_parameters():
    respx.get(RESULTS).mock(return_value=csv_response())
    with client() as mode:
        results = mode.exports.report_run("r1", "run1")

    assert not results.is_archive
    assert results.media_type == "text/csv"
    assert results.text == "a,b\n1,2\n"
    assert results.filename == "revenue.csv"


@respx.mock
def test_a_csv_export_tables_under_the_name_mode_sent_it_with():
    """Mode's filename carries the report title and the run timestamp, so it is parsed out
    of Content-Disposition rather than invented.
    """
    name = "monthly-revenue-2026-08-05-10-26-37.csv"
    respx.get(RESULTS).mock(return_value=csv_response(filename=name))
    with client() as mode:
        assert mode.exports.report_run("r1", "run1").tables() == {name: "a,b\n1,2\n"}


@respx.mock
def test_a_missing_content_disposition_leaves_the_filename_unknown():
    respx.get(RESULTS).mock(return_value=csv_response(filename=None))
    with client() as mode:
        results = mode.exports.report_run("r1", "run1")

    assert results.filename is None
    assert results.tables() == {"results": "a,b\n1,2\n"}


def test_a_byte_order_mark_does_not_survive_into_the_first_column_name():
    results = RunResults(b"\xef\xbb\xbfa,b\n1,2\n", "text/csv", None)
    assert results.text.startswith("a,b")


def test_the_repr_does_not_inline_the_export():
    results = RunResults(b"x" * 5000, "text/csv", "revenue.csv")
    assert repr(results) == "<RunResults text/csv revenue.csv 5000 bytes>"


@respx.mock
def test_save_writes_the_bytes_untouched(tmp_path):
    payload = zipped({"one.csv": "a\n1\n"})
    respx.get(RESULTS).mock(return_value=archive_response({"one.csv": "a\n1\n"}))
    with client() as mode:
        written = mode.exports.report_run("r1", "run1").save(tmp_path / "export.zip")

    assert written.read_bytes() == payload
    assert zipfile.ZipFile(written).namelist() == ["one.csv"]


@respx.mock
def test_saving_into_a_directory_keeps_modes_own_file_name(tmp_path):
    respx.get(RESULTS).mock(return_value=csv_response(filename="revenue-2026-08-05.csv"))
    with client() as mode:
        written = mode.exports.report_run("r1", "run1").save(tmp_path)

    assert written == tmp_path / "revenue-2026-08-05.csv"
    assert written.read_bytes() == b"a,b\n1,2\n"


def test_saving_a_nameless_export_into_a_directory_still_lands_somewhere(tmp_path):
    written = RunResults(b"a\n1\n", "text/csv", None).save(tmp_path)
    assert written == tmp_path / "results"


@respx.mock
def test_report_run_tables_is_sugar_for_one_request_not_two():
    route = respx.get(RESULTS).mock(return_value=csv_response())
    with client() as mode:
        assert mode.exports.report_run_tables("r1", "run1") == {"revenue.csv": "a,b\n1,2\n"}
    assert route.call_count == 1


@respx.mock
def test_a_hal_href_loses_the_api_prefix_base_url_already_carries():
    route = respx.get(f"{API}/acme/reports/r1/runs/run1/results/content.csv").mock(
        return_value=csv_response()
    )
    with client() as mode:
        results = mode.exports.from_href("/api/acme/reports/r1/runs/run1/results")

    assert route.called
    assert results.text == "a,b\n1,2\n"


@respx.mock
def test_a_href_without_the_api_prefix_is_left_alone():
    """``_forms.clone.action`` omits the prefix that ``edit`` and ``destroy`` carry."""
    route = respx.get(f"{API}/acme/reports/r1/runs/run1/results/content.csv").mock(
        return_value=csv_response()
    )
    with client() as mode:
        mode.exports.from_href("/acme/reports/r1/runs/run1/results")
    assert route.called


@respx.mock
def test_one_querys_export_stays_a_plain_string():
    """A single query is always text, so wrapping it in RunResults would be ceremony."""
    respx.get(f"{API}/acme/reports/r1/runs/run1/query_runs/qr1/results/content.csv").mock(
        return_value=csv_response()
    )
    with client() as mode:
        assert mode.query_runs.results("r1", "run1", "qr1") == "a,b\n1,2\n"


@respx.mock
def test_a_json_export_is_requested_by_extension():
    route = respx.get(f"{API}/acme/reports/r1/runs/run1/results/content.json").mock(
        return_value=httpx.Response(200, text="[]", headers={"content-type": "application/json"})
    )
    with client() as mode:
        assert mode.exports.report_run("r1", "run1", format="json").text == "[]"
    assert route.called


@respx.mock
def test_a_report_level_json_export_says_out_loud_that_it_drops_queries(caplog):
    """On a multi-query report ``content.json`` returns the first query alone -- no
    marker, no error, not even a Content-Disposition naming which query it was. Nothing in
    the response reveals the loss, so the SDK has to.
    """
    respx.get(f"{API}/acme/reports/r1/runs/run1/results/content.json").mock(
        return_value=httpx.Response(200, text="[]", headers={"content-type": "application/json"})
    )
    with caplog.at_level(logging.WARNING, logger="mode_sdk.exports"):
        with client() as mode:
            mode.exports.report_run("r1", "run1", format="json")

    assert "only the first query" in caplog.text
    assert "r1" in caplog.text


@respx.mock
def test_a_csv_export_says_nothing_because_it_loses_nothing(caplog):
    respx.get(RESULTS).mock(return_value=archive_response({"one.csv": "a\n1\n"}))
    with caplog.at_level(logging.WARNING, logger="mode_sdk.exports"):
        with client() as mode:
            mode.exports.report_run("r1", "run1")

    assert caplog.text == ""


@respx.mock
def test_extra_params_reach_a_schedule_listing_query_string():
    route = respx.get(f"{API}/acme/reports/r1/schedules").mock(
        return_value=httpx.Response(200, json={"_embedded": {"report_schedules": []}})
    )
    with client() as mode:
        mode.report_schedules.list("r1", per_page=25, extra_params={"state": "active"}).list()

    assert dict(route.calls[0].request.url.params) == {
        "state": "active",
        "per_page": "25",
        "page": "1",
    }


@respx.mock
def test_extra_body_merges_inside_the_wrapper_mode_requires():
    route = respx.post(f"{API}/acme/reports/r1/subscriptions").mock(
        return_value=httpx.Response(200, json={"token": "sub1"})
    )
    with client() as mode:
        mode.report_subscriptions.create(
            "r1", csv_attachments_enabled=True, extra_body={"recipient": "ops@acme.test"}
        )

    assert sent(route) == {
        "report_subscription": {"csv_attachments_enabled": True, "recipient": "ops@acme.test"}
    }


def test_text_refuses_a_pdf_as_firmly_as_it_refuses_a_zip():
    """``pdf()`` returns its bytes through this same type, so "not an archive" is not the
    same question as "safe to decode". Only text/* and JSON-ish media types answer.
    """
    results = RunResults(b"%PDF-1.4\n", "application/pdf", "report.pdf")

    assert not results.is_archive
    assert not results.is_text
    with pytest.raises(ValueError, match="not text"):
        _ = results.text


def test_a_missing_content_type_is_mode_declining_to_say_not_saying_binary():
    assert RunResults(b"a,b\n", "", None).is_text
    assert RunResults(b"a,b\n", "", None).text == "a,b\n"


@respx.mock
def test_a_report_and_a_run_can_be_passed_as_models_instead_of_tokens():
    route = respx.get(RESULTS).mock(return_value=csv_response())
    report = Report.from_payload({"token": "r1", "name": "Revenue"})
    run = ReportRun.from_payload({"token": "run1", "state": "succeeded"})
    with client() as mode:
        assert mode.exports.report_run(report, run).text == "a,b\n1,2\n"
    assert route.called


def test_a_run_that_lost_its_token_is_named_rather_than_requested():
    with client() as mode:
        with pytest.raises(ValueError, match="run= was passed a ReportRun"):
            mode.exports.report_run("r1", ReportRun.from_payload({"state": "succeeded"}))


def job(state: str, *, download: bool = False) -> httpx.Response:
    """The document ``GET .../pdf.pdf`` answers with -- a render job, never a PDF."""
    payload: dict = {
        "state": state,
        "created_at": None if state == "new" else "2026-08-07T15:29:26.888Z",
        "stale": False,
        "filename": "revenue-a3b4c5d6e7f8-2026-07-04.pdf" if download else None,
        "_links": {"self": {"href": "/api/acme/reports/r1/exports/runs/run1/pdf.pdf"}},
    }
    if download:
        payload["_links"]["download"] = {
            "href": "/api/acme/reports/r1/exports/runs/run1/pdf/download.pdf"
        }
    return httpx.Response(200, json=payload, headers={"content-type": "application/hal+json"})


def pdf_response() -> httpx.Response:
    return httpx.Response(
        200,
        content=b"%PDF-1.4\n%%EOF\n",
        headers={
            "content-type": "application/pdf",
            "content-disposition": 'attachment; filename="revenue-a3b4c5d6e7f8-2026-07-04.pdf"',
        },
    )


@respx.mock
def test_a_pdf_is_rendered_first_and_then_downloaded():
    """The download link answers ``404 export not yet generated`` for any run whose PDF was
    never rendered, which is the normal case. The first GET of pdf.pdf enqueues the render;
    only once it reaches ``completed`` does a ``download`` link exist.
    """
    route = respx.get(PDF_JOB).mock(
        side_effect=[job("requested"), job("requested"), job("completed", download=True)]
    )
    download = respx.get(PDF_DOWNLOAD).mock(return_value=pdf_response())
    with client() as mode:
        results = mode.exports.pdf("r1", "run1", interval=0)

    assert route.call_count == 3
    assert download.called
    assert results.media_type == "application/pdf"
    assert results.content.startswith(b"%PDF")
    assert results.filename == "revenue-a3b4c5d6e7f8-2026-07-04.pdf"


@respx.mock
def test_an_already_rendered_pdf_costs_one_extra_request_and_no_waiting():
    route = respx.get(PDF_JOB).mock(return_value=job("completed", download=True))
    respx.get(PDF_DOWNLOAD).mock(return_value=pdf_response())
    with client() as mode:
        mode.exports.pdf("r1", "run1", interval=0)

    assert route.call_count == 1


@respx.mock
def test_a_render_that_never_finishes_raises_with_the_state_it_was_last_in():
    respx.get(PDF_JOB).mock(return_value=job("requested"))
    with client() as mode:
        with pytest.raises(RunTimeoutError) as raised:
            mode.exports.pdf("r1", "run1", timeout=0, interval=0)

    assert raised.value.state == "requested"
    assert raised.value.token == "run1"


@respx.mock
def test_the_render_job_is_readable_on_its_own():
    """``state`` is a plain ``str``, so a fourth state Mode may add must not break parsing."""
    respx.get(PDF_JOB).mock(return_value=job("new"))
    with client() as mode:
        export = mode.exports.pdf_export("r1", "run1")

    assert isinstance(export, PdfExport)
    assert export.state == "new"
    assert export.stale is False
    assert export.download_href is None
    assert not export.is_ready


@respx.mock
def test_an_absolute_hal_href_is_followed_as_readily_as_a_relative_one():
    """Mode mixes both inside one document: a query run's results payload has a relative
    ``_links.self`` and absolute ``_links.csv`` / ``_links.json``.
    """
    route = respx.get(f"{API}/acme/reports/r1/runs/run1/query_runs/qr1/results/content.csv").mock(
        return_value=csv_response()
    )
    with client() as mode:
        results = mode.exports.from_href(
            "https://app.mode.com/api/acme/reports/r1/runs/run1/query_runs/qr1/results/content.csv"
        )

    assert route.called
    assert results.text == "a,b\n1,2\n"


@respx.mock
def test_an_href_that_already_names_a_content_file_is_not_given_a_second_one():
    """A report run's ``_links.content`` is already ``.../results/content.csv``; appending
    to it answers ``404 request path not recognized``.
    """
    route = respx.get(RESULTS).mock(return_value=csv_response())
    with client() as mode:
        mode.exports.from_href("/api/acme/reports/r1/runs/run1/results/content.csv")

    assert route.called


@respx.mock
def test_a_schedule_is_written_with_the_nested_cron_object_mode_declares():
    """Mode's own ``_forms.edit.input.report_schedule`` lists ``name``, a nested ``cron``
    of freq/hour/minute/time_zone/day_of_week/day_of_month, ``params`` and ``timeout``.
    """
    route = respx.post(f"{API}/acme/reports/r1/schedules").mock(
        return_value=httpx.Response(200, json={"token": "sch1"})
    )
    with client() as mode:
        mode.report_schedules.create(
            "r1",
            name="Daily",
            cron={"freq": "daily", "hour": 10, "minute": 0, "time_zone": "UTC"},
            timeout=3600,
        )

    assert sent(route) == {
        "report_schedule": {
            "name": "Daily",
            "cron": {"freq": "daily", "hour": 10, "minute": 0, "time_zone": "UTC"},
            "timeout": 3600,
        }
    }


@respx.mock
def test_subscriptions_of_one_schedule_take_the_nested_path():
    route = respx.get(f"{API}/acme/reports/r1/schedules/sch1/subscriptions").mock(
        return_value=httpx.Response(200, json={"_embedded": {"report_subscriptions": []}})
    )
    with client() as mode:
        mode.report_subscriptions.list("r1", schedule="sch1").first_page()
    assert route.called
