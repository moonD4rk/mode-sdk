"""Shared plumbing for the resource namespaces hanging off a Mode client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from typing import TypeVar

from .._transport import Transport
from .._types import QueryValue
from ..models import Model
from ..pagination import Page
from ..pagination import offset_pages

T = TypeVar("T", bound=Model)


def body(wrapper: str, **values: object) -> dict[str, Any]:
    """Mode wraps write payloads in a resource-named object and rejects unknown nulls."""
    return {wrapper: {k: v for k, v in values.items() if v is not None}}


class Resource:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def _one(
        self,
        model: type[T],
        method: str,
        path: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool | None = None,
        workspace: bool = True,
    ) -> T:
        return model.from_payload(
            self._t.payload(
                method,
                path,
                params=params,
                json=json,
                headers=headers,
                retry=retry,
                workspace=workspace,
            )
        )

    def _many(
        self,
        model: type[T],
        path: str,
        key: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[T]:
        """``extra_params`` merges last and wins over any documented keyword.

        ``per_page`` is a request, not a promise: Mode caps it at ``MAX_PER_PAGE`` and
        some collections ignore it. The Page walks the whole collection either way.
        """
        query = dict(params or {}) | dict(extra_params or {})
        return Page(
            lambda: offset_pages(
                self._t, path, key, model.from_payload, params=query or None, per_page=per_page
            ),
            path,
        )
