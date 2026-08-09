"""The workspace itself, its Collections, and who may see them.

Mode renamed Spaces to Collections in the UI but not in the API. Both spellings resolve;
this SDK sends ``/spaces``, while every ``_links.self`` Mode returns says
``/collections``.

The write vocabulary is not uniform: ``space_type`` is settable only at creation,
``default_access_level`` offers ``none`` on create and ``restricted`` on edit, and a
private Collection accepts neither a name nor a description.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from typing import Literal
from typing import get_args

from .._types import QueryValue
from ..errors import ModeError
from ..models import Dataset
from ..models import Report
from ..models import Space
from ..models import SpaceMembership
from ..models import User
from ..models import Verification
from ..models import Workspace
from ..pagination import Page
from ._args import SpaceMembershipRef
from ._args import SpaceRef
from ._args import token_of
from ._args import write_body
from ._base import Resource

#: The three values Mode implements. Every other string -- ``ALL`` included, the
#: comparison being case-sensitive -- answers 200 with the unfiltered result.
SpaceFilter = Literal["all", "custom", "mine"]
SPACE_FILTERS = frozenset(get_args(SpaceFilter))

#: Settable when a Collection is created and never afterwards.
SpaceType = Literal["private", "community", "custom"]

#: Two vocabularies, not one: ``none`` is settable only at creation and ``restricted``
#: only afterwards.
NewAccessLevel = Literal["view", "edit", "none"]
AccessLevel = Literal["view", "edit", "restricted"]


def _href(payload: dict[str, Any], rel: str) -> str | None:
    return ((payload.get("_links") or {}).get(rel) or {}).get("href")


class WorkspaceResource(Resource):
    def verify(self) -> Verification:
        """Cheapest authenticated call, and the one Mode documents for workspace tokens.

        Answers with the key's scope, not the workspace record -- call ``get()`` for that,
        but not as a credential check: that endpoint answers anonymously.
        """
        return self._one(Verification, "GET", "/verify", workspace=False)

    def get(self) -> Workspace:
        """The workspace record: name, plan, limits, counts.

        Answers 200 anonymously, so a 200 here proves nothing about the credential --
        ``verify()`` is the check.
        """
        return self._one(Workspace, "GET", "")

    def account(self) -> User:
        """The member who owns the credential, in three requests.

        ``GET /account`` answers ``400 unsupported for use with Api Keys`` for every API
        key, so this follows Mode's own link chain instead: ``/verify`` names the
        credential's record, that names its member, and the member record is the user. A
        workspace-scope key may have no member behind it, and this raises when it does not.
        """
        key_href = self.verify().link("self")
        if not key_href:
            raise ModeError(
                "/verify named no api_keys record (rel 'self'), so the member "
                "behind this credential cannot be resolved"
            )
        key = self._t.payload("GET", key_href.removeprefix("/api"), workspace=False)
        member_href = _href(key, "member")
        if not member_href:
            raise ModeError(
                "this credential's api_keys record names no member (rel 'member'); a "
                "workspace-scope key belongs to the workspace rather than to a person"
            )
        return self._one(User, "GET", member_href.removeprefix("/api"), workspace=False)


class SpacesResource(Resource):
    def list(
        self,
        *,
        filter: SpaceFilter | None = None,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[Space]:
        """Unfiltered means "the Collections I am a member of", not "the workspace's".

        Nothing in the payload says anything was withheld, so the default is a truncation
        a caller cannot detect; pass ``filter="all"`` to enumerate the workspace. An
        unrecognised value raises here rather than being forwarded, because Mode answers
        one with the unfiltered result. ``extra_params={"filter": ...}`` sends one anyway.
        """
        if filter is not None and filter not in SPACE_FILTERS:
            raise ValueError(
                f"filter={filter!r} is not one of {sorted(SPACE_FILTERS)}. Mode answers an "
                f"unknown filter with 200 and the unfiltered result, so this would have "
                f"returned the wrong rows. Use extra_params={{'filter': ...}} to send it anyway."
            )
        return self._many(
            Space,
            "/spaces",
            "spaces",
            params={"filter": filter} if filter else None,
            per_page=per_page,
            extra_params=extra_params,
        )

    def get(self, space: SpaceRef) -> Space:
        return self._one(Space, "GET", f"/spaces/{token_of(space, 'space')}")

    def create(
        self,
        name: str,
        *,
        description: str | None = None,
        space_type: SpaceType | None = None,
        default_access_level: NewAccessLevel | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> Space:
        """Wrapped in ``space``. These four are what ``_forms.create`` declares.

        An omitted keyword is not sent and Mode applies its own default
        (``space_type="custom"``, ``default_access_level="none"``).
        ``default_access_level="restricted"`` is set with ``update()``, not here.
        """
        payload = write_body(
            "space",
            extra_body,
            name=name,
            description=description,
            space_type=space_type,
            default_access_level=default_access_level,
        )
        return self._one(Space, "POST", "/spaces", json=payload)

    def update(
        self,
        space: SpaceRef,
        *,
        name: str | None = None,
        description: str | None = None,
        default_access_level: AccessLevel | None = None,
        extra_body: Mapping[str, object] | None = None,
    ) -> Space:
        """PATCH, like every other update, and wrapped in ``space``.

        ``space_type`` is absent on purpose: a PATCH carrying it answers 200 and leaves
        the value untouched, so it would report a change that did not happen. Set it with
        ``create()``.

        Which of the three a Collection accepts varies -- a private one takes only
        ``default_access_level`` -- so all are optional.
        """
        payload = write_body(
            "space",
            extra_body,
            name=name,
            description=description,
            default_access_level=default_access_level,
        )
        return self._one(Space, "PATCH", f"/spaces/{token_of(space, 'space')}", json=payload)

    def delete(self, space: SpaceRef) -> None:
        """Private Collections publish no ``_forms.destroy``; only custom ones do."""
        self._t.request("DELETE", f"/spaces/{token_of(space, 'space')}")

    def reports(
        self,
        space: SpaceRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[Report]:
        """Fixed 30-item pages: ``per_page`` is accepted and ignored, and the envelope
        carries neither a ``pagination`` block nor a next link. Paging itself works.
        """
        return self._many(
            Report,
            f"/spaces/{token_of(space, 'space')}/reports",
            "reports",
            per_page=per_page,
            extra_params=extra_params,
        )

    def datasets(
        self,
        space: SpaceRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[Dataset]:
        """The items arrive under ``_embedded.reports``, not ``datasets``: Mode names the
        envelope after the underlying type.
        """
        return self._many(
            Dataset,
            f"/spaces/{token_of(space, 'space')}/datasets",
            "reports",
            per_page=per_page,
            extra_params=extra_params,
        )


class SpaceMembershipsResource(Resource):
    """Who may see a Collection. Deprecated by Mode, which has moved Collection access
    elsewhere.

    None of these routes tolerates a private Collection -- they answer
    ``400 Invalid space`` rather than an empty list -- and ``spaces.list()`` returns the
    caller's private Collection by default, so a loop over every Collection meets this
    on its first item.
    """

    def list(
        self,
        space: SpaceRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[SpaceMembership]:
        return self._many(
            SpaceMembership,
            f"/spaces/{token_of(space, 'space')}/memberships",
            "space_memberships",
            per_page=per_page,
            extra_params=extra_params,
        )

    def get(self, space: SpaceRef, membership: SpaceMembershipRef) -> SpaceMembership:
        return self._one(
            SpaceMembership,
            "GET",
            f"/spaces/{token_of(space, 'space')}/memberships/{token_of(membership, 'membership')}",
        )

    def add(
        self,
        space: SpaceRef,
        member_token: str,
        *,
        member_type: str = "User",
        extra_body: Mapping[str, object] | None = None,
    ) -> SpaceMembership:
        """Wrapped in ``membership``. Mode publishes no create form for this collection,
        so the wrapper and field names follow its documentation rather than its hypermedia.

        Mode's two endpoints disagree about ``member_type``: memberships come back as
        ``"account"`` while ``/permissions/search`` calls the same thing ``"User"``. Pass
        ``member_type="account"`` if Mode refuses the default. That same endpoint is where
        a ``member_token`` comes from; reach it with ``mode.request()``.
        """
        payload = write_body(
            "membership", extra_body, member_type=member_type, member_token=member_token
        )
        return self._one(
            SpaceMembership,
            "POST",
            f"/spaces/{token_of(space, 'space')}/memberships",
            json=payload,
        )

    def remove(self, space: SpaceRef, membership: SpaceMembershipRef) -> None:
        self._t.request(
            "DELETE",
            f"/spaces/{token_of(space, 'space')}/memberships/{token_of(membership, 'membership')}",
        )
