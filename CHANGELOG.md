# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-08

### Added

- `Mode` client covering the documented REST surface: reports, runs, queries, charts, Collections, datasets, data sources, definitions, schedules, subscriptions, memberships, groups, invites and audit logs.
- `Discovery` client and `create_signature_token` for the read-only Discovery batch API.
- Lazy `Page` pagination over Mode's three envelope shapes (`page`/`per_page`, `next_token`, `_links.next_page`).
- Frozen dataclass models that keep unmodelled keys in `.raw`; numeric-looking ids stay strings because Mode routes by token.
- Mapped exceptions under a `ModeError` root; httpx exceptions never escape.
- Full-jitter retries honouring `Retry-After`; `POST` is retried only when the failure was connect-phase.

[Unreleased]: https://github.com/moonD4rk/mode-sdk/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/moonD4rk/mode-sdk/releases/tag/v0.1.0
