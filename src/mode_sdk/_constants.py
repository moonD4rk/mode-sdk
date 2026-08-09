"""Literals more than one module needs."""

from __future__ import annotations

from importlib import metadata

BASE_URL = "https://app.mode.com/api"
WEB_URL = "https://app.mode.com"


def _version() -> str:
    try:
        return metadata.version("mode-sdk")
    except metadata.PackageNotFoundError:
        return "0.0.0"


VERSION = _version()
