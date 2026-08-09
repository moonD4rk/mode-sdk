"""Minting a Discovery credential -- the call a support ticket is most likely about.

It is a POST that creates a long-lived read credential whose secret is returned once,
so it goes over the same transport as everything else rather than a client of its own:
the SDK's User-Agent reaches Mode's access log, failures arrive as ModeErrors, and the
retry policy that refuses to replay a POST past the connect phase applies here too.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
import respx

from mode_sdk import create_signature_token
from mode_sdk.errors import DiscoveryUnavailableError
from mode_sdk.errors import InternalServerError
from mode_sdk.errors import ModeAPIError
from mode_sdk.errors import ModeConnectionError
from mode_sdk.errors import UnprocessableEntityError

MINT = "https://app.mode.com/batch/acme/signature_tokens"
MINTED = {"token": "st1", "access_key": "ak", "access_secret": "as"}


def mint(**kwargs) -> object:
    kwargs.setdefault("expires_at", "2027-01-01T00:00Z")
    return create_signature_token("acme", token="tok", secret="sec", name="etl", **kwargs)


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mode_sdk._transport.time.sleep", lambda _seconds: None)


@respx.mock
def test_the_mint_says_who_it_is_like_every_other_request():
    route = respx.post(MINT).mock(return_value=httpx.Response(200, json=MINTED))
    assert mint().access_secret == "as"
    request = route.calls[0].request
    assert request.headers["user-agent"].startswith("mode-sdk/")
    assert request.headers["authorization"] == "Basic dG9rOnNlYw=="
    assert request.headers["accept"] == "application/json"


@respx.mock
def test_a_naive_expiry_is_read_as_utc_before_it_goes_on_the_wire():
    route = respx.post(MINT).mock(return_value=httpx.Response(200, json=MINTED))
    mint(expires_at=datetime(2027, 1, 1, 12, 0, 0))
    body = route.calls[0].request.content.decode()
    assert '"expires_at":"2027-01-01T12:00:00+00:00"' in body
    assert '"authentication_for":"batch-api"' in body


@respx.mock
def test_a_connect_failure_arrives_as_a_mode_error_not_a_raw_httpx_one():
    respx.post(MINT).mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(ModeConnectionError) as caught:
        mint()
    assert isinstance(caught.value.__cause__, httpx.ConnectError)


@respx.mock
def test_a_connect_phase_failure_replays_because_nothing_was_minted():
    route = respx.post(MINT).mock(side_effect=httpx.ConnectError("dns"))
    with pytest.raises(ModeConnectionError):
        mint()
    assert route.call_count == 4


@respx.mock
def test_a_5xx_is_not_replayed_because_the_credential_may_already_exist():
    """The secret comes back once; a caller who lost the answer must not mint a second."""
    route = respx.post(MINT).mock(return_value=httpx.Response(500))
    with pytest.raises(InternalServerError):
        mint()
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_both_refusals_name_the_key_and_the_plan_rather_than_the_status(status):
    """Neither the scope nor the plan is fixed by presenting the same credential again."""
    respx.post(MINT).mock(return_value=httpx.Response(status, json={"message": "no"}))
    with pytest.raises(DiscoveryUnavailableError, match="workspace-admin key"):
        mint()


@respx.mock
def test_any_other_error_status_keeps_the_class_it_earned():
    respx.post(MINT).mock(return_value=httpx.Response(422, json={"message": "name too short"}))
    with pytest.raises(UnprocessableEntityError):
        mint()


@respx.mock
def test_a_two_hundred_that_is_not_json_is_an_api_error_not_a_decode_error():
    respx.post(MINT).mock(return_value=httpx.Response(200, html="<html>login</html>"))
    with pytest.raises(ModeAPIError) as caught:
        mint()
    assert caught.value.status_code == 200
