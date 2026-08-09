"""Everything the transport decides, with nothing the transport does.

Pure functions over plain values: no sockets, no clock, no sleeping. Jitter and "now"
arrive as parameters so a test never has to arrange either.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .errors import AuthenticationError
from .errors import BadRequestError
from .errors import ConflictError
from .errors import InternalServerError
from .errors import ModeAPIError
from .errors import ModeConnectionError
from .errors import ModeTimeoutError
from .errors import NotFoundError
from .errors import PermissionDeniedError
from .errors import RateLimitError
from .errors import UnprocessableEntityError

MAX_BACKOFF = 30.0
RETRY_AFTER_CAP = 60.0
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

# PATCH is not idempotent per RFC 9110, but every Mode PATCH is a set-style field
# update, so a replay converges.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "PATCH"})

CONNECT_PHASE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
POST_CONNECT_ERRORS = (
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)

# All four httpx exception roots. InvalidURL, CookieConflict and StreamError are not
# HTTPErrors, so catching HTTPError alone would let them escape.
HTTPX_ERRORS = (httpx.HTTPError, httpx.InvalidURL, httpx.CookieConflict, httpx.StreamError)

Rand = Callable[[float, float], float]

_BY_ID: dict[str, type[ModeAPIError]] = {
    "bad_request": BadRequestError,
    "unauthorized": AuthenticationError,
    "forbidden": PermissionDeniedError,
    "not_found": NotFoundError,
}

_BY_STATUS: dict[int, type[ModeAPIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
    429: RateLimitError,
}


def is_idempotent(method: str) -> bool:
    return method.upper() in IDEMPOTENT_METHODS


def should_retry_exception(exc: BaseException, *, method: str, retry: bool | None) -> bool:
    """A connect-phase failure never reached Mode, so replaying it is safe for any
    method. A post-connect failure may already have been processed, so the method has to
    absorb a second copy. An unlisted transport error counts as post-connect.
    """
    if retry is False or not isinstance(exc, httpx.TransportError):
        return False
    if isinstance(exc, CONNECT_PHASE_ERRORS):
        return True
    return retry is True or is_idempotent(method)


def should_retry_status(status: int, *, method: str, retry: bool | None) -> bool:
    """409 is absent from RETRY_STATUSES: a conflict does not resolve by replay."""
    if retry is False or status not in RETRY_STATUSES:
        return False
    return retry is True or is_idempotent(method)


def backoff_delay(attempt: int, backoff_factor: float, *, rand: Rand = random.uniform) -> float:
    """Full jitter, so concurrent clients do not converge on one retry schedule."""
    # min(attempt, 64) keeps 2.0**attempt from overflowing; max(0.0, ...) keeps the
    # window non-negative, which time.sleep requires.
    return rand(0.0, min(MAX_BACKOFF, max(0.0, backoff_factor) * 2.0 ** min(attempt, 64)))


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """RFC 9110 allows delta-seconds or an HTTP-date; both are accepted.

    A date in the past clamps to 0; anything unparseable answers None.
    """
    if value is None:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def retry_after(response: httpx.Response, *, now: datetime | None = None) -> float | None:
    return parse_retry_after(response.headers.get("retry-after"), now=now)


def exceeds_retry_after_cap(hint: float | None) -> bool:
    """Above the cap the retry loop stops and hands the hint back, rather than sleeping
    minutes inside a library call or retrying sooner than the server asked.
    """
    return hint is not None and hint > RETRY_AFTER_CAP


def scoped_path(workspace: str, path: str, *, workspace_scoped: bool = True) -> str:
    """Apply the workspace prefix Mode's paths carry. ``/verify`` and HAL hrefs opt out."""
    if path and not path.startswith("/"):
        path = f"/{path}"
    if workspace_scoped:
        path = f"/{workspace}{path}"
    return path or "/"


def absolute_url(base_url: str, path: str) -> str:
    """Joined here rather than by httpx's base_url merge, so an injected client cannot
    redirect requests elsewhere.
    """
    if path.startswith(("http://", "https://")):
        return path
    return f"{base_url.rstrip('/')}{path}"


def safe_path(path: str) -> str:
    """A path with any query string removed.

    Everything reaching a log record or an exception message goes through here: query
    parameters carry audit-log filters, and those carry usernames, emails and IPs.
    """
    return path.split("?", 1)[0].split("#", 1)[0]


def embedded(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Pull a collection's items out of whichever envelope Mode used.

    Falls back to the sole list under ``_embedded`` when the expected key is absent,
    since Mode does not document the embed key for every collection.
    """
    section = payload.get("_embedded")
    if isinstance(section, dict):
        if isinstance(section.get(key), list):
            return section[key]
        lists = [value for value in section.values() if isinstance(value, list)]
        if len(lists) == 1:
            return lists[0]
    value = payload.get(key)
    return value if isinstance(value, list) else []


def connection_error(exc: Exception, *, method: str, path: str) -> ModeConnectionError:
    """Every HTTPX_ERRORS member lands here, not just the transport ones."""
    cls = ModeTimeoutError if isinstance(exc, httpx.TimeoutException) else ModeConnectionError
    return cls(f"{method} {safe_path(path)}: {type(exc).__name__}: {exc}")


def non_json_error(
    response: httpx.Response, *, method: str | None = None, path: str | None = None
) -> ModeAPIError:
    """A 2xx whose body is not JSON, most often a proxy's login page answering 200.

    Raised in place of ``json.JSONDecodeError``, which carries the whole undecoded body
    on ``.doc``; here the body is reachable only through ``.response``.
    """
    method, path = _named_by(response, method, path)
    media = response.headers.get("content-type", "").split(";")[0].strip()
    return ModeAPIError(
        response.status_code,
        f"expected a JSON body, got {media or 'no content type'}",
        response=response,
        method=method,
        path=path,
    )


def _named_by(
    response: httpx.Response, method: str | None, path: str | None
) -> tuple[str | None, str | None]:
    """The transport passes a path it already sanitised; anyone else gets the request's
    own, which a hand-built Response may not have.
    """
    if method is not None and path is not None:
        return method, path
    try:
        request = response.request
    except RuntimeError:
        return method, path
    return method or request.method, path or request.url.path


def error_from_response(
    response: httpx.Response, *, method: str | None = None, path: str | None = None
) -> ModeAPIError:
    """Build the most specific exception the response supports.

    Mode's ``id`` wins over the numeric status; an unrecognised 4xx stays a bare
    ModeAPIError rather than being forced into a class that would misname it.
    """
    method, path = _named_by(response, method, path)
    try:
        body: object | None = response.json()
    except ValueError:
        body = None
    error_id: str | None = None
    message = ""
    if isinstance(body, dict):
        # A Rails validation failure nests a dict under "message"; only a string is one.
        identifier, stated = body.get("id"), body.get("message")
        error_id = identifier if isinstance(identifier, str) else None
        message = stated if isinstance(stated, str) else ""
    if not message:
        message = " ".join(response.text.split())[:300] or response.reason_phrase

    status = response.status_code
    cls = _BY_ID.get(error_id or "") or _BY_STATUS.get(status)
    if cls is None:
        cls = InternalServerError if status >= 500 else ModeAPIError
    if cls is RateLimitError:
        return RateLimitError(
            status,
            message,
            error_id=error_id,
            body=body,
            response=response,
            method=method,
            path=path,
            retry_after=retry_after(response),
        )
    return cls(
        status, message, error_id=error_id, body=body, response=response, method=method, path=path
    )
