"""What Mode calls a dataset and what it actually sends back.

Every envelope key asserted here is a *report* key on a dataset URL. The tests put a
second list inside ``_embedded`` on purpose: with only one list present, the sole-list
fallback in ``_policy.embedded()`` rescues any key at all, so a single-list fixture
cannot tell a correct declaration from a lucky one.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mode_sdk import Dataset
from mode_sdk import Mode

API = "https://app.mode.com/api"


def client() -> Mode:
    return Mode("acme", token="token", secret="secret", backoff_factor=0)


def sent(route) -> dict:
    return json.loads(route.calls[0].request.read())


def two_lists(key: str, *items: dict) -> httpx.Response:
    """The envelope Mode sends, plus a second list the sole-list fallback cannot see past."""
    return httpx.Response(200, json={"_embedded": {key: list(items), "decoys": [{"token": "x"}]}})


@respx.mock
def test_a_collections_datasets_arrive_under_the_reports_key():
    """``_embedded.reports``, never ``_embedded.datasets``: a Dataset is a Report with
    ``type="DatasetReport"``, and Mode names the envelope after what it really is.
    """
    respx.get(f"{API}/acme/spaces/s1/datasets").mock(
        return_value=two_lists("reports", {"token": "d1", "type": "DatasetReport"})
    )
    with client() as mode:
        datasets = mode.datasets.list(space="s1").first_page()

    assert [d.token for d in datasets] == ["d1"]
    assert datasets[0].type == "DatasetReport"


@respx.mock
def test_a_datasets_runs_arrive_under_the_report_runs_key():
    respx.get(f"{API}/acme/datasets/d1/runs").mock(
        return_value=two_lists("report_runs", {"token": "run1", "state": "succeeded"})
    )
    with client() as mode:
        assert [r.token for r in mode.dataset_runs.list("d1").first_page()] == ["run1"]


@respx.mock
def test_a_datasets_schedules_arrive_under_the_report_schedules_key():
    respx.get(f"{API}/acme/datasets/d1/schedules").mock(
        return_value=two_lists("report_schedules", {"token": "sch1", "cron_hour": 2})
    )
    with client() as mode:
        schedules = mode.dataset_schedules.list("d1").first_page()

    assert [s.token for s in schedules] == ["sch1"]
    assert schedules[0].cron_hour == 2


@respx.mock
def test_a_datasets_fields_and_field_descriptions_keep_their_own_keys():
    """Not everything under a dataset is renamed -- only what is a report underneath."""
    respx.get(f"{API}/acme/datasets/d1/fields").mock(
        return_value=two_lists("fields", {"name": "region", "type": "string"})
    )
    respx.get(f"{API}/acme/datasets/d1/field_descriptions").mock(
        return_value=two_lists("field_descriptions", {"token": "fd1", "name": "region"})
    )
    with client() as mode:
        assert [f.name for f in mode.datasets.fields("d1").first_page()] == ["region"]
        assert [f.token for f in mode.dataset_fields.list("d1").first_page()] == ["fd1"]


@respx.mock
def test_the_reports_built_on_a_dataset_are_a_projection_not_full_reports():
    """Four keys and no more, so every other Report field on these is None. Documented on
    the method because a caller who assumes otherwise reads ``None`` as "no space".
    """
    respx.get(f"{API}/acme/datasets/d1/reports").mock(
        return_value=two_lists("reports", {"token": "r1", "name": "Launch", "creator": "Jia Xin"})
    )
    with client() as mode:
        report = mode.datasets.reports("d1").first_page()[0]

    assert report.token == "r1"
    assert report.space_token is None
    assert report.raw["creator"] == "Jia Xin"


def test_a_numeric_data_source_id_is_refused_before_it_can_404():
    """``DataSource.id`` is a string of digits, so it is the identifier a caller reaches
    for -- and ``GET /data_sources/10001/datasets`` answers 404 'data source not found'.
    """
    with client() as mode:
        with pytest.raises(ValueError, match="numeric id"):
            mode.datasets.list(data_source="10001")


@respx.mock
def test_a_data_source_token_routes_to_that_data_sources_datasets():
    route = respx.get(f"{API}/acme/data_sources/9f8e7d6c5b4a/datasets").mock(
        return_value=two_lists("reports", {"token": "d1"})
    )
    with client() as mode:
        mode.datasets.list(data_source="9f8e7d6c5b4a").first_page()
    assert route.called


def test_listing_with_neither_parent_says_so_rather_than_letting_mode_400():
    with client() as mode:
        with pytest.raises(ValueError, match="space= or data_source="):
            mode.datasets.list()


@respx.mock
def test_a_dataset_write_is_wrapped_in_report_not_dataset():
    """A Dataset is a Report underneath, so Mode's write endpoint takes the report
    wrapper; sending ``{"dataset": ...}`` changes nothing.
    """
    route = respx.patch(f"{API}/acme/datasets/d1").mock(
        return_value=httpx.Response(200, json={"token": "d1", "name": "Revenue"})
    )
    with client() as mode:
        assert mode.datasets.update("d1", name="Revenue").name == "Revenue"

    assert sent(route) == {"report": {"name": "Revenue"}}


@respx.mock
def test_an_omitted_field_is_left_alone_and_extra_body_merges_inside_the_wrapper():
    route = respx.patch(f"{API}/acme/datasets/d1").mock(
        return_value=httpx.Response(200, json={"token": "d1"})
    )
    with client() as mode:
        mode.datasets.update("d1", name="Revenue", extra_body={"public": True})

    assert sent(route) == {"report": {"name": "Revenue", "public": True}}


@respx.mock
def test_refreshing_datasets_in_a_report_accepts_models_and_tokens_alike():
    route = respx.post(f"{API}/acme/reports/r1/runs").mock(
        return_value=httpx.Response(200, json={"token": "run1"})
    )
    with client() as mode:
        mode.datasets.refresh_in_report("r1", [Dataset.from_payload({"token": "d1"}), "d2"])

    assert sent(route) == {"report": {"dataset_tokens": [{"token": "d1"}, {"token": "d2"}]}}


@respx.mock
def test_a_dataset_model_can_stand_in_for_its_token_everywhere():
    dataset = Dataset.from_payload({"token": "d1", "name": "Revenue"})
    route = respx.get(f"{API}/acme/datasets/d1").mock(
        return_value=httpx.Response(200, json={"token": "d1"})
    )
    with client() as mode:
        assert mode.datasets.get(dataset).token == "d1"
    assert route.called


def test_a_model_without_a_token_is_named_in_the_error_rather_than_404ing():
    """Mode omits keys per endpoint, so ``token`` is optional on every model. Passing one
    that lost it used to build ``/datasets//runs`` and get a 404 about nothing.
    """
    with client() as mode:
        with pytest.raises(ValueError, match="dataset= was passed a Dataset"):
            mode.datasets.get(Dataset.from_payload({"name": "Revenue"}))


@respx.mock
def test_per_page_and_extra_params_reach_the_query_string():
    """Both dataset listings ignore ``per_page`` server-side -- sending it is free where it
    works and the walk absorbs it where it does not.
    """
    route = respx.get(f"{API}/acme/datasets/d1/runs").mock(
        return_value=two_lists("report_runs", {"token": "run1"})
    )
    with client() as mode:
        mode.dataset_runs.list("d1", per_page=3, extra_params={"state": "succeeded"}).first_page()

    assert dict(route.calls[0].request.url.params) == {
        "state": "succeeded",
        "per_page": "3",
        "page": "1",
    }


@respx.mock
def test_a_field_description_write_carries_no_wrapper_at_all():
    """The exception to the house rule, and Mode's own ``_forms.create`` is the evidence:
    it declares exactly ``name`` and ``desc``, unwrapped.
    """
    route = respx.post(f"{API}/acme/datasets/d1/field_descriptions").mock(
        return_value=httpx.Response(200, json={"token": "fd1"})
    )
    with client() as mode:
        mode.dataset_fields.create("d1", "region", "<p>Kanto</p>")

    assert sent(route) == {"name": "region", "desc": "<p>Kanto</p>"}


@respx.mock
def test_deleting_a_dataset_answers_with_nothing():
    respx.delete(f"{API}/acme/datasets/d1").mock(return_value=httpx.Response(204))
    with client() as mode:
        assert mode.datasets.delete("d1") is None
