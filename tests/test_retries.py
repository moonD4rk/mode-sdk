"""The retry loop as a caller experiences it: how many requests actually go out.

_policy already proves the rules; these prove the loop obeys them, sleeps for the
right hint, and never lets an httpx exception out.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from mode_sdk import Mode
from mode_sdk.errors import ConflictError
from mode_sdk.errors import InternalServerError
from mode_sdk.errors import ModeConnectionError
from mode_sdk.errors import ModeError
from mode_sdk.errors import ModeTimeoutError
from mode_sdk.errors import RateLimitError
from mode_sdk.errors import UnprocessableEntityError

API = "https://app.mode.com/api"


def client(**kwargs) -> Mode:
    kwargs.setdefault("backoff_factor", 0)
    return Mode("acme", token="token", secret="secret", **kwargs)


def http_date(offset: float) -> str:
    return format_datetime(datetime.now(UTC) + timedelta(seconds=offset))


@respx.mock
def test_a_post_is_retried_when_the_connection_never_happened():
    """DNS failed, so no run was started and no warehouse was billed."""
    route = respx.post(f"{API}/acme/reports/r1/runs").mock(side_effect=httpx.ConnectError("dns"))
    with client(max_retries=2) as mode, pytest.raises(ModeConnectionError):
        mode.report_runs.create("r1")
    assert route.call_count == 3


@respx.mock
def test_a_post_is_not_retried_once_the_request_was_on_the_wire():
    """The run may already be running; a replay would start a second one."""
    route = respx.post(f"{API}/acme/reports/r1/runs").mock(side_effect=httpx.ReadTimeout("slow"))
    with client(max_retries=2) as mode, pytest.raises(ModeTimeoutError):
        mode.report_runs.create("r1")
    assert route.call_count == 1


@respx.mock
def test_a_get_is_retried_on_a_read_timeout():
    route = respx.get(f"{API}/acme/reports/r1").mock(side_effect=httpx.ReadTimeout("slow"))
    with client(max_retries=2) as mode, pytest.raises(ModeTimeoutError):
        mode.reports.get("r1")
    assert route.call_count == 3


@respx.mock
def test_a_pool_timeout_is_a_connect_phase_failure_and_reaches_a_post():
    route = respx.post(f"{API}/acme/reports/r1/runs").mock(side_effect=httpx.PoolTimeout("busy"))
    with client(max_retries=1) as mode, pytest.raises(ModeTimeoutError):
        mode.report_runs.create("r1")
    assert route.call_count == 2


@respx.mock
def test_408_is_retried_now_that_it_is_in_the_list():
    route = respx.get(f"{API}/acme/reports/r1")
    route.side_effect = [httpx.Response(408), httpx.Response(200, json={"token": "r1"})]
    with client() as mode:
        assert mode.reports.get("r1").token == "r1"
    assert route.call_count == 2


@respx.mock
def test_409_is_left_alone_because_a_conflict_does_not_resolve_by_replaying():
    route = respx.patch(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(409, json={"message": "already moved"})
    )
    with client(max_retries=3) as mode, pytest.raises(ConflictError):
        mode.reports.update("r1", name="x")
    assert route.call_count == 1


@respx.mock
def test_422_is_a_validation_failure_not_something_to_repeat():
    route = respx.patch(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(422, json={"message": "name too long"})
    )
    with client(max_retries=3) as mode, pytest.raises(UnprocessableEntityError):
        mode.reports.update("r1", name="x")
    assert route.call_count == 1


@respx.mock
def test_a_retry_after_date_is_honoured_like_a_number():
    route = respx.get(f"{API}/acme/reports/r1")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": http_date(-1)}),
        httpx.Response(200, json={"token": "r1"}),
    ]
    with client() as mode:
        assert mode.reports.get("r1").token == "r1"
    assert route.call_count == 2


@respx.mock
def test_a_retry_after_above_the_cap_stops_the_loop_instead_of_retrying_early():
    """Sleeping 30s when the server asked for 120 is the opposite of polite."""
    route = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"}, json={"message": "slow"})
    )
    with client(max_retries=3) as mode, pytest.raises(RateLimitError) as caught:
        mode.reports.get("r1")
    assert route.call_count == 1
    assert caught.value.retry_after == 120.0


@respx.mock
def test_the_cap_applies_to_the_date_form_too():
    route = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(429, headers={"Retry-After": http_date(600)})
    )
    with client(max_retries=3) as mode, pytest.raises(RateLimitError) as caught:
        mode.reports.get("r1")
    assert route.call_count == 1
    assert caught.value.retry_after is not None and caught.value.retry_after > 60


@respx.mock
def test_an_over_cap_5xx_bails_out_under_its_own_name_with_the_hint_still_readable():
    route = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(503, headers={"Retry-After": "900"})
    )
    with client(max_retries=3) as mode, pytest.raises(InternalServerError) as caught:
        mode.reports.get("r1")
    assert route.call_count == 1
    assert caught.value.response is not None
    assert caught.value.response.headers["retry-after"] == "900"


@respx.mock
def test_a_garbage_retry_after_falls_back_to_backoff_rather_than_giving_up():
    route = respx.get(f"{API}/acme/reports/r1")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "whenever"}),
        httpx.Response(200, json={"token": "r1"}),
    ]
    with client() as mode:
        assert mode.reports.get("r1").token == "r1"
    assert route.call_count == 2


@respx.mock
def test_no_httpx_exception_escapes_and_the_original_stays_as_the_cause():
    respx.get(f"{API}/acme/reports/r1").mock(side_effect=httpx.ReadError("reset"))
    with client(max_retries=0) as mode, pytest.raises(ModeError) as caught:
        mode.reports.get("r1")
    assert not isinstance(caught.value, httpx.HTTPError)
    assert isinstance(caught.value.__cause__, httpx.ReadError)


@respx.mock
def test_a_redirect_loop_is_wrapped_too_even_though_it_is_no_transport_error():
    """follow_redirects is on for the signed-S3 result hop, so a loop is reachable here."""
    respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(302, headers={"Location": f"{API}/acme/reports/r1"})
    )
    with client(max_retries=3) as mode, pytest.raises(ModeConnectionError) as caught:
        mode.reports.get("r1")
    assert isinstance(caught.value.__cause__, httpx.TooManyRedirects)


@respx.mock
def test_a_stray_newline_in_a_token_is_a_mode_error_not_an_invalid_url():
    """Tokens come out of files and CSV columns, and those bring their line endings."""
    with client(max_retries=0) as mode, pytest.raises(ModeConnectionError) as caught:
        mode.reports.get("r1\n")
    assert isinstance(caught.value.__cause__, httpx.InvalidURL)


def test_a_body_that_lies_about_its_encoding_is_a_mode_error_as_well():
    """respx hands back an already-read body, so only a raw transport reaches the decoder."""

    def gzip_in_name_only(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=httpx.ByteStream(b'{"token": "r1"}'),
        )

    with httpx.Client(transport=httpx.MockTransport(gzip_in_name_only)) as http:
        with Mode("acme", token="t", secret="s", http_client=http) as mode:
            with pytest.raises(ModeConnectionError) as caught:
                mode.reports.get("r1")
    assert isinstance(caught.value.__cause__, httpx.DecodingError)


@respx.mock
def test_retry_false_pins_a_get_down():
    route = respx.get(f"{API}/acme/reports/r1").mock(return_value=httpx.Response(500))
    with client(max_retries=3) as mode, pytest.raises(InternalServerError):
        mode.transport.request("GET", "/reports/r1", retry=False)
    assert route.call_count == 1
