"""Pure, testable policy and risk logic. No I/O here — ideal for property-based tests.

NOTE: This is an initial reference implementation. The Spec session (Lessons 1 & 4)
will refine the exact rules and generate property-based tests against them.
"""

from __future__ import annotations

from pydantic import BaseModel

from .models import Decision, Effect, Operation, WriteRequest


class Policy(BaseModel):
    """Configurable guardrails. Safe defaults: deny destructive/bulk unless allowed."""

    max_batch_size: int = 10
    allow_delete: bool = False
    protected_priorities: list[str] = ["1"]  # e.g. P1 incidents
    kill_switch_engaged: bool = False


# Risk weights per operation (0-100 scale is clamped at the end).
_OPERATION_RISK: dict[Operation, int] = {
    Operation.CREATE: 10,
    Operation.UPDATE: 30,
    Operation.CLOSE: 40,
    Operation.DELETE: 80,
}


def risk_score(req: WriteRequest, policy: Policy) -> int:
    """Return a risk score in the range [0, 100]. Pure function."""
    base = _OPERATION_RISK.get(req.operation, 50)
    batch_penalty = min(req.batch_size * 2, 40)
    priority_penalty = 0
    if req.fields.get("priority") in policy.protected_priorities:
        priority_penalty = 30
    score = base + batch_penalty + priority_penalty
    return max(0, min(score, 100))


def evaluate_policy(req: WriteRequest, policy: Policy) -> Decision:
    """Decide allow / needs_approval / deny for an intended write. Pure function."""
    score = risk_score(req, policy)

    if policy.kill_switch_engaged:
        return Decision(effect=Effect.DENY, reason="Kill switch engaged", risk_score=score)

    if req.operation is Operation.DELETE and not policy.allow_delete:
        return Decision(effect=Effect.DENY, reason="Deletes are not allowed", risk_score=score)

    if req.batch_size > policy.max_batch_size:
        return Decision(
            effect=Effect.DENY,
            reason=f"Batch size {req.batch_size} exceeds limit {policy.max_batch_size}",
            risk_score=score,
        )

    if req.fields.get("priority") in policy.protected_priorities:
        return Decision(
            effect=Effect.NEEDS_APPROVAL,
            reason="Change touches a protected priority record",
            risk_score=score,
        )

    if score >= 70:
        return Decision(effect=Effect.NEEDS_APPROVAL, reason="High risk score", risk_score=score)

    return Decision(effect=Effect.ALLOW, reason="Within policy", risk_score=score)
