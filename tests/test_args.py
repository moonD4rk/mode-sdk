"""The one conversion every resource module shares, and the fact that it is one.

Three modules shipped three ``token_of`` implementations with three different error
messages, and the differences were not cosmetic: one returned the empty string
unexamined, and two disagreed about which numeric data-source value to refuse. These
tests pin the merged behaviour and the merge itself.
"""

from __future__ import annotations

import ast
import json
import pathlib

import httpx
import pytest
import respx

from mode_sdk import Mode
from mode_sdk.models import Dataset
from mode_sdk.models import DataSource
from mode_sdk.models import Report
from mode_sdk.models import ReportRun
from mode_sdk.models import Space
from mode_sdk.resources import _args

API = "https://app.mode.com/api"
RESOURCES = pathlib.Path(_args.__file__).parent


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


def _definitions(name: str) -> list[str]:
    """Which resource modules define a function of this name at module level."""
    found = []
    for path in sorted(RESOURCES.glob("*.py")):
        tree = ast.parse(path.read_text())
        if any(isinstance(n, ast.FunctionDef) and n.name == name for n in tree.body):
            found.append(path.name)
    return found


def test_the_shared_helpers_are_defined_exactly_once():
    """A duplicate is not a style problem: the copies had diverged in behaviour."""
    assert _definitions("token_of") == ["_args.py"]
    assert _definitions("write_body") == ["_args.py"]
    assert _definitions("data_source_token") == ["_args.py"]


def test_a_token_or_a_model_both_reach_the_same_string():
    assert _args.token_of("r1", "report") == "r1"
    assert _args.token_of(Report.from_payload({"token": "r1"}), "report") == "r1"


def test_an_empty_token_is_refused_rather_than_built_into_a_path():
    """``/reports//runs`` is a 404 that names nothing; this names the argument."""
    with pytest.raises(ValueError, match="report= is an empty string"):
        _args.token_of("", "report")


def test_a_model_that_lost_its_token_is_named_with_its_type():
    with pytest.raises(ValueError, match=r"run= was passed a ReportRun whose \.token is None"):
        _args.token_of(ReportRun.from_payload({"state": "succeeded"}), "run")


def test_an_export_path_refuses_the_empty_token_too():
    """The export module's own copy returned ``""`` unexamined, so this call used to be a
    request to ``/reports//runs/run1/results/content.csv``.
    """
    with client() as mode, pytest.raises(ValueError, match="report= is an empty string"):
        mode.exports.report_run("", "run1")


# --- the numeric data-source id, which the read side hands back as a string ---


@pytest.mark.parametrize("wrong", [10001, "10001", "1"])
def test_a_data_source_id_is_refused_in_either_json_type(wrong: int | str):
    """``DataSource.id`` arrives as the *string* ``"10001"``, so a check for the ``int``
    alone misses the value a caller actually holds. Both spellings 404 at Mode.
    """
    with pytest.raises(ValueError, match="numeric id"):
        _args.data_source_token(wrong)


def test_a_twelve_character_all_digit_token_is_not_mistaken_for_an_id():
    """The length test is load-bearing: all-digit 12-character tokens are real, so a bare
    ``isdigit()`` refusal would reject a legal one.
    """
    assert _args.data_source_token("92a3b4c5d6e7") == "92a3b4c5d6e7"


def test_a_data_source_model_travels_by_token_not_by_its_id():
    source = DataSource.from_payload({"id": "10001", "token": "9f8e7d6c5b4a"})
    assert _args.data_source_token(source) == "9f8e7d6c5b4a"


@respx.mock
def test_every_data_source_route_shares_that_refusal():
    """Every route that 404s on a numeric id shares one guard, so all are checked."""
    with client() as mode:
        for call in (
            lambda: mode.data_sources.get("10001"),
            lambda: mode.datasets.list(data_source="10001"),
            lambda: mode.reports.list(data_source="10001"),
        ):
            with pytest.raises(ValueError, match="numeric id"):
                call()
    assert not respx.calls


# --- extra_body merges inside Mode's wrapper ---


def test_extra_body_merges_inside_the_wrapper_and_after_the_none_strip():
    payload = _args.write_body("space", {"description": None}, name="Q3", description="dropped")
    assert payload == {"space": {"name": "Q3", "description": None}}


def test_a_none_keyword_is_not_sent_at_all():
    assert _args.write_body("report", None, name="Q3", description=None) == {
        "report": {"name": "Q3"}
    }


@respx.mock
def test_a_dataset_move_sends_the_space_token_not_the_model():
    """``space_token=`` takes a ``Space`` like every other ref; reports.update already did."""
    route = respx.patch(f"{API}/acme/datasets/d1").mock(
        return_value=httpx.Response(200, json={"token": "d1"})
    )
    with client() as mode:
        mode.datasets.update(
            Dataset.from_payload({"token": "d1"}), space_token=Space.from_payload({"token": "sp1"})
        )
    assert json.loads(route.calls[0].request.read()) == {"report": {"space_token": "sp1"}}
