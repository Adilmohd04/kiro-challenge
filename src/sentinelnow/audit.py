"""Append-only audit log. Records who/what/why/result for every executed action.

Security rule: never write secret values or full record contents here — IDs and the
fields needed for the audit entry only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from .models import Decision, Effect, WriteRequest


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


def build_entry(req: WriteRequest, decision: Decision, applied: bool) -> AuditEntry:
    """Construct the :class:`AuditEntry` for a record write.

    This is the single place the closed key set is assembled for record writes, so
    every store (in-memory and SQLite) builds identical entries and cannot drift.
    Only safe fields are read from ``req`` — never ``req.fields`` or any secret.
    """
    return AuditEntry(
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


def build_event_entry(actor: str, action: str, result: str) -> AuditEntry:
    """Construct the :class:`AuditEntry` for a non-record gateway event.

    Reuses the closed AuditEntry key set so the audit log stays uniform and
    append-only (Req 9.1). ``actor`` is the invoking identity (who), ``action``
    is the event (what, e.g. "engage"/"release"), and ``result`` the outcome.
    A sentinel ``table`` marks this as a gateway event rather than a record
    write; ``record_ids`` is empty and no secret/credential value is included.

    The effect is mapped from the action to a valid :class:`Effect` value:
    engaging the kill switch moves the gateway to a deny-all posture
    (``Effect.DENY``), releasing it restores the allow posture
    (``Effect.ALLOW``). Approval-workflow events (``approve``/``reject``) record
    under a neutral ``__approval__`` sentinel table; ``result`` carries only the
    non-secret approval id so no record contents or credentials are logged.
    """
    if action in ("engage", "release"):
        effect = Effect.DENY if action == "engage" else Effect.ALLOW
        return AuditEntry(
            audit_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_id=actor,
            table="__killswitch__",
            operation=action,
            record_ids=[],
            reason=f"kill switch {action}: {result}",
            effect=effect.value,
            risk_score=0,
            applied=True,
        )

    effect = Effect.DENY if action == "reject" else Effect.ALLOW
    return AuditEntry(
        audit_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc).isoformat(),
        agent_id=actor,
        table="__approval__",
        operation=action,
        record_ids=[],
        reason=f"approval {action}: {result}",
        effect=effect.value,
        risk_score=0,
        applied=True,
    )


@runtime_checkable
class AuditStore(Protocol):
    """Structural interface every audit store satisfies.

    Both the in-memory :class:`AuditLog` and the SQLite-backed store implement
    this, so the gateway can be backed by either without any behavior change.
    """

    def record(self, req: WriteRequest, decision: Decision, applied: bool) -> AuditEntry: ...

    def record_event(self, actor: str, action: str, result: str) -> AuditEntry: ...

    def all(self) -> list[AuditEntry]: ...


class AuditLog:
    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(self, req: WriteRequest, decision: Decision, applied: bool) -> AuditEntry:
        entry = build_entry(req, decision, applied)
        self._entries.append(entry)
        return entry

    def record_event(self, actor: str, action: str, result: str) -> AuditEntry:
        """Record a non-record gateway event (e.g. kill-switch engage/release)."""
        entry = build_event_entry(actor, action, result)
        self._entries.append(entry)
        return entry

    def all(self) -> list[AuditEntry]:
        return list(self._entries)
