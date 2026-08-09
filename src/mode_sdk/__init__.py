"""Unofficial Python SDK for the Mode Analytics API.

from mode_sdk import Mode, query_spec

# Every identifier argument takes the object a previous call returned, or its token.
with Mode("acme", token=token, secret=secret) as mode:
    report = mode.reports.create(space, "revenue", [query_spec(sql, data_source_id)])
    run = mode.reports.run_and_wait(report)
    for name, csv in mode.exports.report_run(report, run).tables().items():
        print(name, len(csv))
"""

from ._constants import BASE_URL
from ._constants import VERSION as __version__
from .client import Mode
from .discovery import Discovery
from .discovery import create_signature_token
from .errors import AuthenticationError
from .errors import BadRequestError
from .errors import ConflictError
from .errors import DiscoveryUnavailableError
from .errors import InternalServerError
from .errors import ModeAPIError
from .errors import ModeConnectionError
from .errors import ModeError
from .errors import ModeTimeoutError
from .errors import NotFoundError
from .errors import PermissionDeniedError
from .errors import RateLimitError
from .errors import RunTimeoutError
from .errors import UnprocessableEntityError
from .models import SUCCESS_RUN_STATES
from .models import TERMINAL_RUN_STATES
from .models import AuditLogEntry
from .models import Chart
from .models import Dataset
from .models import DatasetField
from .models import DatasetRun
from .models import DatasetSchedule
from .models import DataSource
from .models import Definition
from .models import DiscoveryObject
from .models import FieldDescription
from .models import FormField
from .models import Group
from .models import GroupMembership
from .models import Invite
from .models import Membership
from .models import PdfExport
from .models import PdfExportState
from .models import Query
from .models import QueryRun
from .models import Report
from .models import ReportFilter
from .models import ReportRun
from .models import ReportSchedule
from .models import ReportSubscription
from .models import RunState
from .models import SignatureToken
from .models import Space
from .models import SpaceMembership
from .models import User
from .models import Verification
from .models import Workspace
from .pagination import MAX_PER_PAGE
from .pagination import Page
from .resources.distribution import CronSpec
from .resources.distribution import RunResults
from .resources.reports import QuerySpec
from .resources.reports import query_spec

__all__ = [
    "BASE_URL",
    "MAX_PER_PAGE",
    "SUCCESS_RUN_STATES",
    "TERMINAL_RUN_STATES",
    "AuditLogEntry",
    "AuthenticationError",
    "BadRequestError",
    "Chart",
    "ConflictError",
    "CronSpec",
    "DataSource",
    "Dataset",
    "DatasetField",
    "DatasetRun",
    "DatasetSchedule",
    "Definition",
    "Discovery",
    "DiscoveryObject",
    "DiscoveryUnavailableError",
    "FieldDescription",
    "FormField",
    "Group",
    "GroupMembership",
    "InternalServerError",
    "Invite",
    "Membership",
    "Mode",
    "ModeAPIError",
    "ModeConnectionError",
    "ModeError",
    "ModeTimeoutError",
    "NotFoundError",
    "Page",
    "PdfExport",
    "PdfExportState",
    "PermissionDeniedError",
    "Query",
    "QueryRun",
    "QuerySpec",
    "RateLimitError",
    "Report",
    "ReportFilter",
    "ReportRun",
    "ReportSchedule",
    "ReportSubscription",
    "RunResults",
    "RunState",
    "RunTimeoutError",
    "SignatureToken",
    "Space",
    "SpaceMembership",
    "UnprocessableEntityError",
    "User",
    "Verification",
    "Workspace",
    "__version__",
    "create_signature_token",
    "query_spec",
]
