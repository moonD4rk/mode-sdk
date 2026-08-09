"""Datasets: reports promoted to reusable sources, plus their runs and field docs.

A Dataset is a Report underneath, and Mode's envelopes say so: writes are wrapped in
``report``, listings arrive under ``_embedded.reports``, runs under ``report_runs`` and
schedules under ``report_schedules``. The guessable spellings do not exist -- asking for
them survives only on ``_policy.embedded()``'s sole-list fallback, which stops firing the
day Mode embeds a second list.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from .._types import QueryValue
from ..models import Dataset
from ..models import DatasetField
from ..models import DatasetRun
from ..models import FieldDescription
from ..models import Report
from ..pagination import Page
from ._args import DatasetRef
from ._args import DatasetRunRef
from ._args import DataSourceRef
from ._args import FieldDescriptionRef
from ._args import ReportRef
from ._args import SpaceRef
from ._args import data_source_token
from ._args import token_of
from ._args import write_body
from ._base import Resource


class DatasetsResource(Resource):
    def get(self, dataset: DatasetRef) -> Dataset:
        """One dataset in full -- except ``description``, which only listings carry, so
        re-fetching to enrich a listing loses it.
        """
        return self._one(Dataset, "GET", f"/datasets/{token_of(dataset, 'dataset')}")

    def list(
        self,
        *,
        space: SpaceRef | None = None,
        data_source: DataSourceRef | None = None,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[Dataset]:
        """Datasets of one Collection or one data source; Mode has no workspace-wide list.

        ``per_page`` is accepted and ignored by both listings, as is ``archived`` -- there
        is no way to list an archived dataset through this API.
        """
        if space is not None:
            path = f"/spaces/{token_of(space, 'space')}/datasets"
        elif data_source is not None:
            path = f"/data_sources/{data_source_token(data_source)}/datasets"
        else:
            raise ValueError("pass space= or data_source=; Mode cannot list all datasets at once")
        return self._many(Dataset, path, "reports", per_page=per_page, extra_params=extra_params)

    def update(
        self,
        dataset: DatasetRef,
        *,
        name: str | None = None,
        description: str | None = None,
        space_token: SpaceRef | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> Dataset:
        """Rename, re-describe or move a dataset. The payload is wrapped in ``report``,
        not ``dataset``, because a Dataset is a Report with ``type="DatasetReport"``.
        An omitted keyword is left as it was rather than nulled.
        """
        payload = write_body(
            "report",
            extra_body,
            name=name,
            description=description,
            space_token=None if space_token is None else token_of(space_token, "space_token"),
        )
        return self._one(
            Dataset, "PATCH", f"/datasets/{token_of(dataset, 'dataset')}", json=payload
        )

    def delete(self, dataset: DatasetRef) -> None:
        self._t.request("DELETE", f"/datasets/{token_of(dataset, 'dataset')}")

    def reports(
        self,
        dataset: DatasetRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[Report]:
        """The reports built on this dataset, as a projection of ``token``, ``name``,
        ``creator`` and ``_links`` -- every other ``Report`` field is ``None``. Re-fetch
        through ``mode.reports.get`` when more is needed.
        """
        return self._many(
            Report,
            f"/datasets/{token_of(dataset, 'dataset')}/reports",
            "reports",
            per_page=per_page,
            extra_params=extra_params,
        )

    def fields(
        self,
        dataset: DatasetRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[DatasetField]:
        """The dataset's columns: ``name`` and ``type``, and nothing else. Field prose is
        a different collection -- ``mode.dataset_fields``, at ``/field_descriptions``.
        """
        return self._many(
            DatasetField,
            f"/datasets/{token_of(dataset, 'dataset')}/fields",
            "fields",
            per_page=per_page,
            extra_params=extra_params,
        )

    def refresh_in_report(
        self, report: ReportRef, dataset_tokens: Sequence[DatasetRef]
    ) -> dict[str, Any]:
        """Re-run a report against fresh copies of the datasets it embeds.

        Returns the raw payload because Mode documents no stable shape for it;
        ``mode.report_runs.create`` is the modelled way to start an ordinary run.
        """
        tokens = [{"token": token_of(t, "dataset_tokens")} for t in dataset_tokens]
        return self._t.payload(
            "POST",
            f"/reports/{token_of(report, 'report')}/runs",
            json={"report": {"dataset_tokens": tokens}},
        )


class DatasetRunsResource(Resource):
    """Runs of a dataset, which are report runs under ``_embedded.report_runs``.

    The one dataset collection that paginates honestly: it ships a full ``pagination``
    block and respects ``per_page``.
    """

    def list(
        self,
        dataset: DatasetRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[DatasetRun]:
        return self._many(
            DatasetRun,
            f"/datasets/{token_of(dataset, 'dataset')}/runs",
            "report_runs",
            per_page=per_page,
            extra_params=extra_params,
        )

    def get(self, dataset: DatasetRef, run: DatasetRunRef) -> DatasetRun:
        return self._one(
            DatasetRun,
            "GET",
            f"/datasets/{token_of(dataset, 'dataset')}/runs/{token_of(run, 'run')}",
        )

    def create(self, dataset: DatasetRef) -> DatasetRun:
        """Start a run. Answers immediately with a non-terminal run, so poll from here."""
        return self._one(DatasetRun, "POST", f"/datasets/{token_of(dataset, 'dataset')}/runs")


class FieldDescriptionsResource(Resource):
    """Prose attached to a dataset's columns.

    The write body is *not* wrapped -- ``{"name": ..., "desc": ...}`` goes up bare, which
    is the exception to the rule every other write in this package follows.
    """

    def list(
        self,
        dataset: DatasetRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[FieldDescription]:
        return self._many(
            FieldDescription,
            f"/datasets/{token_of(dataset, 'dataset')}/field_descriptions",
            "field_descriptions",
            per_page=per_page,
            extra_params=extra_params,
        )

    def create(self, dataset: DatasetRef, name: str, desc: str) -> FieldDescription:
        """``name`` is the column; ``desc`` is HTML in Mode's own UI (``<p>...</p>``)."""
        return self._one(
            FieldDescription,
            "POST",
            f"/datasets/{token_of(dataset, 'dataset')}/field_descriptions",
            json={"name": name, "desc": desc},
        )

    def update(
        self, dataset: DatasetRef, field_description: FieldDescriptionRef, desc: str
    ) -> FieldDescription:
        return self._one(
            FieldDescription,
            "PATCH",
            f"/datasets/{token_of(dataset, 'dataset')}/field_descriptions/"
            f"{token_of(field_description, 'field_description')}",
            json={"desc": desc},
        )

    def delete(self, dataset: DatasetRef, field_description: FieldDescriptionRef) -> None:
        self._t.request(
            "DELETE",
            f"/datasets/{token_of(dataset, 'dataset')}/field_descriptions/"
            f"{token_of(field_description, 'field_description')}",
        )
