"""Pure, testable policy and risk logic. No I/O here — ideal for property-based tests.

NOTE: This is an initial reference implementation. The Spec session (Lessons 1 & 4)
will refine the exact rules and generate property-based tests against them.
"""

from __future__ import annotations

from pydantic import BaseModel

from .models import Decision, Effect, Operation, WriteRequest


class TableRule(BaseModel):
    """Optional per-table overrides for the base guardrails.

    Each field is optional; a ``None`` means "fall back to the base Policy value".
    Only the fields that gate deny/approval decisions are overridable here.
    """

    max_batch_size: int | None = None
    allow_delete: bool | None = None
    protected_priorities: list[str] | None = None


class Policy(BaseModel):
    """Configurable guardrails. Safe defaults: deny destructive/bulk unless allowed."""

    max_batch_size: int = 10
    allow_delete: bool = False
    protected_priorities: list[str] = ["1"]  # e.g. P1 incidents
    kill_switch_engaged: bool = False
    # Optional per-table overrides keyed by table name; absent tables use base rules.
    table_overrides: dict[str, TableRule] = {}
    # Optional per-agent rate limit (writes per trailing minute). None = unlimited.
    max_writes_per_minute: int | None = None


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


def evaluate_policy(
    req: WriteRequest,
    policy: Policy,
    current_priorities: list[str] | None = None,
    *,
    recent_write_count: int = 0,
) -> Decision:
    """Decide allow / needs_approval / deny for an intended write. Pure function.

    ``current_priorities`` carries the CURRENT priority of each record the write
    targets (read by the gateway via the async instance Protocol and passed in as
    pure data — this function performs no I/O). The protected-priority rule fires
    when EITHER a current priority OR the target priority in ``req.fields`` is in
    the EFFECTIVE ``protected_priorities``, so an agent cannot downgrade or modify
    a record that is currently a protected priority.

    The EFFECTIVE rule for the request's table is the base Policy with any matching
    entry in ``policy.table_overrides`` overlaid: a non-``None`` override field wins,
    otherwise the base value is used. This lets, e.g., one table allow deletes while
    the rest stay locked down.

    ``recent_write_count`` is the number of writes the requesting agent has applied
    in the trailing minute, counted by the gateway and passed in as pure data. When
    ``policy.max_writes_per_minute`` is set and the count has reached it, the write
    maps to NEEDS_APPROVAL (not a hard DENY) so a human can authorize a legitimate
    burst. This is checked AFTER the kill switch and hard-deny rules, preserving the
    precedence: kill switch > deny > rate-limit approval.
    """
    score = risk_score(req, policy)

    override = policy.table_overrides.get(req.table)
    eff_max_batch_size = policy.max_batch_size
    eff_allow_delete = policy.allow_delete
    eff_protected_priorities = policy.protected_priorities
    if override is not None:
        if override.max_batch_size is not None:
            eff_max_batch_size = override.max_batch_size
        if override.allow_delete is not None:
            eff_allow_delete = override.allow_delete
        if override.protected_priorities is not None:
            eff_protected_priorities = override.protected_priorities

    if policy.kill_switch_engaged:
        return Decision(effect=Effect.DENY, reason="Kill switch engaged", risk_score=score)

    if req.operation is Operation.DELETE and not eff_allow_delete:
        return Decision(effect=Effect.DENY, reason="Deletes are not allowed", risk_score=score)

    if req.batch_size > eff_max_batch_size:
        return Decision(
            effect=Effect.DENY,
            reason=f"Batch size {req.batch_size} exceeds limit {eff_max_batch_size}",
            risk_score=score,
        )

    if (
        policy.max_writes_per_minute is not None
        and recent_write_count >= policy.max_writes_per_minute
    ):
        return Decision(
            effect=Effect.NEEDS_APPROVAL,
            reason=(
                f"Rate limit reached: {recent_write_count} writes in the last minute "
                f"meets the limit of {policy.max_writes_per_minute}"
            ),
            risk_score=score,
        )

    candidate_priorities = set(current_priorities or [])
    target_priority = req.fields.get("priority")
    if target_priority is not None:
        candidate_priorities.add(target_priority)
    if candidate_priorities & set(eff_protected_priorities):
        return Decision(
            effect=Effect.NEEDS_APPROVAL,
            reason="Change touches a protected priority record",
            risk_score=score,
        )

    if score >= 70:
        return Decision(effect=Effect.NEEDS_APPROVAL, reason="High risk score", risk_score=score)

    return Decision(effect=Effect.ALLOW, reason="Within policy", risk_score=score)
