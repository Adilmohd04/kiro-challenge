"""MCP tool-boundary request/response models for the SentinelNow gateway.

These pydantic models define the external contract for each MCP tool. All input
from the AI agent is validated here before it reaches gateway/policy logic, in
keeping with the project convention of validating external input at the boundary.

The core domain models (WriteRequest, Decision, WriteResult, AuditEntry) live in
.models / .audit and are reused here rather than redefined.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .audit import AuditEntry
from .models import Decision, WriteRequest, WriteResult


class RecordChange(BaseModel):
    """A single previewed before/after diff for one record.

    Mirrors the keys returned by ``MockServiceNow.preview`` ("sys_id", "before",
    "after"), so a preview dict ``d`` can be lifted into this model via
    ``RecordChange(**d)``.
    """

    sys_id: str
    before: str
    after: str


class PreviewChangeRequest(BaseModel):
    request: WriteRequest


class PreviewChangeResponse(BaseModel):
    changes: list[RecordChange]


class PolicyCheckRequest(BaseModel):
    request: WriteRequest


class PolicyCheckResponse(BaseModel):
    decision: Decision


class GuardedWriteRequest(BaseModel):
    request: WriteRequest


class GuardedWriteResponse(BaseModel):
    result: WriteResult


class AuditLogRequest(BaseModel):
    """No parameters — requesting the audit log takes no input."""


class AuditLogResponse(BaseModel):
    entries: list[AuditEntry]


class KillSwitchAction(str, Enum):
    ENGAGE = "engage"
    RELEASE = "release"


class KillSwitchRequest(BaseModel):
    """Request to engage or release the kill switch.

    Because ``action`` is typed as the :class:`KillSwitchAction` enum, any value
    other than "engage" or "release" fails pydantic validation at the boundary
    (Req 3.5) — the gateway never sees an unknown action.
    """

    action: KillSwitchAction
    actor: str = Field(..., min_length=1)


class KillSwitchResponse(BaseModel):
    engaged: bool
    audit_id: str
