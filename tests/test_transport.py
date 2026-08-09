from __future__ import annotations

import httpx
import pytest
import respx

from mode_sdk import Discovery
from mode_sdk import Mode
from mode_sdk._transport import Transport
from mode_sdk.errors import AuthenticationError
from mode_sdk.errors import BadRequestError
from mode_sdk.errors import InternalServerError
from mode_sdk.errors import ModeAPIError
from mode_sdk.errors import ModeConnectionError
from mode_sdk.errors import NotFoundError
from mode_sdk.errors import PermissionDeniedError
from mode_sdk.errors import RateLimitError

API = "https://app.mode.com/api"


def client(**kwargs) -> Mode:
    kwargs.setdefault("backoff_factor", 0)
    return Mode("acme", token="token", secret="secret", **kwargs)


@pytest.mark.parametrize(
    ("status", "error_id", "expected"),
    [
        (400, "bad_request", BadRequestError),
        (401, "unauthorized", AuthenticationError),
        (403, "forbidden", PermissionDeniedError),
        (404, "not_found", NotFoundError),
    ],
)
@respx.mock
def test_documented_error_ids_map_to_named_exceptions(status, error_id, expected):
    respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(status, json={"id": error_id, "message": "nope"})
    )
    with client() as mode, pytest.raises(expected) as caught:
        mode.reports.get("r1")
    assert caught.value.error_id == error_id
    assert caught.value.message == "nope"
    assert caught.value.status_code == status


@respx.mock
def test_status_alone_is_enough_when_the_body_is_not_mode_json():
    respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(404, html="<html>gateway</html>")
    )
    with client() as mode, pytest.raises(NotFoundError) as caught:
        mode.reports.get("r1")
    assert caught.value.error_id is None
    assert "gateway" in caught.value.message


@respx.mock
def test_unknown_5xx_becomes_server_error_not_a_raw_httpx_error():
    respx.get(f"{API}/acme/reports/r1").mock(return_value=httpx.Response(503, text="down"))
    with client(max_retries=0) as mode, pytest.raises(InternalServerError) as caught:
        mode.reports.get("r1")
    assert isinstance(caught.value, ModeAPIError)


@respx.mock
def test_get_is_retried_until_max_retries_then_raises():
    route = respx.get(f"{API}/acme/reports/r1").mock(return_value=httpx.Response(500))
    with client(max_retries=3) as mode, pytest.raises(InternalServerError):
        mode.reports.get("r1")
    assert route.call_count == 4


@respx.mock
def test_post_is_never_retried_because_a_second_run_costs_warehouse_time():
    route = respx.post(f"{API}/acme/reports/r1/runs").mock(return_value=httpx.Response(500))
    with client(max_retries=3) as mode, pytest.raises(InternalServerError):
        mode.report_runs.create("r1")
    assert route.call_count == 1


@respx.mock
def test_a_caller_can_opt_a_post_into_retrying():
    route = respx.post(f"{API}/acme/reports/purge").mock(return_value=httpx.Response(500))
    with client(max_retries=2) as mode, pytest.raises(InternalServerError):
        mode.transport.request("POST", "/reports/purge", retry=True)
    assert route.call_count == 3


@respx.mock
def test_429_is_retried_and_honours_retry_after():
    route = respx.get(f"{API}/acme/spaces")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"_embedded": {"spaces": [{"token": "s1"}]}}),
        httpx.Response(200, json={"_embedded": {"spaces": []}}),
    ]
    with client() as mode:
        spaces = list(mode.spaces.list())
    assert [s.token for s in spaces] == ["s1"]
    assert route.call_count == 3


@respx.mock
def test_exhausted_429_surfaces_as_rate_limited_with_the_hint():
    respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "0"}, json={"message": "slow down"}
        )
    )
    with client(max_retries=0) as mode, pytest.raises(RateLimitError) as caught:
        mode.reports.get("r1")
    assert caught.value.retry_after == 0


@respx.mock
def test_connection_failures_are_retried_then_wrapped():
    route = respx.get(f"{API}/acme/reports/r1").mock(side_effect=httpx.ConnectError("boom"))
    with client(max_retries=2) as mode, pytest.raises(ModeConnectionError):
        mode.reports.get("r1")
    assert route.call_count == 3


@respx.mock
def test_empty_body_is_not_a_parse_error():
    respx.delete(f"{API}/acme/reports/r1").mock(return_value=httpx.Response(204))
    with client() as mode:
        assert mode.reports.delete("r1") is None


@respx.mock
def test_verify_skips_the_workspace_prefix_and_reports_the_key_scope():
    route = respx.get(f"{API}/verify").mock(
        return_value=httpx.Response(
            200,
            json={"workspace": "acme", "workspace_token": "w1", "api_key_scope": "member"},
        )
    )
    with client() as mode:
        checked = mode.verify()
    assert route.called
    assert (checked.workspace, checked.api_key_scope) == ("acme", "member")


@respx.mock
def test_workspace_root_has_no_trailing_slash():
    route = respx.get(f"{API}/acme").mock(return_value=httpx.Response(200, json={"id": 1}))
    with client() as mode:
        mode.workspace.get()
    assert route.called


@respx.mock
def test_request_escape_hatch_reaches_unmodelled_paths():
    respx.get(f"{API}/acme/something/new").mock(
        return_value=httpx.Response(200, json={"shape": "unknown"})
    )
    with client() as mode:
        assert mode.request("GET", "/something/new") == {"shape": "unknown"}


@respx.mock
def test_every_request_says_who_it_is():
    """Mode's support has nothing to correlate against an anonymous httpx default."""
    route = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(200, json={"token": "r1"})
    )
    with client() as mode:
        mode.reports.get("r1")
    agent = route.calls[0].request.headers["user-agent"]
    assert agent.startswith("mode-sdk/")
    assert " python/" in agent and " httpx/" in agent
    assert not [h for h in route.calls[0].request.headers if h.lower().startswith("x-")]


@respx.mock
def test_a_bodyless_request_does_not_claim_to_carry_json():
    route = respx.delete(f"{API}/acme/reports/r1").mock(return_value=httpx.Response(204))
    with client() as mode:
        mode.reports.delete("r1")
    assert "content-type" not in route.calls[0].request.headers


@respx.mock
def test_a_request_with_a_body_still_gets_its_content_type_from_httpx():
    route = respx.post(f"{API}/acme/reports/r1/runs").mock(
        return_value=httpx.Response(200, json={"token": "run1"})
    )
    with client() as mode:
        mode.report_runs.create("r1")
    assert route.calls[0].request.headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("timeout", 5.0),
        ("headers", {"X-Trace": "1"}),
        ("limits", httpx.Limits(max_connections=5)),
        ("http2", False),
        ("verify", False),
        ("proxy", "http://localhost:8080"),
    ],
)
def test_injecting_a_client_refuses_the_settings_it_would_silently_ignore(name, value):
    with httpx.Client() as http, pytest.raises(ValueError, match=f"{name}="):
        Transport("acme", "token", "secret", http_client=http, **{name: value})


@respx.mock
def test_an_injected_client_still_carries_the_sdks_credentials():
    route = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(200, json={"token": "r1"})
    )
    with httpx.Client() as http:
        with Mode("acme", token="t", secret="s", http_client=http) as mode:
            mode.reports.get("r1")
        assert route.calls[0].request.headers["authorization"] == "Basic dDpz"
        assert not http.is_closed


def test_a_client_the_sdk_did_not_open_is_not_a_client_it_may_close():
    http = httpx.Client()
    Mode("acme", token="t", secret="s", http_client=http).close()
    assert not http.is_closed
    http.close()


def test_the_pool_says_whether_it_is_still_open():
    mode = client()
    assert not mode.transport.is_closed
    mode.close()
    assert mode.transport.is_closed


def test_a_dead_host_fails_in_seconds_while_a_slow_export_keeps_its_budget():
    with client() as mode:
        assert mode.transport._http.timeout == httpx.Timeout(60.0, connect=5.0)


def test_a_bare_float_timeout_still_covers_every_phase():
    with client(timeout=12.0) as mode:
        assert mode.transport._http.timeout == httpx.Timeout(12.0)


def test_pooling_and_tls_are_tunable_without_building_a_whole_client():
    with Transport(
        "acme", "token", "secret", limits=httpx.Limits(max_connections=7), verify=False
    ) as transport:
        assert transport._http._transport._pool._max_connections == 7


@respx.mock
def test_default_headers_ride_every_request():
    route = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(200, json={"token": "r1"})
    )
    with client(default_headers={"X-Trace": "abc"}) as mode:
        mode.reports.get("r1")
    assert route.calls[0].request.headers["x-trace"] == "abc"


def wire_values(request: httpx.Request, name: str) -> list[str]:
    return [v.decode() for k, v in request.headers.raw if k.lower() == name.encode()]


@pytest.mark.parametrize("spelling", ["accept", "Accept", "ACCEPT"])
@respx.mock
def test_a_per_request_accept_replaces_the_sdks_rather_than_joining_it(spelling):
    """/audit_logs 406s the hal+json default before it looks at the credential, so a
    caller reaching for the escape hatch must be able to take that header off the wire
    -- and httpx is case-insensitive everywhere else, so the spelling cannot matter.
    """
    route = respx.get(f"{API}/acme/audit_logs").mock(return_value=httpx.Response(200, json={}))
    with client() as mode:
        mode.transport.request("GET", "/audit_logs", headers={spelling: "application/json"})
    assert wire_values(route.calls[0].request, "accept") == ["application/json"]


@pytest.mark.parametrize("spelling", ["user-agent", "User-Agent", "USER-AGENT"])
@respx.mock
def test_a_default_header_can_take_over_the_user_agent_without_sending_two(spelling):
    """User-Agent is a singleton field per RFC 9110 and some proxies reject a repeat."""
    route = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(200, json={"token": "r1"})
    )
    with client(default_headers={spelling: "myapp/1.0"}) as mode:
        mode.reports.get("r1")
    assert wire_values(route.calls[0].request, "user-agent") == ["myapp/1.0"]


@respx.mock
def test_a_two_hundred_that_is_not_json_is_an_api_error_not_a_decode_error():
    """An SSO interstitial in front of Mode answers 200 with a login page."""
    respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(200, html="<html>alice@example.com</html>")
    )
    with client() as mode, pytest.raises(ModeAPIError) as caught:
        mode.request("GET", "/reports/r1")
    assert caught.value.status_code == 200
    assert str(caught.value) == (
        "GET /acme/reports/r1 -> HTTP 200: expected a JSON body, got text/html"
    )
    assert caught.value.response is not None


def test_a_negative_backoff_factor_is_refused_before_it_can_reach_time_sleep():
    """The sleep is two requests into a failing call, long after the mistake was made."""
    with pytest.raises(ValueError, match="backoff_factor="):
        Mode("acme", token="t", secret="s", backoff_factor=-1.0)


@respx.mock
def test_discovery_keeps_its_bearer_when_the_caller_brings_their_own_client():
    """The credential is applied per request, so injection does not quietly drop it."""
    route = respx.get("https://app.mode.com/batch/acme/members").mock(
        return_value=httpx.Response(200, json={"members": []})
    )
    with httpx.Client() as http:
        with Discovery("acme", token="t", access_key="k", access_secret="s", http_client=http) as d:
            list(d.members())
    assert route.calls[0].request.headers["authorization"] == "Bearer dDprOnM="
    assert route.calls[0].request.headers["accept"] == "application/json"
