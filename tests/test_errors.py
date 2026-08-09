"""What a failure looks like by the time a caller sees it.

The taxonomy is a promise: catching ModeError catches everything, catching
ModeAPIError catches exactly "Mode answered", and picking a branch never means
reading a status code. The message is a second promise -- it names the call without
naming whoever the call was filtering on.
"""

from __future__ import annotations

import httpx
import pytest

from mode_sdk import _policy
from mode_sdk.errors import AuthenticationError
from mode_sdk.errors import BadRequestError
from mode_sdk.errors import ConflictError
from mode_sdk.errors import DiscoveryUnavailableError
from mode_sdk.errors import InternalServerError
from mode_sdk.errors import ModeAPIError
from mode_sdk.errors import ModeConnectionError
from mode_sdk.errors import ModeError
from mode_sdk.errors import ModeTimeoutError
from mode_sdk.errors import NotFoundError
from mode_sdk.errors import PermissionDeniedError
from mode_sdk.errors import RateLimitError
from mode_sdk.errors import RunTimeoutError
from mode_sdk.errors import UnprocessableEntityError
from mode_sdk.models import ReportRun

URL = "https://app.mode.com/api/acme/reports/r1"


def answered(status: int, *, url: str = URL, **kwargs) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", url), **kwargs)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, BadRequestError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (422, UnprocessableEntityError),
        (429, RateLimitError),
        (500, InternalServerError),
        (502, InternalServerError),
        (503, InternalServerError),
        (599, InternalServerError),
    ],
)
def test_every_mapped_status_gets_its_own_class(status, expected):
    error = _policy.error_from_response(answered(status, json={"message": "no"}))
    assert type(error) is expected
    assert error.status_code == status


@pytest.mark.parametrize("status", [402, 405, 418, 451])
def test_an_unrecognised_4xx_stays_a_bare_api_error_rather_than_being_misnamed(status):
    error = _policy.error_from_response(answered(status, json={"message": "no"}))
    assert type(error) is ModeAPIError


def test_modes_error_id_wins_over_the_numeric_status():
    """The id is the more precise of the two, and Mode sometimes disagrees with itself."""
    error = _policy.error_from_response(answered(400, json={"id": "not_found", "message": "gone"}))
    assert isinstance(error, NotFoundError)
    assert error.status_code == 400
    assert error.error_id == "not_found"


def test_a_json_body_is_parsed_and_kept():
    body = {"id": "bad_request", "message": "bad column", "details": {"column": "id"}}
    error = _policy.error_from_response(answered(400, json=body))
    assert error.body == body
    assert error.message == "bad column"


def test_an_html_body_maps_by_status_and_is_collapsed_into_the_message():
    page = "<html>\n  <body>   502 Bad   Gateway\n</body>\n</html>"
    error = _policy.error_from_response(answered(502, html=page))
    assert isinstance(error, InternalServerError)
    assert error.body is None
    assert error.message == "<html> <body> 502 Bad Gateway </body> </html>"


def test_a_long_non_json_body_is_truncated_rather_than_pasted_whole():
    error = _policy.error_from_response(answered(500, text="x " * 500))
    assert len(error.message) == 300


def test_an_empty_body_falls_back_to_the_reason_phrase():
    error = _policy.error_from_response(answered(404, text=""))
    assert error.message == "Not Found"
    assert error.body is None


def test_a_nested_validation_body_becomes_a_readable_message_not_a_dict():
    """Rails answers a 422 with the failures under "message"; ``.message`` is still a str."""
    body = {"message": {"name": ["is too long"]}}
    error = _policy.error_from_response(answered(422, json=body))
    assert error.message == '{"message":{"name":["is too long"]}}'
    assert error.body == body


def test_an_id_that_is_not_a_string_names_nothing_rather_than_lying_about_its_type():
    error = _policy.error_from_response(answered(400, json={"id": 42, "message": "no"}))
    assert error.error_id is None
    assert type(error) is BadRequestError


def test_a_non_dict_json_body_is_kept_but_names_nothing():
    error = _policy.error_from_response(answered(400, json=["nope"]))
    assert error.body == ["nope"]
    assert error.error_id is None


def test_the_rate_limit_hint_is_parsed_off_the_response():
    error = _policy.error_from_response(answered(429, headers={"Retry-After": "12"}))
    assert isinstance(error, RateLimitError)
    assert error.retry_after == 12.0


def test_a_429_without_a_hint_says_so_instead_of_guessing():
    assert _policy.error_from_response(answered(429)).retry_after is None


def test_a_success_whose_body_is_not_json_is_still_an_api_error():
    """A proxy's login page answers 200; JSONDecodeError is a ValueError, not a
    ModeError, and it carries the whole undecoded body on ``.doc``.
    """
    error = _policy.non_json_error(answered(200, html="<html>alice@example.com</html>"))
    assert type(error) is ModeAPIError
    assert error.status_code == 200
    assert error.message == "expected a JSON body, got text/html"
    assert "alice@example.com" not in str(error)
    assert error.response is not None


def test_a_body_with_no_content_type_says_that_rather_than_guessing():
    assert _policy.non_json_error(answered(200, content=b"nope")).message.endswith(
        "no content type"
    )


def test_the_message_names_the_call_that_failed():
    error = _policy.error_from_response(
        answered(404, json={"id": "not_found", "message": "Report not found"}),
        method="GET",
        path="/acme/reports/abc",
    )
    assert str(error) == "GET /acme/reports/abc -> HTTP 404 not_found: Report not found"


def test_the_message_never_carries_a_query_string_even_when_handed_one():
    """Audit-log filters ride in the query and carry usernames, emails and IPs."""
    filtered = "/acme/audit_logs?username=alice@example.com&ip=10.0.0.1"
    error = ModeAPIError(404, "nope", path=filtered)
    assert "alice@example.com" not in str(error)
    assert "?" not in str(error)
    assert error.path == "/acme/audit_logs"


def test_a_path_derived_from_the_response_is_query_free_too():
    error = _policy.error_from_response(answered(404, url=f"{URL}?username=alice@example.com"))
    assert "alice" not in str(error)
    assert error.path == "/api/acme/reports/r1"


def test_the_request_is_reachable_for_anyone_who_needs_the_whole_story():
    error = _policy.error_from_response(answered(404))
    assert error.request is not None
    assert error.request.method == "GET"


def test_an_error_built_without_a_response_has_no_request():
    assert ModeAPIError(201, "no token in the answer").request is None


def test_an_unbound_response_does_not_break_error_construction():
    error = _policy.error_from_response(httpx.Response(404, json={"message": "no"}))
    assert isinstance(error, NotFoundError)
    assert error.method is None
    assert str(error) == "HTTP 404: no"


@pytest.mark.parametrize(
    "error",
    [
        ModeAPIError(500, "x"),
        BadRequestError(400, "x"),
        RateLimitError(429, "x", retry_after=1.0),
        ModeConnectionError("x"),
        ModeTimeoutError("x"),
        RunTimeoutError("x"),
        DiscoveryUnavailableError("x"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_except_mode_error_catches_everything_this_package_raises(error):
    assert isinstance(error, ModeError)


@pytest.mark.parametrize(
    ("error", "answered_by_mode"),
    [
        (NotFoundError(404, "x"), True),
        (RateLimitError(429, "x"), True),
        (InternalServerError(500, "x"), True),
        (ModeConnectionError("x"), False),
        (ModeTimeoutError("x"), False),
        (RunTimeoutError("x"), False),
        (DiscoveryUnavailableError("x"), False),
    ],
    ids=lambda e: type(e).__name__ if isinstance(e, Exception) else str(e),
)
def test_except_mode_api_error_means_exactly_mode_answered_with_an_error(error, answered_by_mode):
    assert isinstance(error, ModeAPIError) is answered_by_mode


def test_a_timeout_is_a_connection_error_a_caller_can_still_narrow():
    """ "Mode is slow" and "the network is down" deserve different reactions."""
    assert issubclass(ModeTimeoutError, ModeConnectionError)


def test_no_exception_here_is_an_httpx_exception():
    for cls in (ModeError, ModeAPIError, ModeConnectionError, ModeTimeoutError, RunTimeoutError):
        assert not issubclass(cls, httpx.HTTPError)


def test_every_exception_httpx_defines_is_inside_the_transports_catch():
    """The guarantee is a closure, not a sample: httpx raises from four unrelated roots
    and only one of them is HTTPError, so catching that alone would still let three out.
    """
    defined = [v for v in vars(httpx).values() if isinstance(v, type) and issubclass(v, Exception)]
    assert len(defined) > 20
    assert [c.__name__ for c in defined if not issubclass(c, _policy.HTTPX_ERRORS)] == []


def test_the_rate_limit_signature_is_spelled_out_rather_than_forwarded():
    error = RateLimitError(
        429, "slow down", error_id="rate_limited", body={"m": 1}, method="GET", path="/acme"
    )
    assert (error.retry_after, error.error_id, error.body) == (None, "rate_limited", {"m": 1})


def test_a_run_timeout_hands_back_the_run_it_last_saw():
    """Resuming the poll should not cost another fetch of something we already had."""
    run = ReportRun._from({"token": "run1", "state": "enqueued"})
    error = RunTimeoutError("still going", run=run)
    assert error.run is run
    assert (error.token, error.state) == ("run1", "enqueued")


def test_a_run_timeout_still_accepts_the_two_strings_on_their_own():
    error = RunTimeoutError("still going", token="run1", state="enqueued")
    assert error.run is None
    assert (error.token, error.state) == ("run1", "enqueued")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectError("refused"), ModeConnectionError),
        (httpx.ReadError("reset"), ModeConnectionError),
        (httpx.ConnectTimeout("slow"), ModeTimeoutError),
        (httpx.ReadTimeout("slow"), ModeTimeoutError),
        (httpx.PoolTimeout("slow"), ModeTimeoutError),
    ],
    ids=lambda x: getattr(x, "__name__", type(x).__name__),
)
def test_transport_failures_become_the_narrowest_connection_error(exc, expected):
    error = _policy.connection_error(exc, method="GET", path="/acme/audit_logs?username=alice")
    assert type(error) is expected
    assert "alice" not in str(error)
    assert type(exc).__name__ in str(error)
