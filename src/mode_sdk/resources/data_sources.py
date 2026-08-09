"""Warehouse connections: connection settings only, no tables and no columns.

**Every path here wants the 12-character token, never the numeric ``DataSource.id``**,
which answers ``404 data source not found``. Listed is not the same as usable: a source
can appear in ``list()`` and still be unreachable to the key that listed it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .._types import QueryValue
from ..models import DataSource
from ..pagination import Page
from ._args import DataSourceRef
from ._args import data_source_token
from ._args import write_body
from ._base import Resource


class DataSourcesResource(Resource):
    def list(
        self,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[DataSource]:
        """The whole set, every time: ``page``, ``per_page`` and ``limit`` are ignored
        here, so the duplicate-page guard in ``pagination`` is what ends the walk.
        """
        return self._many(
            DataSource,
            "/data_sources",
            "data_sources",
            per_page=per_page,
            extra_params=extra_params,
        )

    def get(self, data_source: DataSourceRef) -> DataSource:
        return self._one(DataSource, "GET", f"/data_sources/{data_source_token(data_source)}")

    def update(
        self,
        data_source: DataSourceRef,
        *,
        name: str | None = None,
        description: str | None = None,
        password: str | None = None,
        custom_attributes: Mapping[str, object] | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> DataSource:
        """Wrapped in ``data_source``; an omitted keyword is not sent at all.

        These four come from Mode's REST documentation, not from its hypermedia:
        ``_forms.edit`` declares only ``data_source.tables[]``, which is reachable through
        ``extra_body={"tables": [...]}``.
        """
        payload = write_body(
            "data_source",
            extra_body,
            name=name,
            description=description,
            password=password,
            custom_attributes=dict(custom_attributes) if custom_attributes is not None else None,
        )
        return self._one(
            DataSource,
            "PATCH",
            f"/data_sources/{data_source_token(data_source)}",
            json=payload,
        )

    def refresh_schema(self, data_source: DataSourceRef) -> dict[str, Any]:
        """Mode documents no stable response shape, so it is handed back unmodelled."""
        return self._t.payload(
            "POST", f"/data_sources/{data_source_token(data_source)}/schema_updates"
        )

    def purge(self, data_source: DataSourceRef) -> dict[str, Any]:
        """Delete every stored report result for this connection. Answered with 202."""
        return self._t.payload("POST", f"/data_sources/{data_source_token(data_source)}/purge")
