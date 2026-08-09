"""The vocabulary every resource module shares: model-or-token refs and write bodies.

Every ``*Ref`` alias is ``<Model> | str``. Mode routes by the 12-character token, and
every model's ``token`` is ``str | None``, so passing the model is the spelling that
cannot go wrong: ``token_of`` extracts it and names the argument when it is missing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from typing import Protocol

from ..models import Chart
from ..models import Dataset
from ..models import DatasetRun
from ..models import DatasetSchedule
from ..models import DataSource
from ..models import Definition
from ..models import FieldDescription
from ..models import Group
from ..models import GroupMembership
from ..models import Membership
from ..models import Query
from ..models import QueryRun
from ..models import Report
from ..models import ReportFilter
from ..models import ReportRun
from ..models import ReportSchedule
from ..models import ReportSubscription
from ..models import Space
from ..models import SpaceMembership
from ._base import body


class Tokened(Protocol):
    """Any Mode object identified by a 12-character token."""

    @property
    def token(self) -> str | None: ...


ChartRef = Chart | str
DataSourceRef = DataSource | str
DatasetRef = Dataset | str
DatasetRunRef = DatasetRun | str
DefinitionRef = Definition | str
FieldDescriptionRef = FieldDescription | str
FilterRef = ReportFilter | str
GroupMembershipRef = GroupMembership | str
GroupRef = Group | str
MembershipRef = Membership | str
QueryRef = Query | str
QueryRunRef = QueryRun | str
ReportRef = Report | str
RunRef = ReportRun | str
ScheduleRef = ReportSchedule | DatasetSchedule | str
SpaceMembershipRef = SpaceMembership | str
SpaceRef = Space | str
SubscriptionRef = ReportSubscription | str

#: Every Mode token is exactly this long, whatever the object.
TOKEN_LENGTH = 12

_NUMERIC_DATA_SOURCE = (
    "is a numeric id, and every data-source route answers one with "
    "404 'data source not found'. Pass the DataSource or its 12-character token."
)


def token_of(ref: Tokened | str, kind: str) -> str:
    """The token behind a model-or-token argument, or a ValueError naming the argument.

    An empty string and a model whose ``token`` is ``None`` both build a path with a
    missing segment, which Mode answers with a 404 that names nothing.
    """
    if isinstance(ref, str):
        if not ref:
            raise ValueError(
                f"{kind}= is an empty string; Mode routes by a 12-character token and an "
                f"empty one builds a path with a missing segment"
            )
        return ref
    token = getattr(ref, "token", None)
    if not isinstance(token, str) or not token:
        raise ValueError(
            f"{kind}= was passed a {type(ref).__name__} whose .token is {token!r}; pass "
            f"the 12-character token as a string instead"
        )
    return token


def data_source_token(ref: DataSourceRef | int, kind: str = "data_source") -> str:
    """Like ``token_of``, plus a check for the numeric ``DataSource.id``, which every
    data-source route answers with ``404 data source not found``.

    The length test matters: all-digit 12-character tokens are legal, so ``isdigit()``
    alone would refuse one. Data-source ids are shorter than that.
    """
    if isinstance(ref, int):
        raise ValueError(f"{kind}={ref} {_NUMERIC_DATA_SOURCE}")
    token = token_of(ref, kind)
    if token.isdigit() and len(token) != TOKEN_LENGTH:
        raise ValueError(f"{kind}={token!r} {_NUMERIC_DATA_SOURCE}")
    return token


def write_body(
    wrapper: str, extra: Mapping[str, object] | None, **values: object
) -> dict[str, Any]:
    """``extra_body`` merges inside the wrapper Mode requires, after ``body()`` strips
    ``None`` -- which makes it the only way to send an explicit null.
    """
    payload = body(wrapper, **values)
    payload[wrapper].update(extra or {})
    return payload
