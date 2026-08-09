"""What the SDK is allowed to say about a request, and what it must never say.

The forbidden list is a security property, not a style rule: audit-log filters ride
in query strings and carry usernames, emails and IP addresses, and credentials ride
in headers. Nothing in the transport formats either into a log record, and this file
is where a reviewer gets to check that claim against every record an exercised retry
loop produces.

Scoped to the package's own two loggers on purpose. httpx logs the full request URL
on its own logger at INFO; silencing another library's logger is not the SDK's to do.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from mode_sdk import Mode
from mode_sdk.errors import ModeError
from mode_sdk.errors import RateLimitError

API = "https://app.mode.com/api"
TOKEN = "tok-abc123"
SECRET = "sec-xyz789"
FORBIDDEN = [
    "?",
    TOKEN,
    SECRET,
    "Basic ",
    "dG9rLWFiYzEyMzpzZWMteHl6Nzg5",
    "alice@example.com",
    "10.11.12.13",
    "hal+json",
    "slow down",
]


def client(**kwargs) -> Mode:
    kwargs.setdefault("backoff_factor", 0)
    return Mode("acme", token=TOKEN, secret=SECRET, **kwargs)


def ours(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("mode_sdk")]


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in ours(caplog) if r.levelno == logging.WARNING]


@pytest.fixture(autouse=True)
def _debug_level(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)


@respx.mock
def test_a_retried_call_is_visible_at_debug_and_warning(caplog):
    route = respx.get(f"{API}/acme/reports/r1")
    route.side_effect = [httpx.Response(500), httpx.Response(200, json={"token": "r1"})]
    with client(max_retries=2) as mode:
        mode.reports.get("r1")

    messages = [r.getMessage() for r in ours(caplog)]
    assert "client created workspace=acme owns_pool=True" in messages
    assert "client closed workspace=acme owns_pool=True" in messages
    assert "GET /acme/reports/r1 attempt 1" in messages
    assert any("GET /acme/reports/r1 -> 500" in m and "ms attempt 1" in m for m in messages)
    assert any("GET /acme/reports/r1 -> 200" in m and "ms attempt 2" in m for m in messages)
    assert warnings(caplog) == ["retrying GET /acme/reports/r1 after 500 in 0.00s (attempt 1/2)"]


@respx.mock
def test_the_rate_limit_bail_out_says_why_it_stopped(caplog):
    respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "900"}, json={"message": "slow"})
    )
    with client() as mode, pytest.raises(RateLimitError):
        mode.reports.get("r1")

    assert warnings(caplog) == [
        "GET /acme/reports/r1 asked for 900s, above the 60s cap; not retrying"
    ]


@respx.mock
def test_a_failed_connection_is_named_by_its_class(caplog):
    respx.get(f"{API}/acme/reports/r1").mock(side_effect=httpx.ConnectError("boom"))
    with client(max_retries=1) as mode, pytest.raises(ModeError):
        mode.reports.get("r1")

    assert warnings(caplog) == [
        "retrying GET /acme/reports/r1 after ConnectError in 0.00s (attempt 1/1)"
    ]


@respx.mock
def test_nothing_a_query_string_or_a_credential_touches_reaches_a_record(caplog):
    """The filters below are exactly the kind of thing /audit_logs is asked about."""
    respx.get(f"{API}/acme/audit_logs").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "900"}, json={"message": "slow"})
    )
    respx.get(f"{API}/reports").mock(return_value=httpx.Response(500, text="slow down"))

    with client(max_retries=1) as mode:
        with pytest.raises(RateLimitError):
            mode.transport.request(
                "GET",
                "/audit_logs",
                params={"username": "alice@example.com", "ip": "10.11.12.13"},
                headers={"Accept": "application/json"},
            )
        with pytest.raises(ModeError):
            mode.transport.request(
                "GET", "/reports?username=alice@example.com", workspace=False, json={"q": SECRET}
            )

    records = ours(caplog)
    assert records
    for record in records:
        message = record.getMessage()
        for banned in FORBIDDEN:
            assert banned not in message, f"{banned!r} leaked into {message!r}"


def test_the_sdk_installs_no_handlers_and_leaves_the_level_to_the_application():
    for name in ("mode_sdk", "mode_sdk.transport"):
        logger = logging.getLogger(name)
        assert logger.handlers == []
        assert logger.level == logging.NOTSET
        assert logger.propagate
