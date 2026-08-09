"""The Discovery API: read-only batch listings of everything in a workspace.

A separate service with its own host path, pagination and credential -- a *signature
token* of token, access key and access secret, base64'd into a Bearer header.

Discovery requires a Mode Enterprise plan; minting additionally requires a
workspace-admin key. Both refusals (401 for the key, 403 for the plan) surface as
DiscoveryUnavailableError, since neither is fixed by retrying the same credential.
"""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime

import httpx

from ._config import SECRET_VAR
from ._config import TOKEN_VAR
from ._config import WORKSPACE_VAR
from ._config import refuse_ignored_options
from ._config import required
from ._config import resolve
from ._constants import WEB_URL
from ._transport import DEFAULT_TIMEOUT
from ._transport import Transport
from ._types import QueryValue
from .errors import AuthenticationError
from .errors import DiscoveryUnavailableError
from .errors import PermissionDeniedError
from .models import DiscoveryObject
from .models import SignatureToken
from .pagination import Batch
from .pagination import Page
from .pagination import link_pages


def bearer(token: str, access_key: str, access_secret: str) -> str:
    raw = f"{token}:{access_key}:{access_secret}".encode()
    return f"Bearer {b64encode(raw).decode()}"


def create_signature_token(
    workspace: str | None = None,
    *,
    token: str | None = None,
    secret: str | None = None,
    name: str,
    expires_at: datetime | str,
    authorization_type: str = "read-only",
    base_url: str = WEB_URL,
    timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
) -> SignatureToken:
    """Mint a Discovery credential. The access_secret is returned once and never again.

    Authenticates with the same workspace/token/secret triple as ``Mode``. ``name`` must
    be 4-64 characters; ``expires_at`` is ISO 8601.
    """
    workspace = required(resolve(workspace, WORKSPACE_VAR), "workspace", WORKSPACE_VAR)
    token = required(resolve(token, TOKEN_VAR), "token", TOKEN_VAR)
    secret = required(resolve(secret, SECRET_VAR), "secret", SECRET_VAR)
    if isinstance(expires_at, datetime):
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        expires_at = expires_at.isoformat()
    payload = {
        "signature_token": {
            "name": name,
            "expires_at": expires_at,
            "auth_scope": {
                "authentication_for": "batch-api",
                "authorization_type": authorization_type,
            },
        }
    }
    with Transport(
        f"batch/{workspace}",
        token,
        secret,
        base_url=base_url,
        timeout=timeout,
        accept="application/json",
    ) as transport:
        try:
            minted = transport.payload("POST", "/signature_tokens", json=payload)
        except (AuthenticationError, PermissionDeniedError) as exc:
            raise DiscoveryUnavailableError(
                f"workspace {workspace!r} cannot mint Discovery signature tokens "
                f"(HTTP {exc.status_code}); minting needs a workspace-admin key on a "
                "Mode Enterprise plan, so check the key's scope before the plan"
            ) from exc
    return SignatureToken.from_payload(minted)


class Discovery:
    """Read-only batch access to a workspace.

    signature = create_signature_token(
        "acme", token=key, secret=secret, name="etl", expires_at="2027-01-01T00:00Z"
    )
    with Discovery("acme", signature=signature) as discovery:
        for report in discovery.reports(include_spaces="all"):
            print(report.name)
    """

    def __init__(
        self,
        workspace: str | None = None,
        *,
        signature: SignatureToken | None = None,
        token: str | None = None,
        access_key: str | None = None,
        access_secret: str | None = None,
        base_url: str = WEB_URL,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        http_client: httpx.Client | None = None,
    ) -> None:
        owned = http_client is None
        if not owned:
            refuse_ignored_options([("timeout", timeout is not DEFAULT_TIMEOUT)])
        self.workspace_name = required(
            resolve(workspace, WORKSPACE_VAR), "workspace", WORKSPACE_VAR
        )
        if signature is not None:
            token = token or signature.token
            access_key = access_key or signature.access_key
            access_secret = access_secret or signature.access_secret

        if token and access_key and access_secret:
            auth_headers: dict[str, str] | None = {
                "Authorization": bearer(token, access_key, access_secret)
            }
        elif not owned and not (token or access_key or access_secret):
            # An injected client may carry its own bearer; part of a triple never does.
            auth_headers = None
        else:
            raise ValueError(
                "Discovery needs a signature token: pass signature=, or the "
                "token/access_key/access_secret triple. Mint one with create_signature_token()."
            )

        # The prefix carries /batch so that _links.next_page hrefs, which are absolute
        # from the host root, resolve against the same base_url.
        self.transport = Transport(
            f"batch/{self.workspace_name}",
            base_url=base_url,
            timeout=timeout if owned else None,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            accept="application/json",
            auth_headers=auth_headers,
            http_client=http_client,
        )

    @property
    def is_closed(self) -> bool:
        return self.transport.is_closed

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> Discovery:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<Discovery workspace={self.workspace_name!r}>"

    def _batch(
        self,
        path: str,
        key: str,
        *,
        include_spaces: str | None = None,
        per_page: int | None = None,
    ) -> Page[DiscoveryObject]:
        """The two query parameters every batch resource shares."""
        query: dict[str, QueryValue] = {}
        if include_spaces is not None:
            query["include_spaces"] = include_spaces
        if per_page is not None:
            query["per_page"] = per_page

        def pages() -> Iterator[Batch[DiscoveryObject]]:
            try:
                yield from link_pages(
                    self.transport, path, key, DiscoveryObject.from_payload, params=query or None
                )
            except PermissionDeniedError as exc:
                raise DiscoveryUnavailableError(
                    f"the Discovery API refused {path}; it requires a Mode Enterprise plan"
                ) from exc

        return Page(pages, path)

    def reports(
        self, *, include_spaces: str | None = None, per_page: int | None = None
    ) -> Page[DiscoveryObject]:
        return self._batch("/reports", "reports", include_spaces=include_spaces, per_page=per_page)

    def report_stats(
        self, *, include_spaces: str | None = None, per_page: int | None = None
    ) -> Page[DiscoveryObject]:
        return self._batch(
            "/report_stats", "report_stats", include_spaces=include_spaces, per_page=per_page
        )

    def queries(
        self, *, include_spaces: str | None = None, per_page: int | None = None
    ) -> Page[DiscoveryObject]:
        return self._batch("/queries", "queries", include_spaces=include_spaces, per_page=per_page)

    def charts(
        self, *, include_spaces: str | None = None, per_page: int | None = None
    ) -> Page[DiscoveryObject]:
        return self._batch("/charts", "charts", include_spaces=include_spaces, per_page=per_page)

    def collections(self, *, per_page: int | None = None) -> Page[DiscoveryObject]:
        """Collections are served at /spaces, matching the REST API's older spelling."""
        return self._batch("/spaces", "spaces", per_page=per_page)

    def members(self, *, per_page: int | None = None) -> Page[DiscoveryObject]:
        return self._batch("/members", "members", per_page=per_page)
