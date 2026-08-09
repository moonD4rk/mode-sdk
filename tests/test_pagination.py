"""How a Page spends requests, and the one guard that stops it spending them forever.

Several Mode collections ignore ``page=`` and answer every page number with their full
contents, so the duplicate-page guard is all that stands between them and a loop that
never ends. The tests pinning it are the ones to fix first if they go red.
"""

from __future__ import annotations

import logging
from itertools import islice

import httpx
import pytest
import respx

from mode_sdk import BadRequestError
from mode_sdk import Discovery
from mode_sdk import InternalServerError
from mode_sdk import Mode

API = "https://app.mode.com/api"
BATCH = "https://app.mode.com/batch"


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


def unretrying() -> Mode:
    """A client whose 500 is final, for the tests about what a Page does with one."""
    return Mode("acme", token="token", secret="secret", backoff_factor=0, max_retries=0)


def page(*tokens: str, key: str = "reports") -> httpx.Response:
    return httpx.Response(200, json={"_embedded": {key: [{"token": t} for t in tokens]}})


@respx.mock
def test_iteration_walks_every_page():
    route = respx.get(f"{API}/acme/spaces/s1/reports")
    route.side_effect = [page("r1", "r2"), page("r3"), page()]
    with client() as mode:
        reports = list(mode.reports.list(space="s1"))
    assert [r.token for r in reports] == ["r1", "r2", "r3"]
    assert route.call_count == 3


@respx.mock
def test_pages_yields_one_list_per_request():
    route = respx.get(f"{API}/acme/spaces/s1/reports")
    route.side_effect = [page("r1", "r2"), page("r3"), page()]
    with client() as mode:
        batches = [[r.token for r in batch] for batch in mode.reports.list(space="s1").pages()]
    assert batches == [["r1", "r2"], ["r3"]]


@respx.mock
def test_first_page_makes_exactly_one_request():
    route = respx.get(f"{API}/acme/spaces/s1/reports").mock(return_value=page("r1"))
    with client() as mode:
        first = mode.reports.list(space="s1").first_page()
    assert [r.token for r in first] == ["r1"]
    assert route.call_count == 1


@respx.mock
def test_the_first_page_is_fetched_before_anyone_iterates():
    """A listing is a value, not a suspended computation: the request is already paid."""
    route = respx.get(f"{API}/acme/spaces/s1/reports").mock(return_value=page("r1"))
    with client() as mode:
        reports = mode.reports.list(space="s1")
        assert route.call_count == 1
        assert [r.token for r in reports.first_page()] == ["r1"]
        assert route.call_count == 1


@respx.mock
def test_a_rejected_listing_raises_from_the_list_call_not_from_the_loop():
    """Sent through ``extra_params`` because ``spaces.list`` refuses an unknown ``filter``
    before the request leaves; this is about where a *server* rejection surfaces.
    """
    respx.get(f"{API}/acme/spaces").mock(
        return_value=httpx.Response(400, json={"id": "bad_request", "message": "unknown filter"})
    )
    with client() as mode, pytest.raises(BadRequestError):
        mode.spaces.list(extra_params={"filter": "does-not-exist"})


@respx.mock
def test_walking_a_page_twice_costs_one_walk():
    """side_effect holds exactly three responses, so a second walk that re-fetched would
    run out of them rather than quietly double the bill.
    """
    route = respx.get(f"{API}/acme/spaces/s1/reports")
    route.side_effect = [page("r1", "r2"), page("r3"), page()]
    with client() as mode:
        reports = mode.reports.list(space="s1")
        first = list(reports)
        walked = route.call_count
        second = list(reports)
    assert [r.token for r in first] == [r.token for r in second] == ["r1", "r2", "r3"]
    assert route.call_count == walked == 3
    assert first[0] is second[0]


@respx.mock
def test_a_partial_walk_resumes_where_it_stopped():
    route = respx.get(f"{API}/acme/spaces/s1/reports")
    route.side_effect = [page("r1"), page("r2"), page()]
    with client() as mode:
        reports = mode.reports.list(space="s1")
        assert [r.token for r in islice(reports, 1)] == ["r1"]
        assert route.call_count == 1
        assert [r.token for r in reports] == ["r1", "r2"]
    assert route.call_count == 3


@respx.mock
def test_a_walk_broken_by_a_500_stays_broken_instead_of_going_quiet():
    """A generator that raised is closed, so without the sticky failure the cached Page
    would answer later reads with a short collection that claims to be complete.
    """
    route = respx.get(f"{API}/acme/spaces/s1/reports")
    route.side_effect = [page("r1", "r2"), httpx.Response(500), page("r3")]
    with unretrying() as mode:
        reports = mode.reports.list(space="s1")
        with pytest.raises(InternalServerError):
            reports.list()
        with pytest.raises(InternalServerError):
            reports.list()
        with pytest.raises(InternalServerError):
            list(reports.pages())
        assert "failed=InternalServerError" in repr(reports)
    assert route.call_count == 2


@respx.mock
def test_a_broken_walk_is_not_retried_behind_the_caller_s_back():
    """Re-driving an unwound generator would restart the walk at page one, so the second
    read re-raises rather than re-asking.
    """
    route = respx.get(f"{API}/acme/spaces/s1/reports")
    route.side_effect = [page("r1"), httpx.Response(500)]
    with unretrying() as mode:
        reports = mode.reports.list(space="s1")
        for _ in range(3):
            with pytest.raises(InternalServerError):
                reports.list()
        assert [r.token for r in reports.first_page()] == ["r1"]
    assert route.call_count == 2


@respx.mock
def test_first_page_replays_the_cache_after_the_walk_moved_on():
    route = respx.get(f"{API}/acme/spaces/s1/reports")
    route.side_effect = [page("r1"), page("r2"), page()]
    with client() as mode:
        reports = mode.reports.list(space="s1")
        assert [r.token for r in reports] == ["r1", "r2"]
        walked = route.call_count
        assert [r.token for r in reports.first_page()] == ["r1"]
    assert route.call_count == walked == 3


@respx.mock
def test_a_collection_that_ignores_page_cannot_loop_forever():
    """/data_sources, /definitions and /reports/{r}/queries answer every page number with
    the same rows. Delete the fingerprint guard and this call never returns.
    """
    everything = httpx.Response(
        200, json={"_embedded": {"data_sources": [{"token": f"ds{n}"} for n in range(1, 5)]}}
    )
    route = respx.get(f"{API}/acme/data_sources").mock(return_value=everything)
    with client() as mode:
        sources = list(mode.data_sources.list())
    assert [d.token for d in sources] == ["ds1", "ds2", "ds3", "ds4"]
    assert route.call_count == 2


@respx.mock
def test_per_page_does_not_relieve_the_guard():
    """A full-looking page proves nothing about whether another follows, because most
    collections ignore per_page too. Only the guard settles it.
    """
    everything = httpx.Response(
        200, json={"_embedded": {"spaces": [{"token": f"sp{n}"} for n in range(1, 5)]}}
    )
    route = respx.get(f"{API}/acme/spaces").mock(return_value=everything)
    with client() as mode:
        spaces = list(mode.spaces.list(per_page=4))
    assert [s.token for s in spaces] == ["sp1", "sp2", "sp3", "sp4"]
    assert route.call_count == 2


@respx.mock
def test_an_endpoint_that_ignores_page_terminates_instead_of_looping():
    route = respx.get(f"{API}/acme/definitions").mock(
        return_value=httpx.Response(
            200, json={"_embedded": {"definitions": [{"token": "d1"}, {"token": "d2"}]}}
        )
    )
    with client() as mode:
        definitions = list(mode.definitions.list())
    assert [d.token for d in definitions] == ["d1", "d2"]
    assert route.call_count == 2


def counted(count: int, *, page: int, total_pages: int, total_count: int) -> httpx.Response:
    """/spaces as Mode answers it: 30-item pages and a full `pagination` block. A per_page
    above 30 comes back as 30 items with `per_page: 30` echoed.
    """
    start = (page - 1) * 30
    body: dict = {
        "_embedded": {"spaces": [{"token": f"sp{n}"} for n in range(start, start + count)]},
        "pagination": {
            "page": page,
            "per_page": 30,
            "count": count,
            "total_pages": total_pages,
            "total_count": total_count,
        },
        "_links": {"self": {"href": "/api/acme/spaces"}},
    }
    if page < total_pages:
        body["_links"]["next_page"] = {"href": f"/api/acme/spaces?page={page + 1}"}
    return httpx.Response(200, json=body)


@respx.mock
def test_a_per_page_above_the_cap_still_yields_the_whole_collection():
    """Mode caps per_page at 30 and several collections ignore it outright, so
    terminating on `len(items) < per_page` returns 30 of 42 items with no error and
    nothing for the caller to notice. That heuristic must not come back.
    """
    route = respx.get(f"{API}/acme/reports/r1/schedules")
    route.side_effect = [
        page(*(f"s{n}" for n in range(30)), key="report_schedules"),
        page(*(f"s{n}" for n in range(30, 42)), key="report_schedules"),
        page(key="report_schedules"),
    ]
    with client() as mode:
        schedules = mode.report_schedules.list("r1", per_page=50).list()
    assert len(schedules) == 42
    assert route.call_count == 3
    assert [call.request.url.params["per_page"] for call in route.calls] == ["50"] * 3


@respx.mock
def test_the_served_page_size_not_the_requested_one_drives_the_walk():
    """Asked per_page=50, Mode echoes per_page=30 and serves 30. The walk has to follow
    the server's arithmetic, not the caller's, or it stops 5 items short.
    """
    route = respx.get(f"{API}/acme/spaces")
    route.side_effect = [
        counted(30, page=1, total_pages=2, total_count=35),
        counted(5, page=2, total_pages=2, total_count=35),
    ]
    with client() as mode:
        spaces = mode.spaces.list(per_page=50)
        assert spaces.total_count == 35
        assert len(spaces.list()) == 35
    assert route.call_count == 2


@respx.mock
def test_the_envelope_ends_the_walk_without_paying_a_proving_request():
    """A collection that says "page 1 of 1" is believed. Everywhere else the walk pays one
    extra request to watch the fingerprint repeat -- here Mode's own counters replace it.
    """
    route = respx.get(f"{API}/acme/spaces").mock(
        return_value=counted(3, page=1, total_pages=1, total_count=3)
    )
    with client() as mode:
        spaces = mode.spaces.list()
        assert [s.token for s in spaces] == ["sp0", "sp1", "sp2"]
        assert spaces.has_more is False
        assert spaces.total_count == 3
    assert route.call_count == 1


@respx.mock
def test_total_count_is_none_where_the_envelope_states_none():
    """/data_sources carries no counters at all, and a length is not a total."""
    respx.get(f"{API}/acme/data_sources").mock(return_value=page("ds1", key="data_sources"))
    with client() as mode:
        assert mode.data_sources.list().total_count is None


@respx.mock
def test_a_clamped_per_page_is_visible_in_the_log(caplog):
    """The walk absorbs the clamp silently by design; debug logging is where "why did
    per_page=100 give me 30-item batches" gets answered.
    """
    respx.get(f"{API}/acme/spaces").mock(
        return_value=counted(30, page=1, total_pages=1, total_count=30)
    )
    with caplog.at_level(logging.DEBUG, logger="mode_sdk.pagination"), client() as mode:
        mode.spaces.list(per_page=100).list()
    assert "/spaces served per_page=30 for the requested 100" in caplog.text


@respx.mock
def test_page_number_increments():
    route = respx.get(f"{API}/acme/spaces/s1/reports")
    route.side_effect = [page("r1"), page("r2"), page()]
    with client() as mode:
        list(mode.reports.list(space="s1"))
    assert [call.request.url.params["page"] for call in route.calls] == ["1", "2", "3"]


@respx.mock
def test_items_are_found_even_when_the_embed_key_is_not_what_we_guessed():
    respx.get(f"{API}/acme/reports/r1/filters").mock(
        return_value=httpx.Response(200, json={"_embedded": {"filters": [{"token": "f1"}]}})
    )
    with client() as mode:
        assert [f.token for f in mode.report_filters.list("r1").first_page()] == ["f1"]


@respx.mock
def test_audit_logs_follow_the_next_token_cursor():
    route = respx.get(f"{API}/acme/audit_logs")
    route.side_effect = [
        httpx.Response(
            200, json={"_embedded": {"audit_logs": [{"id": "a1"}]}, "next_token": "cursor-2"}
        ),
        httpx.Response(200, json={"_embedded": {"audit_logs": [{"id": "a2"}]}}),
    ]
    with client() as mode:
        entries = list(mode.audit_logs.list("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z"))
    assert [e.id for e in entries] == ["a1", "a2"]
    assert route.calls[1].request.url.params["next_token"] == "cursor-2"


@respx.mock
def test_a_repeated_cursor_does_not_loop_forever():
    route = respx.get(f"{API}/acme/audit_logs").mock(
        return_value=httpx.Response(
            200, json={"_embedded": {"audit_logs": [{"id": "a1"}]}, "next_token": "same"}
        )
    )
    with client() as mode:
        entries = list(mode.audit_logs.list("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z"))
    assert [e.id for e in entries] == ["a1", "a1"]
    assert route.call_count == 2


@respx.mock
def test_discovery_follows_next_page_links():
    respx.get(f"{BATCH}/acme/reports", params={"page": "2"}).mock(
        return_value=httpx.Response(200, json={"reports": [{"token": "r2"}]})
    )
    respx.get(f"{BATCH}/acme/reports").mock(
        return_value=httpx.Response(
            200,
            json={
                "reports": [{"token": "r1"}],
                "_links": {"next_page": {"href": "/batch/acme/reports?page=2"}},
            },
        )
    )
    with Discovery("acme", token="t", access_key="k", access_secret="s") as discovery:
        reports = list(discovery.reports())
    assert [r.token for r in reports] == ["r1", "r2"]


@respx.mock
@pytest.mark.parametrize(
    ("envelope", "expected"),
    [
        ({"pagination": {"page": 1, "per_page": 20, "total_pages": 43}}, True),
        ({"pagination": {"page": 43, "per_page": 20, "total_pages": 43}}, False),
        ({"_links": {"next_page": {"href": "/api/acme/reports/r1/runs?page=2"}}}, True),
        ({"_links": {"self": {"href": "/api/acme/reports/r1/runs"}}}, None),
        (
            {
                "pagination": {"page": 1, "per_page": 20, "count": 1, "total_pages": 1},
                "_links": {"next_page": {"href": "/api/1234567/groups.acme?page=2"}},
            },
            False,
        ),
    ],
)
def test_has_more_repeats_the_envelope_and_never_improves_on_it(envelope, expected):
    """A `pagination` block always carries the same five keys, but only some collections
    carry one at all. For the rest an absent next link means "not said", not "no more",
    so None is the honest answer.
    """
    respx.get(f"{API}/acme/reports/r1/runs").mock(
        return_value=httpx.Response(
            200, json={"_embedded": {"report_runs": [{"token": "run1"}]}} | envelope
        )
    )
    with client() as mode:
        assert mode.report_runs.list("r1").has_more is expected


@respx.mock
def test_mode_own_counters_outrank_a_next_page_link_that_contradicts_them():
    """Mode emits next_page unconditionally, past the end of a drained collection and on
    to hrefs that 404 if followed, so where both are present the counters are the
    statement and the link is noise.
    """
    route = respx.get(f"{API}/acme/groups").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {"groups": [{"token": "g1"}]},
                "pagination": {
                    "page": 1,
                    "per_page": 20,
                    "count": 1,
                    "total_pages": 1,
                    "total_count": 1,
                },
                "_links": {"next_page": {"href": "/api/1234567/groups.acme?page=2"}},
            },
        )
    )
    with client() as mode:
        groups = mode.groups.list()
        assert [g.token for g in groups.list()] == ["g1"]
        assert groups.has_more is False
        assert groups.total_count == 1
        assert "has_more=False" in repr(groups)
    assert route.call_count == 1


@respx.mock
def test_has_more_tracks_the_cursor_as_the_walk_advances():
    route = respx.get(f"{API}/acme/audit_logs")
    route.side_effect = [
        httpx.Response(
            200, json={"_embedded": {"audit_logs": [{"id": "a1"}]}, "next_token": "cursor-2"}
        ),
        httpx.Response(200, json={"_embedded": {"audit_logs": [{"id": "a2"}]}}),
    ]
    with client() as mode:
        entries = mode.audit_logs.list("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z")
        assert entries.has_more is True
        assert [e.id for e in entries] == ["a1", "a2"]
        assert entries.has_more is False


@respx.mock
def test_discovery_sends_the_signature_bearer_header():
    route = respx.get(f"{BATCH}/acme/members").mock(
        return_value=httpx.Response(200, json={"members": []})
    )
    with Discovery("acme", token="t", access_key="k", access_secret="s") as discovery:
        list(discovery.members())
    assert route.calls[0].request.headers["authorization"] == "Bearer dDprOnM="
