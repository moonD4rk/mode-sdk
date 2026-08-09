# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Unofficial Python SDK for the Mode Analytics REST and Discovery APIs. Python 3.11+, `src/` layout, single runtime dependency (`httpx`). Everything runs through uv (`uv_build` backend).

## Commands

```sh
uv sync                                        # create/update the venv from uv.lock
uv run pytest                                  # full suite — offline, respx-mocked, no network
uv run pytest tests/test_reports.py            # one file
uv run pytest tests/test_reports.py::test_x    # one test
uv run ruff format                             # format
uv run ruff check --fix                        # lint: E, F, I, B, UP, RUF, ANN; line length 100
uv run pyright                                 # strict, src/ only
```

Imports are one per line (`force-single-line`). CI runs ruff and pyright on 3.11, then pytest across 3.11–3.14.

## Code style

No redundant comments — write self-documenting code with clear names. Comment only to record a constraint the code cannot express (why, not what), one short line. Docstrings are one line, plus at most a line or two where a caller would otherwise hit a trap the signature cannot show. No measurement logs, no dates, no design history.

## Architecture

`Mode` (client.py) owns one httpx pool and exposes resource namespaces (`mode.reports`, `mode.spaces`, …) implemented in `resources/`, one module per API area on a shared `_base.py`. Identifier arguments accept either a model object or its 12-character token string.

Layers below the namespaces:

- `_transport.py` / `_policy.py` — request execution and retry policy (full-jitter backoff; POST is retried only when the failure was connect-phase). All failures surface as `ModeError` subclasses from `errors.py`; httpx exceptions never escape.
- `models.py` — frozen slotted dataclasses. Every field is optional, unmodelled keys stay in `.raw`, and numeric-looking ids are deliberately **strings** — never coerce them to int.
- `pagination.py` — lazy `Page` hiding three envelope shapes: `page`/`per_page` REST collections, the audit-log `next_token` cursor, and Discovery `_links.next_page`.
- `discovery.py` — the separate read-only Discovery batch API (own credential, Enterprise plan only).

## Tests

`tests/test_*.py` is offline only: fixtures are hand-written HAL payloads and every request is respx-mocked. Do not add network calls, real credentials, or identifiers from a real workspace — fixture tokens, ids and names are synthetic and must stay that way.
