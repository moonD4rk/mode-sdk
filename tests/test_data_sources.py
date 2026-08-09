"""Data sources are addressed by token, and by nothing else: ``DataSource.id`` is the
numeric-looking field on the object and the one that does not route.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from mode_sdk import DataSource
from mode_sdk import Mode

API = "https://app.mode.com/api"


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


SOURCE = {"token": "9f8e7d6c5b4a", "id": "10001", "name": "Warehouse"}


@respx.mock
def test_a_data_source_is_fetched_by_its_token_when_handed_the_whole_model():
    """Passing the model is how a caller stops having to know which of its two
    identifiers Mode routes on.
    """
    route = respx.get(f"{API}/acme/data_sources/9f8e7d6c5b4a").mock(
        return_value=httpx.Response(200, json=SOURCE)
    )
    with client() as mode:
        assert mode.data_sources.get(DataSource.from_payload(SOURCE)).id == "10001"

    assert route.called


def test_the_numeric_id_is_refused_here_instead_of_404ing_at_mode():
    """An int would reach Mode and come back 404, so no request goes out at all."""
    with client() as mode, pytest.raises(ValueError, match="data_source="):
        mode.data_sources.get(10001)  # type: ignore[arg-type]


@respx.mock
def test_every_data_source_route_takes_the_same_kind_of_reference():
    """All four routes are checked together rather than one standing in for the rest."""
    source = DataSource.from_payload(SOURCE)
    routes = [
        respx.patch(f"{API}/acme/data_sources/9f8e7d6c5b4a").mock(
            return_value=httpx.Response(200, json=SOURCE)
        ),
        respx.post(f"{API}/acme/data_sources/9f8e7d6c5b4a/schema_updates").mock(
            return_value=httpx.Response(202, json={})
        ),
        respx.post(f"{API}/acme/data_sources/9f8e7d6c5b4a/purge").mock(
            return_value=httpx.Response(202, json={})
        ),
    ]
    with client() as mode:
        mode.data_sources.update(source, name="renamed")
        mode.data_sources.refresh_schema(source)
        mode.data_sources.purge(source)

    assert all(route.called for route in routes)


@respx.mock
def test_the_one_form_mode_actually_declares_is_reachable_through_extra_body():
    """``_forms.edit`` declares only ``data_source.tables[]``, which rides ``extra_body``
    rather than becoming a guessed-at keyword.
    """
    import json

    route = respx.patch(f"{API}/acme/data_sources/9f8e7d6c5b4a").mock(
        return_value=httpx.Response(200, json=SOURCE)
    )
    with client() as mode:
        mode.data_sources.update(
            "9f8e7d6c5b4a", extra_body={"tables": [{"name": "orders", "description": "one row"}]}
        )

    body = json.loads(route.calls[0].request.content)
    assert body == {"data_source": {"tables": [{"name": "orders", "description": "one row"}]}}
