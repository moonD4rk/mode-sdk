# mode-sdk

Unofficial Python SDK for the [Mode Analytics API](https://mode.com/developer/api-reference/introduction/) — the documented REST surface plus the Discovery batch API. Not affiliated with Mode or ThoughtSpot.

- Python 3.11+, one runtime dependency (`httpx`), fully typed
- Lazy pagination, mapped errors, frozen dataclass models, retries with backoff
- Requires a [Mode Business workspace](https://mode.com/help/articles/organizations/#api-tokens) — only resources inside one are reachable over the API

## Install

```sh
uv add mode-sdk          # or: pip install mode-sdk
```

## Quickstart

```python
from mode_sdk import Mode

# Explicit, or from MODE_WORKSPACE / MODE_API_TOKEN / MODE_API_SECRET
with Mode("acme", token=token, secret=secret) as mode:
    mode.verify()                                    # cheapest credential check
    for space in mode.spaces.list(filter="all"):     # default lists only *your* collections
        for report in mode.reports.list(space=space):
            print(report.token, report.name, report.created_at)
```

Credentials are keyword-only; each argument falls back to its environment variable. Every identifier argument takes the model a previous call returned, or its 12-character token string — passing the model is the safer spelling.

## Examples

### Run a report and read the results

```python
run = mode.reports.run_and_wait(report, parameters={"country": "US"})
if run.succeeded:
    for filename, csv in mode.report_runs.results_tables(report, run).items():
        print(filename, len(csv))
else:
    print(mode.report_runs.failure_detail(report, run))
```

The shape of an export is Mode's choice, not the report's: the same path answers `text/csv` for one report and `application/zip` for another. `results_tables()` returns `{filename: csv}` either way; `results()` returns a `RunResults` when you want the raw bytes and their `media_type`.

### Save results to disk, or render a PDF

```python
mode.report_runs.results(report, run).save("./exports")   # a directory keeps Mode's filename
mode.exports.pdf(report, run).save("report.pdf")          # starts and polls the async render
```

### Create a report

```python
from mode_sdk import query_spec

report = mode.reports.create(space, "revenue", [query_spec(sql, data_source_id)])
```

Mode documents no create-report endpoint, so this is three requests under the hood; a failure part-way deletes what was minted rather than leaving an unnamed report behind.

### Handle errors

```python
from mode_sdk import ModeError, NotFoundError, RateLimitError

try:
    mode.reports.get(token)
except NotFoundError:
    ...
except RateLimitError as exc:
    print(exc.retry_after)        # the server's hint, in seconds
except ModeError:                 # the root: everything this package raises
    raise
```

`ModeAPIError` subclasses map Mode's error bodies — `BadRequestError`, `AuthenticationError`, `PermissionDeniedError`, `ConflictError`, `UnprocessableEntityError`, `InternalServerError`. `ModeConnectionError` and `ModeTimeoutError` mean Mode never answered. httpx exceptions never escape.

### Pagination

```python
page = mode.reports.list(space=space)
for report in page:               # lazily walks every page
    ...
page.first_page()                 # one request, one list
list(page)                        # everything, eagerly
```

### One client, many threads

```python
patient = mode.with_options(timeout=600.0, max_retries=0)
patient.report_runs.results(report, run)      # same connection pool, longer deadline
```

Build one `Mode` and share it across threads: `httpx.Client` is thread-safe and models are frozen. `Page` objects are the exception — each belongs to the thread walking it.

### Call an endpoint this package does not model

```python
mode.request("GET", "/some/new/endpoint")     # workspace-relative, returns the raw dict
```

### Discovery API

```python
from mode_sdk import Discovery, create_signature_token

signature = create_signature_token(
    "acme", token=token, secret=secret, name="etl-reader", expires_at="2027-01-01T00:00Z"
)
# store signature.token / access_key / access_secret — the secret is returned exactly once

with Discovery("acme", signature=signature) as discovery:
    for report in discovery.reports(include_spaces="all"):
        print(report.token, report.name)
```

Read-only batch listings (`reports`, `report_stats`, `queries`, `charts`, `collections`, `members`) with a separate credential. Requires a Mode Enterprise plan; refusals raise `DiscoveryUnavailableError`.

## What it covers

| Namespace | Endpoints |
| --- | --- |
| `mode.workspace` | verify, workspace, account |
| `mode.spaces` | Collections: get, list, create, update, delete, reports, datasets |
| `mode.space_memberships` | list, get, add, remove _(deprecated by Mode)_ |
| `mode.reports` | get, list, create, update, delete, archive, unarchive, purge, run, run_and_wait |
| `mode.report_runs` | list, get, create, clone, duplicate, duplication_status, form_fields, results, results_tables, wait, create_and_wait, failure_detail |
| `mode.report_filters` | list, get, create, update, delete |
| `mode.queries` | list, get, create, update, delete |
| `mode.query_runs` | list, get, results |
| `mode.charts` | list, get |
| `mode.definitions` | list, get, create, update, delete |
| `mode.data_sources` | list, get, update, refresh_schema, purge |
| `mode.datasets` | get, list, update, delete, reports, fields, refresh_in_report |
| `mode.dataset_runs` | list, get, create |
| `mode.dataset_fields` | list, create, update, delete |
| `mode.memberships` | list, get, remove |
| `mode.invites` | create |
| `mode.groups` | list, get, create, update, delete, memberships, add_member, remove_member |
| `mode.audit_logs` | list |
| `mode.exports` | report_run, report_run_tables, query_run, pdf, from_href |
| `mode.report_schedules` | list, get, create, update, delete |
| `mode.report_subscriptions` | list, get, create, update, delete |
| `mode.dataset_schedules` | list |

## Good to know

- **Numeric-looking ids are strings** (`report.id == "5747815"`), and they do not route: Mode's paths take the 12-character `token`, and the numeric id answers 404. Nothing is coerced.
- **Every model field is optional.** Mode's key set varies by object type and endpoint; unmodelled keys stay in `model.raw`, and an unparseable timestamp leaves the attribute `None` with the original string in `raw`.
- **`POST` is not retried by default** — a replayed run-create starts a second run and bills the warehouse twice. Connection failures and 408/429/5xx on idempotent methods retry with full-jitter backoff; opt in per request with `mode.transport.request(..., retry=True)`.
- **A `Retry-After` above 60 seconds stops the retry loop** instead of sleeping minutes inside a library call; the hint comes back on `RateLimitError.retry_after`.
- **`per_page` is capped at 30** by Mode and ignored entirely by several collections; the page walk absorbs both and still returns the whole collection.
- **Logging** uses the standard library: `logging.getLogger("mode_sdk").setLevel(logging.DEBUG)`. Header values, bodies, query strings, tokens and secrets are never logged at any level.
- **Bring your own HTTP client** with `Mode(..., http_client=httpx.Client(...))`. That hands you HTTP policy, so combining it with `timeout`, `proxy`, `verify` and friends raises rather than being silently ignored.

## Development

```sh
uv sync                  # create the venv from uv.lock
uv run pytest            # offline suite — no network, no credentials
uv run ruff format && uv run ruff check --fix
uv run pyright           # strict
```

Fixtures are hand-written payloads and every request is mocked; tests must stay offline, with synthetic tokens and names only.

## Licence

Apache-2.0
