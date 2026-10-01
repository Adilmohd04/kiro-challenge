"""Append-only audit log. Records who/what/why/result for every executed action.

Security rule: never write secret values or full record contents here — IDs and the
fields needed for the audit entry only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel

from .models import Decision, WriteRequest


class AuditEntry(BaseModel):
    audit_id: str
    timestamp: str
    agent_id: str
    table: str
    operation: str
    record_ids: list[str]
    reason: str
    effect: str
    risk_score: int
    applied: bool


class AuditLog:
    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(self, req: WriteRequest, decision: Decision, applied: bool) -> AuditEntry:
        entry = AuditEntry(
            audit_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_id=req.agent_id,
            table=req.table,
            operation=req.operation.value,
            record_ids=req.record_ids,
            reason=req.reason,
            effect=decision.effect.value,
            risk_score=decision.risk_score,
            applied=applied,
        )
        self._entries.append(entry)
        return entry

    def all(self) -> list[AuditEntry]:
        return list(self._entries)
