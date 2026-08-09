"""Credential resolution and option conflicts, shared by both clients."""

from __future__ import annotations

import os
from collections.abc import Sequence

WORKSPACE_VAR = "MODE_WORKSPACE"
TOKEN_VAR = "MODE_API_TOKEN"
SECRET_VAR = "MODE_API_SECRET"


def resolve(value: str | None, variable: str) -> str | None:
    return value or os.environ.get(variable) or None


def required(value: str | None, name: str, variable: str) -> str:
    if not value:
        raise ValueError(f"{name} is required: pass {name}= or set {variable}")
    return value


def refuse_ignored_options(given: Sequence[tuple[str, bool]]) -> None:
    for name, passed in given:
        if passed:
            raise ValueError(
                f"http_client= owns HTTP policy, so {name}= would be ignored: "
                f"configure {name} on the client you pass, or drop http_client="
            )
