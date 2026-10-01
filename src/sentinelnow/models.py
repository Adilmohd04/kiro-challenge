"""Boundary models for SentinelNow. All external input is validated here."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Effect(str, Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


class Operation(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    CLOSE = "close"


class WriteRequest(BaseModel):
    """An intended change an AI agent wants to make to ServiceNow."""

    agent_id: str = Field(..., min_length=1)
    table: str = Field(..., min_length=1)
    operation: Operation
    record_ids: list[str] = Field(default_factory=list)
    fields: dict[str, str] = Field(default_factory=dict)
    reason: str = Field(..., min_length=1, description="Why the agent wants this change")

    @property
    def batch_size(self) -> int:
        return max(len(self.record_ids), 1)


class Decision(BaseModel):
    effect: Effect
    reason: str
    risk_score: int = Field(..., ge=0, le=100)


class WriteResult(BaseModel):
    applied: bool
    decision: Decision
    audit_id: str | None = None
