"""The retry rules on their own, with no socket and no clock.

_policy exists so these decisions can be enumerated rather than sampled through a
mock server, which is why the matrix below is a table and not a handful of scenarios.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest

from mode_sdk import _policy

CONNECT_PHASE = [
    httpx.ConnectError("refused"),
    httpx.ConnectTimeout("too slow"),
    httpx.PoolTimeout("no free connection"),
]
POST_CONNECT = [
    httpx.ReadError("reset"),
    httpx.ReadTimeout("too slow"),
    httpx.WriteError("broken pipe"),
    httpx.WriteTimeout("too slow"),
    httpx.RemoteProtocolError("garbage"),
]
# httpx errors the transport catches that are not TransportErrors at all.
NEVER_REPLAYED = [
    httpx.TooManyRedirects("round and round"),
    httpx.DecodingError("not gzip after all"),
    httpx.InvalidURL("newline in the path"),
]


@pytest.mark.parametrize("exc", CONNECT_PHASE, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE", "PATCH", "POST"])
def test_connect_phase_failures_are_retried_for_every_method(exc, method):
    """Nothing reached Mode, so a replay cannot start a second warehouse run."""
    assert _policy.should_retry_exception(exc, method=method, retry=None)


@pytest.mark.parametrize("exc", POST_CONNECT, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("GET", True),
        ("HEAD", True),
        ("PUT", True),
        ("DELETE", True),
        ("PATCH", True),
        ("POST", False),
    ],
)
def test_post_connect_failures_are_retried_only_where_a_replay_converges(exc, method, expected):
    assert _policy.should_retry_exception(exc, method=method, retry=None) is expected


@pytest.mark.parametrize("exc", CONNECT_PHASE + POST_CONNECT, ids=lambda e: type(e).__name__)
def test_retry_false_pins_any_request_down(exc):
    assert not _policy.should_retry_exception(exc, method="GET", retry=False)


@pytest.mark.parametrize("exc", POST_CONNECT, ids=lambda e: type(e).__name__)
def test_retry_true_opts_a_post_into_replaying(exc):
    assert _policy.should_retry_exception(exc, method="POST", retry=True)


def test_an_unclassified_transport_error_is_treated_as_post_connect():
    exc = httpx.ProxyError("proxy said no")
    assert _policy.should_retry_exception(exc, method="GET", retry=None)
    assert not _policy.should_retry_exception(exc, method="POST", retry=None)


def test_a_non_transport_exception_is_never_a_retry():
    assert not _policy.should_retry_exception(ValueError("nope"), method="GET", retry=None)


@pytest.mark.parametrize("exc", NEVER_REPLAYED, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("retry", [None, True])
def test_the_httpx_errors_outside_the_transport_family_repeat_exactly_when_replayed(
    exc, method, retry
):
    """A redirect loop, a body that is not the gzip it claims, a URL that will not
    parse: none of them turn out differently the second time, so none of them retry.
    """
    assert not _policy.should_retry_exception(exc, method=method, retry=retry)


@pytest.mark.parametrize(
    ("status", "method", "expected"),
    [
        (408, "GET", True),
        (429, "GET", True),
        (500, "GET", True),
        (502, "GET", True),
        (503, "GET", True),
        (504, "GET", True),
        (408, "PATCH", True),
        (500, "DELETE", True),
        (409, "GET", False),
        (400, "GET", False),
        (404, "GET", False),
        (422, "GET", False),
        (200, "GET", False),
        (429, "POST", False),
        (500, "POST", False),
    ],
)
def test_status_retry_matrix(status, method, expected):
    assert _policy.should_retry_status(status, method=method, retry=None) is expected


@pytest.mark.parametrize("status", sorted(_policy.RETRY_STATUSES))
def test_a_post_can_be_opted_in_and_any_method_can_be_pinned_down(status):
    assert _policy.should_retry_status(status, method="POST", retry=True)
    assert not _policy.should_retry_status(status, method="GET", retry=False)


@pytest.mark.parametrize("attempt", range(8))
def test_backoff_stays_inside_the_full_jitter_window(attempt):
    ceiling = min(_policy.MAX_BACKOFF, 0.5 * 2**attempt)
    assert _policy.backoff_delay(attempt, 0.5, rand=lambda _lo, hi: hi) == ceiling
    assert _policy.backoff_delay(attempt, 0.5, rand=lambda lo, _hi: lo) == 0.0


def test_backoff_never_exceeds_the_cap_however_many_attempts():
    assert _policy.backoff_delay(30, 0.5, rand=lambda _lo, hi: hi) == _policy.MAX_BACKOFF


def test_an_absurd_retry_budget_still_yields_a_delay_rather_than_an_overflow():
    """2.0**attempt stops being a float around 1024, and OverflowError is not a ModeError."""
    assert _policy.backoff_delay(5000, 0.5, rand=lambda _lo, hi: hi) == _policy.MAX_BACKOFF


@pytest.mark.parametrize("attempt", range(4))
def test_a_negative_backoff_factor_never_asks_for_a_negative_sleep(attempt):
    """time.sleep rejects one, from inside a call that was already failing."""
    assert _policy.backoff_delay(attempt, -1.0, rand=lambda _lo, hi: hi) == 0.0
    assert _policy.backoff_delay(attempt, -1.0) >= 0.0


def test_full_jitter_starts_at_zero_rather_than_at_the_previous_delay():
    """Additive jitter leaves concurrent clients retrying together; full jitter does not."""
    window = [_policy.backoff_delay(3, 0.5) for _ in range(200)]
    assert min(window) < 1.0
    assert max(window) <= 4.0


def test_retry_after_reads_delta_seconds():
    assert _policy.parse_retry_after("30") == 30.0


def test_retry_after_reads_an_http_date():
    now = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
    assert _policy.parse_retry_after(format_datetime(now + timedelta(seconds=45)), now=now) == 45.0


def test_a_retry_after_date_in_the_past_means_now_not_a_negative_wait():
    now = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
    assert _policy.parse_retry_after(format_datetime(now - timedelta(hours=1)), now=now) == 0.0


def test_a_negative_delta_seconds_also_clamps_to_zero():
    assert _policy.parse_retry_after("-5") == 0.0


@pytest.mark.parametrize("value", ["soon", "", "Tue, 32 Aug 2026 99:99:99 GMT", "  "])
def test_unparseable_retry_after_is_no_hint_at_all(value):
    assert _policy.parse_retry_after(value) is None


def test_a_missing_retry_after_is_no_hint():
    assert _policy.parse_retry_after(None) is None


def test_a_naive_http_date_is_read_as_utc():
    now = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
    assert _policy.parse_retry_after("Fri, 07 Aug 2026 12:00:10 -0000", now=now) == 10.0


@pytest.mark.parametrize(
    ("hint", "expected"),
    [(None, False), (0.0, False), (60.0, False), (60.1, True), (3600.0, True)],
)
def test_the_cap_decides_when_a_hint_stops_being_a_retry(hint, expected):
    assert _policy.exceeds_retry_after_cap(hint) is expected


@pytest.mark.parametrize(
    ("workspace", "path", "scoped", "expected"),
    [
        ("acme", "/reports/r1", True, "/acme/reports/r1"),
        ("acme", "reports/r1", True, "/acme/reports/r1"),
        ("acme", "", True, "/acme"),
        ("acme", "/verify", False, "/verify"),
        ("acme", "", False, "/"),
        ("batch/acme", "/reports", True, "/batch/acme/reports"),
    ],
)
def test_paths_are_workspace_scoped_unless_they_opt_out(workspace, path, scoped, expected):
    assert _policy.scoped_path(workspace, path, workspace_scoped=scoped) == expected


def test_urls_are_joined_by_the_sdk_and_absolute_hrefs_pass_through():
    assert _policy.absolute_url("https://app.mode.com/api", "/acme/reports") == (
        "https://app.mode.com/api/acme/reports"
    )
    assert _policy.absolute_url("https://app.mode.com/api/", "/acme") == (
        "https://app.mode.com/api/acme"
    )
    assert _policy.absolute_url("https://app.mode.com", "https://s3.example/x") == (
        "https://s3.example/x"
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/acme/audit_logs?username=alice@example.com", "/acme/audit_logs"),
        ("/batch/acme/reports?page=2#frag", "/batch/acme/reports"),
        ("/acme/reports/r1", "/acme/reports/r1"),
    ],
)
def test_a_query_string_never_survives_the_trip_to_a_log_or_a_message(path, expected):
    assert _policy.safe_path(path) == expected
