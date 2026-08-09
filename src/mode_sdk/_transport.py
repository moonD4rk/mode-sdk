"""The one place that touches httpx, the clock and the retry loop. Rules live in _policy.

Retries cover connection failures, 408, 429 and 5xx. POST is excluded by default: a
retried ``POST /reports/{r}/runs`` starts a second run and bills the warehouse twice.
Credentials are applied per request, so an injected ``http_client`` keeps the SDK's auth.

The module is private, but ``request``, ``payload``, ``text`` and ``content`` -- with
their ``retry=`` and ``workspace=`` parameters -- are the public escape hatch for anyone
who needs a status line, a header or raw bytes.
"""

from __future__ import annotations

import copy
import logging
import ssl
import sys
import time
from collections.abc import Mapping
from typing import Any

import httpx

from . import _policy
from ._config import refuse_ignored_options
from ._constants import BASE_URL
from ._constants import VERSION
from ._types import QueryValue

DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
USER_AGENT = (
    f"mode-sdk/{VERSION} "
    f"python/{sys.version_info.major}.{sys.version_info.minor} "
    f"httpx/{httpx.__version__}"
)

_log = logging.getLogger("mode_sdk.transport")
_lifecycle = logging.getLogger("mode_sdk")


class Transport:
    def __init__(
        self,
        workspace: str,
        token: str | None = None,
        secret: str | None = None,
        *,
        base_url: str | None = None,
        accept: str = "application/hal+json",
        auth_headers: Mapping[str, str] | None = None,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        headers: Mapping[str, str] | None = None,
        limits: httpx.Limits | None = None,
        http2: bool | None = None,
        verify: bool | ssl.SSLContext | None = None,
        proxy: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        if backoff_factor < 0:
            raise ValueError(f"backoff_factor= must not be negative, got {backoff_factor!r}")
        if http_client is not None:
            refuse_ignored_options(
                [
                    ("timeout", timeout is not None),
                    ("headers", headers is not None),
                    ("limits", limits is not None),
                    ("http2", http2 is not None),
                    ("verify", verify is not None),
                    ("proxy", proxy is not None),
                ]
            )

        self.workspace = workspace
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._auth = httpx.BasicAuth(token or "", secret or "") if token is not None else None
        # httpx.Headers, not a dict: a caller's "user-agent" must replace the SDK's
        # "User-Agent" rather than send both.
        self._headers = httpx.Headers({"Accept": accept, "User-Agent": USER_AGENT})
        self._headers.update(headers or {})
        self._headers.update(auth_headers or {})
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            headers=self._headers,
            timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
            follow_redirects=True,
            **_client_options(limits, http2, verify, proxy),
        )
        # Sent per request rather than left on the client, so a copy sharing this pool
        # can carry a different deadline.
        self._timeout: float | httpx.Timeout = self._http.timeout
        _lifecycle.debug("client created workspace=%s owns_pool=%s", workspace, self._owns_client)

    @property
    def is_closed(self) -> bool:
        return self._http.is_closed

    def with_options(
        self,
        *,
        timeout: float | httpx.Timeout | None = None,
        max_retries: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Transport:
        """A second view of the same pool, with its own deadline, budget and headers.

        The copy never owns the pool: closing it is a no-op, and closing the original
        closes every view.
        """
        clone = copy.copy(self)
        clone._owns_client = False
        if timeout is not None:
            clone._timeout = timeout
        if max_retries is not None:
            clone.max_retries = max_retries
        if headers is not None:
            clone._headers = httpx.Headers(self._headers)
            clone._headers.update(headers)
        return clone

    def close(self) -> None:
        if self._owns_client:
            self._http.close()
        _lifecycle.debug(
            "client closed workspace=%s owns_pool=%s", self.workspace, self._owns_client
        )

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool | None = None,
        workspace: bool = True,
    ) -> httpx.Response:
        method = method.upper()
        scoped = _policy.scoped_path(self.workspace, path, workspace_scoped=workspace)
        url = _policy.absolute_url(self.base_url, scoped)
        # Everything logged or raised below uses this, never the URL: query parameters
        # carry audit-log filters, and those carry people's names.
        where = _policy.safe_path(scoped)
        sent = httpx.Headers(self._headers)
        sent.update(headers or {})
        auth = self._auth if self._auth is not None else httpx.USE_CLIENT_DEFAULT

        attempt = 0
        while True:
            _log.debug("%s %s attempt %d", method, where, attempt + 1)
            started = time.monotonic()
            try:
                response = self._http.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=sent,
                    auth=auth,
                    timeout=self._timeout,
                )
            except _policy.HTTPX_ERRORS as exc:
                retrying = _policy.should_retry_exception(exc, method=method, retry=retry)
                if not retrying or attempt >= self.max_retries:
                    raise _policy.connection_error(exc, method=method, path=where) from exc
                delay = _policy.backoff_delay(attempt, self.backoff_factor)
                self._warn_retry(method, where, type(exc).__name__, delay, attempt)
                time.sleep(delay)
                attempt += 1
                continue

            _log.debug(
                "%s %s -> %d in %.0fms attempt %d",
                method,
                where,
                response.status_code,
                (time.monotonic() - started) * 1000,
                attempt + 1,
            )

            status = response.status_code
            if attempt < self.max_retries and _policy.should_retry_status(
                status, method=method, retry=retry
            ):
                hint = _policy.retry_after(response)
                if _policy.exceeds_retry_after_cap(hint):
                    _log.warning(
                        "%s %s asked for %.0fs, above the %.0fs cap; not retrying",
                        method,
                        where,
                        hint,
                        _policy.RETRY_AFTER_CAP,
                    )
                else:
                    delay = (
                        hint
                        if hint is not None
                        else _policy.backoff_delay(attempt, self.backoff_factor)
                    )
                    self._warn_retry(method, where, str(status), delay, attempt)
                    time.sleep(delay)
                    attempt += 1
                    continue

            if response.is_error:
                raise _policy.error_from_response(response, method=method, path=where)
            return response

    def payload(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool | None = None,
        workspace: bool = True,
    ) -> dict[str, Any]:
        response = self.request(
            method,
            path,
            params=params,
            json=json,
            headers=headers,
            retry=retry,
            workspace=workspace,
        )
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            scoped = _policy.scoped_path(self.workspace, path, workspace_scoped=workspace)
            raise _policy.non_json_error(
                response, method=method.upper(), path=_policy.safe_path(scoped)
            ) from exc
        return body if isinstance(body, dict) else {"_value": body}

    def text(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool | None = None,
        workspace: bool = True,
    ) -> str:
        return self.request(
            method,
            path,
            params=params,
            json=json,
            headers=headers,
            retry=retry,
            workspace=workspace,
        ).text

    def content(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, QueryValue] | None = None,
        json: object | None = None,
        headers: Mapping[str, str] | None = None,
        retry: bool | None = None,
        workspace: bool = True,
    ) -> bytes:
        return self.request(
            method,
            path,
            params=params,
            json=json,
            headers=headers,
            retry=retry,
            workspace=workspace,
        ).content

    def _warn_retry(self, method: str, path: str, reason: str, delay: float, attempt: int) -> None:
        _log.warning(
            "retrying %s %s after %s in %.2fs (attempt %d/%d)",
            method,
            path,
            reason,
            delay,
            attempt + 1,
            self.max_retries,
        )


def _client_options(
    limits: httpx.Limits | None,
    http2: bool | None,
    verify: bool | ssl.SSLContext | None,
    proxy: str | None,
) -> dict[str, Any]:
    """Omit rather than pass None: httpx reads ``Limits()`` as unlimited, not as its
    default pool.
    """
    options: dict[str, Any] = {}
    if limits is not None:
        options["limits"] = limits
    if http2 is not None:
        options["http2"] = http2
    if verify is not None:
        options["verify"] = verify
    if proxy is not None:
        options["proxy"] = proxy
    return options
