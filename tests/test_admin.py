"""Administration: membership routes, group envelope keys, and audit-log error dialects."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import httpx
import pytest
import respx

from mode_sdk import Group
from mode_sdk import Mode

API = "https://app.mode.com/api"


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


def collection(key: str, *items: dict) -> httpx.Response:
    return httpx.Response(200, json={"_embedded": {key: list(items)}})


LITE_ROW = {
    "email": "alice@example.com",
    "name": "Alice Smith",
    "username": "alice105",
    "member_token": "d6e7f8091a2b",
    "state": "active",
    "admin": False,
}


@respx.mock
def test_the_workspaces_people_are_listed_at_memberships_lite():
    """``GET /memberships`` answers 404; only ``/memberships/lite`` returns rows. Only the
    lite route is mocked, so a request to the other path fails the test.
    """
    route = respx.get(f"{API}/acme/memberships/lite").mock(
        return_value=collection("memberships", LITE_ROW)
    )
    with client() as mode:
        rows = mode.memberships.list().first_page()

    assert route.called
    assert [(m.username, m.email, m.admin) for m in rows] == [
        ("alice105", "alice@example.com", False)
    ]


@respx.mock
def test_group_memberships_arrive_under_group_memberships():
    """The envelope here holds two lists, so ``_policy.embedded``'s sole-list fallback
    cannot fire and the declared key has to be right.
    """
    respx.get(f"{API}/acme/groups/g1/memberships").mock(
        return_value=httpx.Response(
            200,
            json={
                "_embedded": {
                    "group_memberships": [{"token": "gm1", "member_token": "u1"}],
                    "decoys": [{"token": "nope"}],
                }
            },
        )
    )
    with client() as mode:
        assert [m.token for m in mode.groups.memberships("g1").first_page()] == ["gm1"]


@respx.mock
def test_filtering_groups_by_data_source_requires_the_member_mode_asks_for():
    """``GET /groups?data_source=...`` alone answers
    400 "When sending data source, must send user". Named here rather than round-tripped.
    """
    route = respx.get(f"{API}/acme/groups").mock(return_value=collection("groups"))
    with client() as mode, pytest.raises(ValueError, match="member="):
        mode.groups.list(data_source="9f8e7d6c5b4a")

    assert route.call_count == 0


@respx.mock
def test_both_group_filters_go_out_together():
    route = respx.get(f"{API}/acme/groups").mock(return_value=collection("groups", {"token": "g1"}))
    with client() as mode:
        mode.groups.list(member="data_team", data_source="9f8e7d6c5b4a")

    params = route.calls[0].request.url.params
    assert params["member"] == "data_team"
    assert params["data_source"] == "9f8e7d6c5b4a"


@respx.mock
def test_a_group_may_be_named_by_its_model():
    route = respx.delete(f"{API}/acme/groups/g1").mock(return_value=httpx.Response(204))
    with client() as mode:
        assert mode.groups.delete(Group.from_payload({"token": "g1"})) is None

    assert route.called


@respx.mock
def test_the_seven_audit_log_filters_are_keywords_and_only_the_set_ones_travel():
    """Named keywords rather than a ``**filters`` bag, so a typo is a TypeError rather
    than a query parameter Mode ignores.
    """
    route = respx.get(f"{API}/acme/audit_logs").mock(
        return_value=httpx.Response(200, json={"audit_logs": []})
    )
    with client() as mode:
        mode.audit_logs.list(
            datetime(2026, 7, 1, tzinfo=UTC),
            "2026-07-02T00:00:00Z",
            action="report.run",
            username="bob",
        )

    params = route.calls[0].request.url.params
    assert params["start_timestamp"] == "2026-07-01T00:00:00+00:00"
    assert params["end_timestamp"] == "2026-07-02T00:00:00Z"
    assert params["action"] == "report.run"
    assert params["username"] == "bob"
    assert "ip" not in params and "entity_id" not in params


def test_an_undocumented_audit_log_filter_is_a_type_error_not_a_silent_parameter():
    with client() as mode, pytest.raises(TypeError):
        mode.audit_logs.list("2026-07-01", "2026-07-02", entiy_type="Report")  # type: ignore[call-arg]


@respx.mock
def test_audit_logs_ask_for_plain_json_because_that_endpoint_answers_hal_with_406():
    """The 406 arrives before authentication -- an anonymous request gets it too -- so the
    header override is what makes every other outcome reachable at all.
    """
    route = respx.get(f"{API}/acme/audit_logs").mock(
        return_value=httpx.Response(200, json={"audit_logs": [{"id": "a1"}]})
    )
    with client() as mode:
        list(mode.audit_logs.list("2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z"))

    assert route.calls[0].request.headers["accept"] == "application/json"
