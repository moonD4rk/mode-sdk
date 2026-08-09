"""Definitions: the workspace's canonical SQL snippets, referenced as ``{{ @name }}``.

The collection ignores ``page`` and ``per_page``, so the duplicate-page guard in
``pagination`` is the only thing that ends a walk and every ``list()`` costs one extra
request.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence

from .._types import QueryValue
from ..models import Definition
from ..pagination import Page
from ._args import DefinitionRef
from ._args import token_of
from ._args import write_body
from ._base import Resource


class DefinitionsResource(Resource):
    def list(
        self,
        *,
        tokens: Sequence[str] | None = None,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[Definition]:
        """Select by token, or take the whole library. Mode implements no third option.

        ``tokens`` only selects alongside ``filter=by_tokens``, which is sent for you;
        there is no ``filter`` parameter here because every other value answers with the
        whole library, cancelling the tokens. ``extra_params`` carries one if Mode ever
        ships a second.

        ``tokens=[]`` selects nothing rather than everything. Selection fails open: an
        unknown token is dropped with a 200, so a short result is the only signal.
        """
        params: dict[str, QueryValue] = {}
        if tokens is not None:
            params["tokens"] = ",".join(tokens)
            params["filter"] = "by_tokens"
        return self._many(
            Definition,
            "/definitions",
            "definitions",
            params=params or None,
            per_page=per_page,
            extra_params=extra_params,
        )

    def get(self, definition: DefinitionRef) -> Definition:
        return self._one(Definition, "GET", f"/definitions/{token_of(definition, 'definition')}")

    def create(
        self,
        name: str,
        source: str,
        *,
        description: str | None = None,
        data_source_id: int | str | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> Definition:
        """Wrapped in ``definition``; an omitted keyword is not sent at all.

        ``data_source_id`` takes both types because Mode reads it back as a string while
        declaring it a JSON integer, so a value off a ``Definition`` needs no cast.
        """
        payload = write_body(
            "definition",
            extra_body,
            name=name,
            source=source,
            description=description,
            data_source_id=data_source_id,
        )
        return self._one(Definition, "POST", "/definitions", json=payload)

    def update(
        self,
        definition: DefinitionRef,
        *,
        name: str | None = None,
        source: str | None = None,
        description: str | None = None,
        data_source_id: int | str | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> Definition:
        """Wrapped in ``definition``; an omitted keyword is not sent, so no field is cleared.

        Do not build this payload from Mode's ``_forms.edit``:
        ``input.definition.data_source_id.value`` holds the *definition's* id, so a
        read-modify-write through the form reassigns the definition to a nonexistent source.
        """
        payload = write_body(
            "definition",
            extra_body,
            name=name,
            source=source,
            description=description,
            data_source_id=data_source_id,
        )
        return self._one(
            Definition, "PATCH", f"/definitions/{token_of(definition, 'definition')}", json=payload
        )

    def delete(self, definition: DefinitionRef) -> None:
        self._t.request("DELETE", f"/definitions/{token_of(definition, 'definition')}")
