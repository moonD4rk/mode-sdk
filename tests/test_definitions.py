"""Definitions. Mode answers a selection it does not understand with 200 and the entire
library, so a failed selection looks exactly like one that returned a lot.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mode_sdk import Definition
from mode_sdk import Mode

API = "https://app.mode.com/api"


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


def collection(*items: dict) -> httpx.Response:
    return httpx.Response(200, json={"_embedded": {"definitions": list(items)}})


@respx.mock
def test_tokens_always_travel_with_the_only_filter_that_makes_them_count():
    """``tokens`` alone is ignored and ``filter=by_tokens`` alone selects nothing; only
    the pair selects, and any other filter value cancels the tokens.
    """
    route = respx.get(f"{API}/acme/definitions").mock(return_value=collection({"token": "d1"}))
    with client() as mode:
        mode.definitions.list(tokens=["d1", "d2"]).first_page()

    params = route.calls[0].request.url.params
    assert params["tokens"] == "d1,d2"
    assert params["filter"] == "by_tokens"


@respx.mock
def test_listing_everything_sends_neither_parameter():
    """A bare ``filter=by_tokens`` answers with zero rows, so it must not ride along
    unasked -- and no other filter value does anything at all.
    """
    route = respx.get(f"{API}/acme/definitions").mock(return_value=collection({"token": "d1"}))
    with client() as mode:
        mode.definitions.list().first_page()

    params = route.calls[0].request.url.params
    assert "filter" not in params
    assert "tokens" not in params


@respx.mock
def test_a_filter_can_still_be_forced_through_the_escape_hatch():
    """Removing the parameter is not removing the capability: ``extra_params`` merges last
    and wins, which is how a filter Mode ships later is reachable without an SDK release.
    """
    route = respx.get(f"{API}/acme/definitions").mock(return_value=collection({"token": "d1"}))
    with client() as mode:
        mode.definitions.list(tokens=["d1"], extra_params={"filter": "something_new"}).first_page()

    assert route.calls[0].request.url.params["filter"] == "something_new"


@respx.mock
def test_a_data_source_id_read_off_a_definition_can_be_written_straight_back():
    """Mode reads the field back as a string and declares it a JSON integer, so both go
    out as given rather than coerced.
    """
    route = respx.post(f"{API}/acme/definitions").mock(
        return_value=httpx.Response(200, json={"token": "d1"})
    )
    with client() as mode:
        mode.definitions.create("n", "select 1", data_source_id="10001")
        mode.definitions.create("n", "select 1", data_source_id=10001)

    sent = [json.loads(c.request.content)["definition"]["data_source_id"] for c in route.calls]
    assert sent == ["10001", 10001]


@respx.mock
def test_a_definition_may_be_named_by_its_model():
    route = respx.get(f"{API}/acme/definitions/d1").mock(
        return_value=httpx.Response(200, json={"token": "d1", "name": "active_user"})
    )
    with client() as mode:
        mode.definitions.get(Definition.from_payload({"token": "d1"}))

    assert route.called


def test_a_definition_with_no_token_raises_before_the_request():
    with client() as mode, pytest.raises(ValueError, match="definition="):
        mode.definitions.get(Definition.from_payload({"name": "unsaved"}))
