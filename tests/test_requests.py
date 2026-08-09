"""What goes out on a read: query parameters and negotiated content types.

Both cases here failed silently rather than loudly, which is why they are pinned.
A dropped filter returned the wrong rows with a 200, and a content type the endpoint
does not speak was refused before the credential was ever read.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mode_sdk import Mode

API = "https://app.mode.com/api"


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


def collection(key: str, *items: dict) -> httpx.Response:
    return httpx.Response(200, json={"_embedded": {key: list(items)}})


@respx.mock
def test_looking_up_definitions_by_token_sends_the_filter_that_makes_tokens_count():
    """Without filter=by_tokens Mode ignores tokens and answers with the whole library."""
    route = respx.get(f"{API}/acme/definitions").mock(
        return_value=collection("definitions", {"token": "d1"})
    )
    with client() as mode:
        mode.definitions.list(tokens=["d1", "d2"]).first_page()

    params = route.calls[0].request.url.params
    assert params["tokens"] == "d1,d2"
    assert params["filter"] == "by_tokens"


@respx.mock
def test_listing_every_definition_sends_no_filter_at_all():
    """A bare filter=by_tokens answers with zero rows, so it must not be sent unasked."""
    route = respx.get(f"{API}/acme/definitions").mock(
        return_value=collection("definitions", {"token": "d1"})
    )
    with client() as mode:
        mode.definitions.list().first_page()

    assert "filter" not in route.calls[0].request.url.params
    assert "tokens" not in route.calls[0].request.url.params


@respx.mock
def test_no_filter_can_be_supplied_to_cancel_a_token_selection():
    """`filter="all"` alongside tokens makes Mode answer with the whole library -- more
    rows than the unfiltered call, from a call that reads as narrower. There is no filter
    parameter for that reason; extra_params is the way past.
    """
    with client() as mode:
        with pytest.raises(TypeError):
            mode.definitions.list(filter="all", tokens=["d1"])  # type: ignore[call-arg]


@respx.mock
def test_an_empty_token_list_selects_nothing_rather_than_everything():
    """`tokens=[]` is a caller asking for no definitions, which is what Mode answers to an
    empty `tokens=`. Treating it as "unset" would fetch the whole library instead.
    """
    route = respx.get(f"{API}/acme/definitions").mock(return_value=collection("definitions"))
    with client() as mode:
        assert mode.definitions.list(tokens=[]).list() == []

    params = route.calls[0].request.url.params
    assert params["tokens"] == ""
    assert params["filter"] == "by_tokens"


@respx.mock
def test_audit_logs_ask_for_plain_json_because_that_endpoint_does_not_speak_hal():
    """The client-wide Accept: application/hal+json gets a 406 here, before auth."""
    route = respx.get(f"{API}/acme/audit_logs").mock(
        return_value=httpx.Response(200, json={"audit_logs": [{"id": "a1"}]})
    )
    with client() as mode:
        list(mode.audit_logs.list("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z"))

    assert route.calls[0].request.headers["accept"] == "application/json"


@respx.mock
def test_every_other_collection_still_asks_for_hal():
    route = respx.get(f"{API}/acme/spaces").mock(
        return_value=collection("spaces", {"token": "sp1"})
    )
    with client() as mode:
        mode.spaces.list().first_page()

    assert route.calls[0].request.headers["accept"] == "application/hal+json"
