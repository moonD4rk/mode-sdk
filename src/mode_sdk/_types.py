"""Types shared across layers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeAlias

QueryPrimitive: TypeAlias = str | int | float | bool | None

#: Mirrors the value type of httpx's ``QueryParamTypes``. Write bodies keep
#: ``Mapping[str, object]`` instead: JSON nests, query strings do not.
QueryValue: TypeAlias = QueryPrimitive | Sequence[QueryPrimitive]
