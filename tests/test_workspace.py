"""The workspace record, its Collections, and the two methods that shipped broken.

Both of the bugs pinned here were shipped as documented facts: ``spaces.update()`` sent
a POST that Mode has never routed, and ``workspace.account()`` called an endpoint Mode
refuses to every API key there is. Neither had a test, which is how a docstring that
asserted the opposite of the API survived a release.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mode_sdk import Mode
from mode_sdk import ModeError
from mode_sdk import Space

API = "https://app.mode.com/api"


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


def collection(key: str, *items: dict) -> httpx.Response:
    return httpx.Response(200, json={"_embedded": {key: list(items)}})


@respx.mock
def test_updating_a_collection_is_a_patch_and_never_a_post():
    """``POST /spaces/{s}`` and ``POST /collections/{s}`` both answer 404; only PATCH
    mutates. Only the PATCH route is mocked, so a POST raises from respx rather than
    quietly passing.
    """
    route = respx.patch(f"{API}/acme/spaces/sp1").mock(
        return_value=httpx.Response(200, json={"token": "sp1", "name": "renamed"})
    )
    with client() as mode:
        assert mode.spaces.update("sp1", name="renamed").name == "renamed"

    assert route.calls[0].request.method == "PATCH"


@respx.mock
def test_an_update_never_sends_space_type_because_mode_accepts_it_and_ignores_it():
    """A PATCH carrying ``space_type`` answers 200 and leaves the value untouched, so a
    keyword for it would report a change that did not happen. There is none, and a caller
    who insists can still reach it through ``extra_body``.
    """
    route = respx.patch(f"{API}/acme/spaces/sp1").mock(
        return_value=httpx.Response(200, json={"token": "sp1"})
    )
    with client() as mode:
        mode.spaces.update("sp1", name="n", description="d", default_access_level="edit")

    body = json.loads(route.calls[0].request.content)
    assert body == {"space": {"name": "n", "description": "d", "default_access_level": "edit"}}


@respx.mock
def test_a_private_collection_can_be_updated_without_sending_a_name():
    """Mode's ``_forms.edit`` on a private Collection declares ``default_access_level``
    and nothing else, so a required ``name=`` would make the private case unreachable
    without sending a field that Collection does not accept.
    """
    route = respx.patch(f"{API}/acme/spaces/sp1").mock(
        return_value=httpx.Response(200, json={"token": "sp1"})
    )
    with client() as mode:
        mode.spaces.update("sp1", default_access_level="view")

    assert json.loads(route.calls[0].request.content) == {"space": {"default_access_level": "view"}}


@respx.mock
def test_an_unknown_collection_filter_raises_instead_of_returning_the_wrong_rows():
    """Mode answers an unrecognised filter with 200 and the unfiltered result, and the
    match is case-sensitive, so ``filter="ALL"`` would silently return the wrong rows. No
    request is made at all, so the mock stays untouched.
    """
    route = respx.get(f"{API}/acme/spaces").mock(return_value=collection("spaces"))
    with client() as mode, pytest.raises(ValueError, match="all"):
        mode.spaces.list(filter="ALL")  # type: ignore[arg-type]

    assert route.call_count == 0


@respx.mock
def test_the_three_filters_mode_implements_go_out_unchanged():
    route = respx.get(f"{API}/acme/spaces").mock(
        return_value=collection("spaces", {"token": "sp1"})
    )
    with client() as mode:
        mode.spaces.list(filter="all")

    assert route.calls[0].request.url.params["filter"] == "all"


@respx.mock
def test_a_collections_datasets_arrive_under_the_reports_key():
    """A Dataset is a Report of type DatasetReport and Mode names the envelope after the
    underlying type. ``_embedded.datasets`` exists nowhere; the envelope here carries two
    lists so the sole-list fallback cannot rescue a wrong key.
    """
    respx.get(f"{API}/acme/spaces/sp1/datasets").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {
                    "reports": [{"token": "d1", "type": "DatasetReport"}],
                    "decoys": [{"token": "nope"}],
                }
            },
        )
    )
    with client() as mode:
        assert [d.token for d in mode.spaces.datasets("sp1").first_page()] == ["d1"]


@respx.mock
def test_a_collection_may_be_named_by_its_model_instead_of_its_token():
    route = respx.get(f"{API}/acme/spaces/sp1/reports").mock(
        return_value=collection("reports", {"token": "r1"})
    )
    with client() as mode:
        mode.spaces.reports(Space.from_payload({"token": "sp1", "name": "Ops"}))

    assert route.called


def test_a_model_with_no_token_names_itself_in_the_error():
    """Every token is ``str | None`` because Mode omits keys per endpoint, so callers used
    to write ``space.token or ""`` -- and an empty token builds ``/spaces//reports``.
    """
    with client() as mode, pytest.raises(ValueError, match="space="):
        mode.spaces.reports(Space.from_payload({"name": "no token here"}))


@respx.mock
def test_the_workspace_account_is_reached_through_modes_own_links_not_get_account():
    """``GET /account`` answers 400 "This endpoint is unsupported for use with Api Keys"
    for every API key, and an API key is the only credential this client carries. The
    route Mode publishes is /verify -> the key's own record -> its member.
    """
    verify = respx.get(f"{API}/verify").mock(
        return_value=httpx.Response(
            200,
            json={"workspace": "acme", "_links": {"self": {"href": "/api/acme/api_keys/k1"}}},
        )
    )
    key = respx.get(f"{API}/acme/api_keys/k1").mock(
        return_value=httpx.Response(
            200, json={"token_name": "k1", "_links": {"member": {"href": "/api/bob"}}}
        )
    )
    member = respx.get(f"{API}/bob").mock(
        return_value=httpx.Response(200, json={"username": "bob", "id": "42", "user": True})
    )
    with client() as mode:
        user = mode.workspace.account()

    assert (user.username, user.id) == ("bob", "42")
    assert (verify.call_count, key.call_count, member.call_count) == (1, 1, 1)


@respx.mock
def test_a_credential_with_no_member_behind_it_raises_rather_than_inventing_one():
    respx.get(f"{API}/verify").mock(
        return_value=httpx.Response(
            200, json={"_links": {"self": {"href": "/api/acme/api_keys/k1"}}}
        )
    )
    respx.get(f"{API}/acme/api_keys/k1").mock(
        return_value=httpx.Response(200, json={"token_name": "k1", "_links": {}})
    )
    with client() as mode, pytest.raises(ModeError, match="member"):
        mode.workspace.account()
