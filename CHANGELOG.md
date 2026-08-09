# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-09

First public release.

### Added

#### Client

- `Mode` client: one shared `httpx` connection pool, thread-safe by design, with 22 resource namespaces covering the documented REST surface — reports, report runs, queries, query runs, charts, Collections, datasets (runs, fields, field descriptions), data sources, definitions, report filters, schedules, subscriptions, memberships, groups, invites, audit logs and exports.
- Credentials from arguments or `MODE_WORKSPACE` / `MODE_API_TOKEN` / `MODE_API_SECRET`, keyword-only so a token and secret cannot be transposed; `mode.verify()` as the cheapest credential check.
- `with_options()` for per-call-site overrides (timeout, retries, headers) as shallow copies sharing the same pool.
- `mode.request()` escape hatch for endpoints not yet modelled; `mode.transport` for raw responses, with per-request `retry=` and `workspace=` control.
- Bring-your-own `httpx.Client` via `http_client=`; combining it with pool options it would override raises instead of being silently ignored.

#### Runs and results

- `reports.run_and_wait()` / `report_runs.wait()` poll runs to a terminal state with adaptive, jittered intervals and a wall-clock deadline; `RunTimeoutError` carries the last snapshot so polling can resume.
- `report_runs.failure_detail()` surfaces the warehouse's real error message from the query runs beneath a failed report run.
- Transactional `reports.create()`: Mode documents no create-report endpoint, so the three underlying requests are composed with rollback — a failure part-way deletes the minted token instead of leaving an unnamed report behind.
- `RunResults` carries export bytes together with their Content-Type; `tables()` returns `{filename: csv}` whether Mode answered `text/csv` or `application/zip`; `save()` writes bytes keeping Mode's own filename; `exports.pdf()` drives the two-step asynchronous PDF render to completion.

#### Discovery

- `Discovery` client for the read-only batch API (Mode Enterprise): `reports`, `report_stats`, `queries`, `charts`, `collections`, `members`.
- `create_signature_token()` mints the Discovery credential explicitly; plan and permission refusals surface as `DiscoveryUnavailableError`.

#### Foundations

- Lazy `Page` pagination over Mode's three envelope shapes — `page`/`per_page` collections, the audit-log `next_token` cursor, Discovery's `_links.next_page` — with duplicate-page guards for the collections that ignore paging parameters, so a walk always terminates and always returns the whole collection.
- Frozen slotted dataclass models: every field optional, unmodelled keys kept in `.raw`, timestamps parsed defensively, and numeric-looking ids kept as strings because Mode routes by the 12-character token.
- Named exceptions mapped from Mode's `{"id", "message"}` error bodies under one `ModeError` root (`NotFoundError`, `RateLimitError` with `retry_after`, …); `httpx` exceptions never escape. Messages carry the request path and never the query string, which on audit-log filters holds usernames, emails and IP addresses.
- Full-jitter retry policy: connection failures and 408/429/5xx, both `Retry-After` forms honoured, hints above 60 seconds handed back instead of slept on, and `POST` replayed only when the failure was provably connect-phase.
- Standard-library `logging` on two loggers; header values, bodies, query strings, tokens and secrets are never logged at any level.

[Unreleased]: https://github.com/moonD4rk/mode-sdk/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/moonD4rk/mode-sdk/releases/tag/v0.1.0
