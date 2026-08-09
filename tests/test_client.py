"""Constructing a client, and everything it refuses to be constructed with.

The failures worth testing here are the quiet ones: a credential that resolved from
somewhere the caller did not mean, a client that looks configured but ignored half of
what it was handed, and an override that leaked into the client it was copied from.
None of those show up in a response, so each argument is tested rather than sampled.
"""

from __future__ import annotations

from base64 import b64encode

import httpx
import pytest
import respx

from mode_sdk import Discovery
from mode_sdk import Mode
from mode_sdk import SignatureToken
from mode_sdk import create_signature_token
from mode_sdk._transport import DEFAULT_TIMEOUT
from mode_sdk.errors import InternalServerError
from mode_sdk.models import Report

API = "https://app.mode.com/api"
BATCH = "https://app.mode.com/batch"
VARIABLES = ("MODE_WORKSPACE", "MODE_API_TOKEN", "MODE_API_SECRET")


def basic(token: str, secret: str) -> str:
    return "Basic " + b64encode(f"{token}:{secret}".encode()).decode()


def client(**kwargs) -> Mode:
    kwargs.setdefault("backoff_factor", 0)
    return Mode("acme", token="token", secret="secret", **kwargs)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer with a populated .env must not see different results from CI."""
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE_WORKSPACE", "acme")
    monkeypatch.setenv("MODE_API_TOKEN", "env-token")
    monkeypatch.setenv("MODE_API_SECRET", "env-secret")


@respx.mock
def test_a_configured_environment_is_a_whole_configuration(configured_env):
    route = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(200, json={"token": "r1"})
    )
    with Mode() as mode:
        assert mode.workspace_name == "acme"
        mode.reports.get("r1")
    assert route.calls[0].request.headers["authorization"] == basic("env-token", "env-secret")


@respx.mock
def test_an_argument_wins_over_the_variable_it_falls_back_to(configured_env):
    route = respx.get(f"{API}/other/reports/r1").mock(
        return_value=httpx.Response(200, json={"token": "r1"})
    )
    with Mode("other", token="arg-token", secret="arg-secret") as mode:
        mode.reports.get("r1")
    assert route.calls[0].request.headers["authorization"] == basic("arg-token", "arg-secret")


@pytest.mark.parametrize(
    ("argument", "variable"),
    [("workspace", "MODE_WORKSPACE"), ("token", "MODE_API_TOKEN"), ("secret", "MODE_API_SECRET")],
)
def test_a_missing_credential_names_the_variable_that_would_have_supplied_it(
    monkeypatch, configured_env, argument, variable
):
    monkeypatch.delenv(variable)
    with pytest.raises(ValueError) as caught:
        Mode()
    assert str(caught.value) == f"{argument} is required: pass {argument}= or set {variable}"


def test_from_env_is_gone_because_the_constructor_now_does_all_of_it():
    assert not hasattr(Mode, "from_env")


def test_a_secret_cannot_be_handed_over_by_position():
    """Two same-shaped strings side by side are one transposition from sending the
    secret as the username, and Mode answers that with a plain 401.
    """
    with pytest.raises(TypeError):
        Mode("acme", "token", "secret")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("timeout", 5.0),
        ("default_headers", {"X-Trace": "1"}),
        ("limits", httpx.Limits(max_connections=5)),
        ("http2", True),
        ("verify", False),
        ("proxy", "http://localhost:8080"),
    ],
)
def test_injecting_a_client_refuses_every_setting_it_would_silently_ignore(name, value):
    with httpx.Client() as http, pytest.raises(ValueError, match=f"{name}="):
        Mode("acme", token="t", secret="s", http_client=http, **{name: value})


@respx.mock
def test_an_injected_client_may_be_the_thing_that_carries_the_credentials():
    """Injection means the caller owns HTTP policy, and auth is part of that policy."""
    route = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(200, json={"token": "r1"})
    )
    with httpx.Client(headers={"Authorization": "Bearer mine"}) as http:
        with Mode("acme", http_client=http) as mode:
            mode.reports.get("r1")
    assert route.calls[0].request.headers["authorization"] == "Bearer mine"


@pytest.mark.parametrize("half", ["argument", "environment"])
def test_half_a_credential_pair_is_a_mistake_even_with_an_injected_client(monkeypatch, half):
    if half == "environment":
        monkeypatch.setenv("MODE_API_TOKEN", "env-token")
        token = None
    else:
        token = "t"
    with httpx.Client() as http, pytest.raises(ValueError, match="MODE_API_SECRET"):
        Mode("acme", token=token, http_client=http)


def test_an_injected_client_does_not_excuse_the_workspace():
    """Scoping lives in the path, not in the pool, so no client can supply it."""
    with httpx.Client() as http, pytest.raises(ValueError, match="MODE_WORKSPACE"):
        Mode(http_client=http)


@respx.mock
def test_a_timeout_override_rides_the_request_rather_than_a_second_pool():
    route = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(200, json={"token": "r1"})
    )
    with client() as mode:
        patient = mode.with_options(timeout=600.0)
        patient.reports.get("r1")
        mode.reports.get("r1")
    assert route.calls[0].request.extensions["timeout"] == httpx.Timeout(600.0).as_dict()
    assert route.calls[1].request.extensions["timeout"] == DEFAULT_TIMEOUT.as_dict()
    assert patient.transport._http is mode.transport._http


@respx.mock
def test_a_retry_override_stays_on_the_copy_that_asked_for_it():
    route = respx.get(f"{API}/acme/reports/r1").mock(return_value=httpx.Response(500))
    with client(max_retries=3) as mode:
        with pytest.raises(InternalServerError):
            mode.with_options(max_retries=1).reports.get("r1")
        assert route.call_count == 2
        with pytest.raises(InternalServerError):
            mode.reports.get("r1")
        assert route.call_count == 6


@respx.mock
def test_a_header_override_reaches_the_copy_and_only_the_copy():
    route = respx.get(f"{API}/acme/reports/r1").mock(
        return_value=httpx.Response(200, json={"token": "r1"})
    )
    with client(default_headers={"X-App": "etl"}) as mode:
        mode.with_options(default_headers={"X-Trace": "abc"}).reports.get("r1")
        mode.reports.get("r1")
    copy, original = route.calls[0].request, route.calls[1].request
    assert (copy.headers["x-trace"], copy.headers["x-app"]) == ("abc", "etl")
    assert "x-trace" not in original.headers


def test_closing_a_copy_does_not_take_the_pool_away_from_its_original():
    with client() as mode:
        copy = mode.with_options(max_retries=0)
        copy.close()
        assert not copy.is_closed
        assert not mode.is_closed


def test_closing_the_original_closes_every_view_of_it():
    mode = client()
    copy = mode.with_options(max_retries=0)
    mode.close()
    assert mode.is_closed
    assert copy.is_closed


def test_is_closed_follows_the_pool_through_the_context_manager():
    with client() as mode:
        assert not mode.is_closed
    assert mode.is_closed


def test_a_namespace_is_built_once_and_not_before_it_is_asked_for():
    with client() as mode:
        assert "reports" not in mode.__dict__
        assert mode.reports is mode.reports
        assert "reports" in mode.__dict__


def test_building_namespaces_lazily_kept_the_wiring_it_replaced():
    with client() as mode:
        assert mode.reports.runs is mode.report_runs
        assert mode.reports._queries is mode.queries
        assert mode.report_runs.query_runs is mode.query_runs
        assert mode.report_runs._exports is mode.exports
        assert mode.query_runs._exports is mode.exports


class Subclassed(Mode):
    """A subclass declares none of the namespaces, so its own class dict names none."""


@pytest.mark.parametrize("build", [Mode, Subclassed])
def test_a_copy_wires_its_namespaces_to_its_own_transport(build):
    """A namespace kept from the original would use the original's budget and deadline
    while the copy reports the ones that were asked for -- a wrong client that answers
    every question right.
    """
    with build("acme", token="token", secret="secret") as mode:
        eager = mode.reports
        copy = mode.with_options(max_retries=0)
        assert copy.reports is not eager
        assert copy.reports._t is copy.transport
        assert copy.reports._t.max_retries == 0


def test_a_repr_says_where_the_client_points_and_nothing_about_how_it_gets_in():
    with client() as mode:
        assert repr(mode) == "<Mode workspace='acme'>"


@respx.mock
def test_discovery_reads_the_same_workspace_variable(configured_env):
    route = respx.get(f"{BATCH}/acme/members").mock(return_value=httpx.Response(200, json={}))
    with Discovery(token="t", access_key="k", access_secret="s") as discovery:
        list(discovery.members())
    assert route.called


def test_a_signature_token_cannot_be_handed_over_by_position():
    signature = SignatureToken._from({"token": "t", "access_key": "k", "access_secret": "s"})
    with pytest.raises(TypeError):
        Discovery("acme", signature)


def test_discovery_refuses_the_timeout_an_injected_client_would_ignore():
    with httpx.Client() as http, pytest.raises(ValueError, match="timeout="):
        Discovery(
            "acme", token="t", access_key="k", access_secret="s", timeout=5.0, http_client=http
        )


@respx.mock
def test_an_injected_client_may_carry_the_discovery_bearer_itself():
    route = respx.get(f"{BATCH}/acme/members").mock(return_value=httpx.Response(200, json={}))
    with httpx.Client(headers={"Authorization": "Bearer mine"}) as http:
        with Discovery("acme", http_client=http) as discovery:
            list(discovery.members())
    assert route.calls[0].request.headers["authorization"] == "Bearer mine"


def test_part_of_a_signature_triple_is_never_an_injected_clients_own_auth():
    with httpx.Client() as http, pytest.raises(ValueError, match="signature token"):
        Discovery("acme", token="t", http_client=http)


@respx.mock
def test_minting_reads_the_same_three_variables_as_the_client(configured_env):
    route = respx.post(f"{BATCH}/acme/signature_tokens").mock(
        return_value=httpx.Response(200, json={"token": "st1"})
    )
    create_signature_token(name="etl", expires_at="2027-01-01T00:00Z")
    assert route.calls[0].request.headers["authorization"] == basic("env-token", "env-secret")


# --- report_url: the last identifier argument that did not go through token_of ---


def test_report_url_takes_the_model_the_rest_of_the_package_takes():
    """``mode.report_url(mode.reports.create(...))`` is the natural call, and it was the
    broken one: interpolating a Report into an f-string is not an error, so the bare-str
    version answered with 400 characters of repr shaped like a URL.
    """
    report = Report.from_payload({"token": "r1", "name": "Revenue"})
    with Mode("acme", token="t", secret="s") as mode:
        assert mode.report_url(report) == "https://app.mode.com/acme/reports/r1"
        assert mode.report_url("r1") == mode.report_url(report)


@pytest.mark.parametrize(
    "report",
    [
        pytest.param("", id="empty-token"),
        pytest.param(Report.from_payload({"name": "Revenue"}), id="tokenless-projection"),
    ],
)
def test_report_url_refuses_what_would_build_a_url_with_a_hole_in_it(report):
    """``/datasets/{d}/reports`` returns a four-key stub with no token, so a Report whose
    token is None is a value a caller really holds.
    """
    with Mode("acme", token="t", secret="s") as mode, pytest.raises(ValueError, match="report="):
        mode.report_url(report)
