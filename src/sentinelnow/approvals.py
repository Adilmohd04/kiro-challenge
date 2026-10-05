"""Human-in-the-loop approval workflow for writes that policy gates as NEEDS_APPROVAL.

When ``evaluate_policy`` returns ``NEEDS_APPROVAL`` the gateway records a PENDING
:class:`ApprovalRequest` here instead of only raising. An operator can then approve
or reject it; an approved request lets a subsequent *identical* ``guarded_write``
bypass ONLY the approval gate (the kill switch and hard denies still win — see
``Gateway.guarded_write``).

Security: an :class:`ApprovalRequest` carries the SAME audit-safe summary as an
``AuditEntry`` — IDs and the fields needed to review the action — and NEVER any
``WriteRequest.fields`` values or secrets. ``fingerprint_request`` hashes a stable,
secret-free tuple so the hash input stays local and is never logged or stored.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel

from .models import WriteRequest


class ApprovalRequest(BaseModel):
    """Audit-safe summary of a write awaiting human approval.

    Holds only the closed, non-secret summary of the pending write (mirroring the
    audit key set): never ``WriteRequest.fields`` values or any credential.
    """

    approval_id: str
    created_at: str
    status: str  # one of "pending" / "approved" / "rejected"
    fingerprint: str
    agent_id: str
    table: str
    operation: str
    record_ids: list[str]
    reason: str
    risk_score: int


def fingerprint_request(req: WriteRequest) -> str:
    """Return a stable SHA-256 fingerprint binding an approval to this exact request.

    The hash input is a deterministic, secret-free tuple:
    ``(agent_id, table, operation, sorted record_ids, reason, sorted
    "key=value" field pairs)``. Field pairs are included so changing any field
    value yields a different fingerprint (an approval is bound to the precise
    change). The assembled input stays local to this function — it is used only
    to compute the digest and is never logged, stored, or returned.
    """
    field_pairs = sorted(f"{key}={value}" for key, value in req.fields.items())
    parts = [
        req.agent_id,
        req.table,
        req.operation.value,
        *sorted(req.record_ids),
        req.reason,
        *field_pairs,
    ]
    # NUL separator so distinct tuples cannot collide via concatenation.
    payload = "\x00".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ApprovalStore:
    """In-memory store of :class:`ApprovalRequest` records.

    Mirrors the shape of the in-memory audit store: a simple list the gateway owns.
    Dedupes pending requests by fingerprint so re-issuing the same gated write
    reuses the existing pending request rather than creating a duplicate.
    """

    def __init__(self) -> None:
        self._requests: list[ApprovalRequest] = []

    def create(self, req: WriteRequest, risk_score: int) -> ApprovalRequest:
        """Create (or reuse) a PENDING approval request for ``req``.

        If a pending request already exists for the same fingerprint it is
        returned unchanged, so repeated gated writes do not pile up duplicates.
        """
        fingerprint = fingerprint_request(req)
        for existing in self._requests:
            if existing.fingerprint == fingerprint and existing.status == "pending":
                return existing
        approval = ApprovalRequest(
            approval_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc).isoformat(),
            status="pending",
            fingerprint=fingerprint,
            agent_id=req.agent_id,
            table=req.table,
            operation=req.operation.value,
            record_ids=req.record_ids,
            reason=req.reason,
            risk_score=risk_score,
        )
        self._requests.append(approval)
        return approval

    def get(self, approval_id: str) -> ApprovalRequest | None:
        for approval in self._requests:
            if approval.approval_id == approval_id:
                return approval
        return None

    def list_pending(self) -> list[ApprovalRequest]:
        return [a for a in self._requests if a.status == "pending"]

    def set_status(self, approval_id: str, status: str) -> ApprovalRequest:
        """Set the status of an existing approval; raises if the id is unknown.

        Returns the updated model. The caller (gateway) maps the missing-id case
        to a typed error.
        """
        for index, approval in enumerate(self._requests):
            if approval.approval_id == approval_id:
                updated = approval.model_copy(update={"status": status})
                self._requests[index] = updated
                return updated
        raise KeyError(approval_id)

    def is_approved(self, fingerprint: str) -> bool:
        """True iff an approved request exists for ``fingerprint``."""
        return any(a.fingerprint == fingerprint and a.status == "approved" for a in self._requests)
