"""Exceptions raised by this package.

Mode's ``{"id", "message"}`` error bodies map to named exceptions, so branching never
needs ``.status_code``. No httpx exception crosses this boundary; the original stays on
``__cause__``. Messages carry the request path and never the query string, which on
audit-log filters holds usernames, emails and IP addresses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .models import DatasetRun
    from .models import ReportRun


def _describe(
    status_code: int,
    message: str,
    error_id: str | None,
    method: str | None,
    path: str | None,
) -> str:
    where = f"{method} {path} -> " if method and path else ""
    label = f"HTTP {status_code} {error_id}" if error_id else f"HTTP {status_code}"
    return f"{where}{label}: {message}" if message else f"{where}{label}"


class ModeError(Exception):
    """Base class for every error this package raises."""


class ModeAPIError(ModeError):
    """Mode returned an HTTP error response."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        error_id: str | None = None,
        body: object | None = None,
        response: httpx.Response | None = None,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.error_id = error_id
        self.body = body
        self.response = response
        self.method = method
        self.path = path.split("?", 1)[0].split("#", 1)[0] if path else path
        super().__init__(_describe(status_code, message, error_id, method, self.path))

    @property
    def request(self) -> httpx.Request | None:
        if self.response is None:
            return None
        try:
            return self.response.request
        except RuntimeError:
            return None


class BadRequestError(ModeAPIError):
    """400 -- malformed query parameters or request body."""


class AuthenticationError(ModeAPIError):
    """401 -- missing or wrong API token/secret."""


class PermissionDeniedError(ModeAPIError):
    """403 -- authenticated, but the token lacks the privilege."""


class NotFoundError(ModeAPIError):
    """404 -- the resource, or something it depends on, does not exist."""


class ConflictError(ModeAPIError):
    """409 -- the request fought with the resource's current state."""


class UnprocessableEntityError(ModeAPIError):
    """422 -- well-formed, but Mode refused the contents."""


class RateLimitError(ModeAPIError):
    """429 -- too many requests; retry_after is the server's hint in seconds."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        error_id: str | None = None,
        body: object | None = None,
        response: httpx.Response | None = None,
        method: str | None = None,
        path: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(
            status_code,
            message,
            error_id=error_id,
            body=body,
            response=response,
            method=method,
            path=path,
        )


class InternalServerError(ModeAPIError):
    """5xx -- Mode failed to handle an otherwise valid request."""


class ModeConnectionError(ModeError):
    """The request never got an answer: DNS, TLS, reset, refused."""


class ModeTimeoutError(ModeConnectionError):
    """The answer ran out of time."""


class RunTimeoutError(ModeError):
    """A run was still going when the local deadline passed.

    The run keeps executing in Mode. ``run`` carries the last snapshot, so resuming the
    poll needs no re-fetch.
    """

    def __init__(
        self,
        message: str,
        *,
        run: ReportRun | DatasetRun | None = None,
        token: str | None = None,
        state: str | None = None,
    ) -> None:
        self.run = run
        self.token = token if token is not None else (run.token if run else None)
        self.state = state if state is not None else (run.state if run else None)
        super().__init__(message)


class DiscoveryUnavailableError(ModeError):
    """The Discovery API refused the workspace; it requires a Mode Enterprise plan."""
