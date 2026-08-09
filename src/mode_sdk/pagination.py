"""Pagination over Mode collections: eager first page, cached from there on.

Three envelopes hide behind ``Page``: ``page``/``per_page`` on the REST collections, a
``next_token`` cursor on audit logs, and ``_links.next_page`` on Discovery.

A walk ends on an empty page, a page identical to its predecessor, or an envelope that
states it was the last. Several REST collections ignore ``page`` and return their full
contents for every page number, so the duplicate guard is their only terminator.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import Generic
from typing import TypeVar

from ._policy import embedded
from ._transport import Transport
from ._types import QueryValue

T = TypeVar("T")
Fingerprint = tuple[str, ...]

_log = logging.getLogger("mode_sdk.pagination")

MAX_PER_PAGE = 30
"""The largest page Mode will serve. A larger ``per_page`` is silently clamped, not
refused; the SDK sends what the caller asked for and lets the walk absorb the clamp.
"""


@dataclass(frozen=True, slots=True)
class Batch(Generic[T]):
    """One request's worth of items, plus what that response said about the next one."""

    items: list[T]
    has_more: bool | None = None
    total_count: int | None = None


def _fingerprint(items: list[dict[str, Any]]) -> Fingerprint:
    return tuple(
        str(item.get("token") or item.get("id") or json.dumps(item, sort_keys=True, default=str))
        for item in items
    )


def _unseen(value: object, seen: set[str]) -> str | None:
    """A cursor or href already followed means the walk is circling; call that the end."""
    return value if isinstance(value, str) and value and value not in seen else None


def _counters(payload: dict[str, Any]) -> dict[str, Any]:
    """Mode's ``pagination`` block, or an empty dict where the envelope omits it."""
    counters = payload.get("pagination")
    return counters if isinstance(counters, dict) else {}


def _offset_has_more(payload: dict[str, Any]) -> bool | None:
    """Answer only from what the envelope states; ``None`` means it did not say.

    Counters are read before the link because Mode emits ``next_page`` past the end of a
    collection, so where the two disagree the link is the one that is wrong.
    """
    counters = _counters(payload)
    page, total = counters.get("page"), counters.get("total_pages")
    if isinstance(page, int) and isinstance(total, int):
        return page < total
    if ((payload.get("_links") or {}).get("next_page") or {}).get("href"):
        return True
    return None


def _total_count(payload: dict[str, Any]) -> int | None:
    """Mode's own size for the whole collection, where the envelope states one."""
    total = _counters(payload).get("total_count")
    return total if isinstance(total, int) else None


def _note_clamped_page_size(payload: dict[str, Any], asked: int | None, path: str) -> None:
    """Note at debug that Mode served smaller pages than the caller asked for.

    Only collections that echo ``pagination.per_page`` can be checked. The walk returns
    the complete collection either way.
    """
    served = _counters(payload).get("per_page")
    if asked and isinstance(served, int) and served < asked:
        _log.debug("%s served per_page=%d for the requested %d", path, served, asked)


class Page(Generic[T]):
    """A collection Mode returns in pieces, presented as one sequence.

    Construction fetches the first page, so a bad filter or a stale credential raises
    from the ``list()`` call rather than from a loop elsewhere. Every page fetched is
    cached for the Page's lifetime, so the cache grows even behind ``pages()`` -- drop
    the Page once the walk is done. A request that fails mid-walk is re-raised by every
    later read, rather than leaving a truncated collection that reports itself complete.
    """

    def __init__(self, pages: Callable[[], Iterator[Batch[T]]], description: str = "") -> None:
        self._source = pages()
        self._cached: list[list[T]] = []
        self._has_more: bool | None = None
        self._total_count: int | None = None
        self._description = description
        self._failure: Exception | None = None
        self._fetch()

    @property
    def has_more(self) -> bool | None:
        """Whether Mode said more pages follow the last one fetched.

        ``None`` means the envelope did not say, which is the answer for every collection
        that ships only ``_links.self``. Iterating to exhaustion is what settles it.
        """
        return self._has_more

    @property
    def total_count(self) -> int | None:
        """Mode's own count for the whole collection, or ``None`` where it states none.

        Never inferred from what has been fetched; use ``len(page.list())`` for that.
        """
        return self._total_count

    def __iter__(self) -> Iterator[T]:
        for page in self.pages():
            yield from page

    def pages(self) -> Iterator[list[T]]:
        """Yield one list per HTTP request instead of one item at a time."""
        index = 0
        while True:
            if index == len(self._cached) and not self._fetch():
                return
            yield self._cached[index]
            index += 1

    def first_page(self) -> list[T]:
        """The page fetched at construction, however far the walk has since gone."""
        return self._cached[0] if self._cached else []

    def list(self) -> list[T]:
        return list(self)

    def __repr__(self) -> str:
        what = f" {self._description}" if self._description else ""
        more = "" if self._has_more is None else f" has_more={self._has_more}"
        total = "" if self._total_count is None else f" of {self._total_count}"
        failed = "" if self._failure is None else f" failed={type(self._failure).__name__}"
        fetched = sum(len(page) for page in self._cached)
        return f"<Page{what} fetched={fetched}{total}{more}{failed}>"

    def _fetch(self) -> bool:
        """Advance the walk by one request, or re-raise the one that already broke it.

        The failure is sticky: a Page is a cached, re-readable value, so retrying here
        would make its contents depend on which read hit a bad minute.
        """
        if self._failure is not None:
            raise self._failure
        try:
            batch = next(self._source, None)
        except Exception as exc:
            self._failure = exc
            raise
        if batch is None:
            return False
        self._cached.append(batch.items)
        self._has_more = batch.has_more
        self._total_count = batch.total_count
        return True


def offset_pages(
    transport: Transport,
    path: str,
    key: str,
    parse: Callable[[dict[str, Any]], T],
    *,
    method: str = "GET",
    params: Mapping[str, QueryValue] | None = None,
    workspace: bool = True,
    per_page: int | None = None,
) -> Iterator[Batch[T]]:
    """Walk a ``page=``/``per_page=`` collection until Mode stops adding to it.

    Three terminators: an empty page, a page whose fingerprint repeats its predecessor,
    or ``pagination.page >= pagination.total_pages``.

    **Never add "the page was shorter than per_page".** Mode caps ``per_page`` at
    ``MAX_PER_PAGE`` and several collections ignore it entirely, so that test truncates
    the walk silently -- ``per_page=50`` would return 30 of 89 items with no error.
    """
    base: dict[str, QueryValue] = dict(params or {})
    if per_page:
        base["per_page"] = per_page
    page = 1
    previous: Fingerprint | None = None
    while True:
        payload = transport.payload(method, path, params=base | {"page": page}, workspace=workspace)
        items = embedded(payload, key)
        if not items:
            return
        current = _fingerprint(items)
        if current == previous:
            return
        previous = current
        if page == 1:
            _note_clamped_page_size(payload, per_page, path)
        has_more = _offset_has_more(payload)
        yield Batch([parse(item) for item in items], has_more, _total_count(payload))
        if has_more is False:
            return
        page += 1


def token_pages(
    transport: Transport,
    path: str,
    key: str,
    parse: Callable[[dict[str, Any]], T],
    *,
    method: str = "GET",
    params: Mapping[str, QueryValue] | None = None,
    workspace: bool = True,
    headers: Mapping[str, str] | None = None,
) -> Iterator[Batch[T]]:
    query: dict[str, QueryValue] = dict(params or {})
    seen: set[str] = set()
    while True:
        payload = transport.payload(
            method, path, params=query, workspace=workspace, headers=headers
        )
        items = embedded(payload, key)
        cursor = _unseen(payload.get("next_token") or _counters(payload).get("next_token"), seen)
        if items:
            yield Batch([parse(item) for item in items], cursor is not None, _total_count(payload))
        if cursor is None:
            return
        seen.add(cursor)
        query = query | {"next_token": cursor}


def link_pages(
    transport: Transport,
    path: str,
    key: str,
    parse: Callable[[dict[str, Any]], T],
    *,
    method: str = "GET",
    params: Mapping[str, QueryValue] | None = None,
    workspace: bool = True,
) -> Iterator[Batch[T]]:
    payload = transport.payload(method, path, params=params, workspace=workspace)
    seen: set[str] = set()
    while True:
        items = embedded(payload, key)
        href = _unseen(((payload.get("_links") or {}).get("next_page") or {}).get("href"), seen)
        if items:
            yield Batch([parse(item) for item in items], href is not None, _total_count(payload))
        if href is None:
            return
        seen.add(href)
        payload = transport.payload(method, href, workspace=False)
