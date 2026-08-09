"""Workspace administration: memberships, invites, groups, audit logs.

Audit logs need a Workspace token; a member-scope key answers 401. Three of the four
collections ignore ``page`` and ``per_page``, so the duplicate-page guard in
``pagination`` is their only terminator; ``/groups`` pages properly.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import ClassVar

from .._types import QueryValue
from ..models import AuditLogEntry
from ..models import Group
from ..models import GroupMembership
from ..models import Invite
from ..models import Membership
from ..pagination import Page
from ..pagination import token_pages
from ._args import DataSourceRef
from ._args import GroupMembershipRef
from ._args import GroupRef
from ._args import MembershipRef
from ._args import token_of
from ._args import write_body
from ._base import Resource


def _timestamp(value: datetime | str) -> str:
    return value.isoformat() if isinstance(value, datetime) else value


class MembershipsResource(Resource):
    """The workspace's people.

    ``GET /memberships`` is not the listing -- it answers 404. ``list()`` calls
    ``/memberships/lite``, which the workspace root advertises as rel
    ``memberships_lite``. Its rows carry no ``token``, which is why ``get()`` and
    ``remove()`` have nothing reachable to be called with.
    """

    def list(
        self,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[Membership]:
        """The whole listing in one response: ``page`` and ``per_page`` are ignored here."""
        return self._many(
            Membership,
            "/memberships/lite",
            "memberships",
            per_page=per_page,
            extra_params=extra_params,
        )

    def get(self, membership: MembershipRef) -> Membership:
        """``GET /memberships/{token}`` answers 404 for every token the API exposes, and
        the lite listing carries no ``token`` to feed it. Likely needs a Workspace-scope
        key on a Business plan; untested.
        """
        return self._one(Membership, "GET", f"/memberships/{token_of(membership, 'membership')}")

    def remove(self, membership: MembershipRef) -> None:
        """Same route and caveat as ``get()``: no membership token is reachable."""
        self._t.request("DELETE", f"/memberships/{token_of(membership, 'membership')}")


class InvitesResource(Resource):
    def create(
        self, email: str, message: str, *, extra_body: Mapping[str, object] | None = None
    ) -> Invite:
        """Nests ``invitee`` inside ``invite`` rather than flattening it.

        Unverified: the workspace advertises only the web form, and Mode documents that a
        Workspace token may not manage invites.
        """
        invite: dict[str, object] = {"invitee": {"email": email}, "message": message}
        invite.update(extra_body or {})
        return self._one(Invite, "POST", "/invites", json={"invite": invite})


class GroupsResource(Resource):
    def list(
        self,
        *,
        member: str | None = None,
        data_source: DataSourceRef | None = None,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[Group]:
        """``data_source`` selects the groups entitled to it, and Mode requires ``member``
        alongside it; the coupling is checked here so the failure names the missing
        argument. ``member`` is a username, not a member token.

        This is the one ``data_source=`` that skips ``data_source_token``: it is a query
        filter rather than a path segment, and Mode's filters do accept numeric ids.
        """
        if data_source is not None and member is None:
            raise ValueError(
                "data_source= must be accompanied by member=; Mode answers a bare "
                "data_source with 400 'When sending data source, must send user'"
            )
        params: dict[str, QueryValue] = {}
        if member is not None:
            params["member"] = member
        if data_source is not None:
            params["data_source"] = token_of(data_source, "data_source")
        return self._many(
            Group,
            "/groups",
            "groups",
            params=params or None,
            per_page=per_page,
            extra_params=extra_params,
        )

    def get(self, group: GroupRef) -> Group:
        return self._one(Group, "GET", f"/groups/{token_of(group, 'group')}")

    def create(self, name: str, *, extra_body: Mapping[str, object] | None = None) -> Group:
        """Wrapped in ``user_group``, not ``group``."""
        return self._one(
            Group, "POST", "/groups", json=write_body("user_group", extra_body, name=name)
        )

    def update(
        self, group: GroupRef, name: str, *, extra_body: Mapping[str, object] | None = None
    ) -> Group:
        """Wrapped in ``user_group``, not ``group``."""
        return self._one(
            Group,
            "PATCH",
            f"/groups/{token_of(group, 'group')}",
            json=write_body("user_group", extra_body, name=name),
        )

    def delete(self, group: GroupRef) -> None:
        self._t.request("DELETE", f"/groups/{token_of(group, 'group')}")

    def memberships(
        self,
        group: GroupRef,
        *,
        per_page: int | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[GroupMembership]:
        """The items arrive under ``_embedded.group_memberships``, not ``memberships``.

        Two server-side filters are reachable through ``extra_params``:
        ``{"filter": "preview"}`` returns a sample, and
        ``{"filter": "by_user_id", "user_id": ...}`` returns one user's membership.
        """
        return self._many(
            GroupMembership,
            f"/groups/{token_of(group, 'group')}/memberships",
            "group_memberships",
            per_page=per_page,
            extra_params=extra_params,
        )

    def get_membership(self, group: GroupRef, membership: GroupMembershipRef) -> GroupMembership:
        return self._one(
            GroupMembership,
            "GET",
            f"/groups/{token_of(group, 'group')}/memberships/{token_of(membership, 'membership')}",
        )

    def add_member(
        self,
        group: GroupRef,
        member_token: str,
        *,
        extra_body: Mapping[str, object] | None = None,
    ) -> GroupMembership:
        """Wrapped in ``membership``. Mode publishes no create form for this collection,
        so the wrapper follows its documentation rather than its hypermedia.
        """
        return self._one(
            GroupMembership,
            "POST",
            f"/groups/{token_of(group, 'group')}/memberships",
            json=write_body("membership", extra_body, member_token=member_token),
        )

    def remove_member(self, group: GroupRef, membership: GroupMembershipRef) -> None:
        self._t.request(
            "DELETE",
            f"/groups/{token_of(group, 'group')}/memberships/{token_of(membership, 'membership')}",
        )


class AuditLogsResource(Resource):
    """The one Mode endpoint that speaks neither HAL nor Mode's own error shape.

    It answers the client-wide ``Accept: application/hal+json`` with 406, so this resource
    overrides the header. Past that it wants a Workspace token; a member-scope key gets 401.

    No failure here carries the ``{"id", "message"}`` pair the error mapper reads, so
    every one arrives with ``error_id=None``. The 400 and 401 still map by status, but the
    400's ``.message`` is a raw JSON blob and the 401's ``.body`` is ``None``; the 406 has
    no status mapping and surfaces as a bare ``ModeAPIError``.
    """

    ACCEPT: ClassVar[dict[str, str]] = {"Accept": "application/json"}

    def list(
        self,
        start_timestamp: datetime | str,
        end_timestamp: datetime | str,
        *,
        action: str | None = None,
        entity_id: str | None = None,
        entity_type: str | None = None,
        entity_name: str | None = None,
        username: str | None = None,
        ip: str | None = None,
        event_source: str | None = None,
        extra_params: Mapping[str, QueryValue] | None = None,
    ) -> Page[AuditLogEntry]:
        """The seven filters are Mode's whole documented vocabulary.

        Both timestamps are required and are sent as given; a naive ``datetime`` carries
        no offset and Mode reads it as UTC, so pass an aware one when the window matters.
        Filters set to ``None`` are not sent, and the response nests what they select --
        see ``AuditLogEntry``.
        """
        filters = {
            "action": action,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "entity_name": entity_name,
            "username": username,
            "ip": ip,
            "event_source": event_source,
        }
        params: dict[str, QueryValue] = {
            "start_timestamp": _timestamp(start_timestamp),
            "end_timestamp": _timestamp(end_timestamp),
            **{k: v for k, v in filters.items() if v is not None},
            **dict(extra_params or {}),
        }
        return Page(
            lambda: token_pages(
                self._t,
                "/audit_logs",
                "audit_logs",
                AuditLogEntry.from_payload,
                params=params,
                headers=self.ACCEPT,
            ),
            "/audit_logs",
        )
