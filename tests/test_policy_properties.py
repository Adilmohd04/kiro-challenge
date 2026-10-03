"""Starter property-based tests (Lesson 4 preview).

These encode rules that must ALWAYS hold, regardless of input. The Spec session will
extend these from the requirements. Run with:  python -m pytest
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from sentinelnow.models import Effect, Operation, WriteRequest
from sentinelnow.policy import Policy, evaluate_policy, risk_score

# Operation severity ordering per Requirement 1.3: CREATE < UPDATE < CLOSE < DELETE.
_OPERATION_ORDER: list[Operation] = [
    Operation.CREATE,
    Operation.UPDATE,
    Operation.CLOSE,
    Operation.DELETE,
]

# Strategy that builds arbitrary, valid write requests.
# record_ids spans 0-25 to cross the default batch limit (10); priority values
# include the protected "1" and non-protected "2"/"3" to exercise both branches.
write_requests = st.builds(
    WriteRequest,
    agent_id=st.text(min_size=1, max_size=10),
    table=st.sampled_from(["incident", "change_request", "cmdb_ci"]),
    operation=st.sampled_from(list(Operation)),
    record_ids=st.lists(st.text(min_size=1, max_size=6), min_size=0, max_size=25),
    fields=st.dictionaries(
        st.sampled_from(["priority", "state", "category"]),
        st.sampled_from(["1", "2", "3", "open", "closed"]),
        max_size=3,
    ),
    reason=st.text(min_size=1, max_size=20),
)

# Strategy that builds arbitrary policies spanning the guardrail configuration space.
policies = st.builds(
    Policy,
    max_batch_size=st.integers(1, 20),
    allow_delete=st.booleans(),
    protected_priorities=st.lists(st.sampled_from(["1", "2", "3"]), max_size=3),
    kill_switch_engaged=st.booleans(),
)


# Feature: sentinelnow-mcp-gateway, Property 1: risk_score is an int bounded in [0, 100]
@given(req=write_requests, policy=policies)
def test_risk_score_always_in_range(req: WriteRequest, policy: Policy) -> None:
    """Property 1: a risk score is always an int between 0 and 100, for any policy.

    **Validates: Requirements 1.1**
    """
    score = risk_score(req, policy)
    assert isinstance(score, int)
    assert 0 <= score <= 100


# Feature: sentinelnow-mcp-gateway, Property 2: risk_score is deterministic for identical inputs
@given(req=write_requests, policy=policies)
def test_risk_score_is_deterministic(req: WriteRequest, policy: Policy) -> None:
    """Property 2: identical request and policy always produce the same risk score.

    **Validates: Requirements 1.2**
    """
    assert risk_score(req, policy) == risk_score(req, policy)


# Feature: sentinelnow-mcp-gateway, Property 3: risk_score is monotonic in operation severity
@given(req=write_requests, policy=policies)
def test_risk_score_monotonic_in_operation(req: WriteRequest, policy: Policy) -> None:
    """Property 3: score is non-decreasing over CREATE <= UPDATE <= CLOSE <= DELETE.

    **Validates: Requirements 1.3**
    """
    scores = [
        risk_score(req.model_copy(update={"operation": op}), policy) for op in _OPERATION_ORDER
    ]
    for lower, higher in zip(scores, scores[1:]):
        assert lower <= higher


# Feature: sentinelnow-mcp-gateway, Property 4: kill switch denies every write
@given(req=write_requests, policy=policies)
def test_kill_switch_denies_everything(req: WriteRequest, policy: Policy) -> None:
    """Property 4: when the kill switch is engaged, every decision is DENY.

    **Validates: Requirements 3.1**
    """
    engaged = policy.model_copy(update={"kill_switch_engaged": True})
    decision = evaluate_policy(req, engaged)
    assert decision.effect is Effect.DENY


# Feature: sentinelnow-mcp-gateway, Property 5: batch size gates on the configured limit
@given(req=write_requests, policy=policies)
def test_batch_size_gates_on_limit(req: WriteRequest, policy: Policy) -> None:
    """Property 5: over-limit batches are DENIED; within-limit are not denied for size.

    **Validates: Requirements 2.1, 2.3, 8.4**
    """
    policy = policy.model_copy(update={"kill_switch_engaged": False})
    decision = evaluate_policy(req, policy)
    if req.batch_size > policy.max_batch_size:
        assert decision.effect is Effect.DENY
    else:
        # Within the limit: must never be denied on the basis of batch size.
        batch_reason = f"Batch size {req.batch_size} exceeds limit {policy.max_batch_size}"
        assert decision.reason != batch_reason


# Feature: sentinelnow-mcp-gateway, Property 6: deletes are denied unless explicitly allowed
@given(req=write_requests, policy=policies)
def test_deletes_denied_unless_allowed(req: WriteRequest, policy: Policy) -> None:
    """Property 6: DELETE is DENY when allow_delete is off; allow_delete does not bypass
    the batch limit.

    **Validates: Requirements 8.1, 8.2, 8.3**
    """
    delete_req = req.model_copy(update={"operation": Operation.DELETE})

    denied = policy.model_copy(update={"allow_delete": False, "kill_switch_engaged": False})
    assert evaluate_policy(delete_req, denied).effect is Effect.DENY

    # allow_delete=True must still respect the batch limit (no bypass of guardrails).
    allowed = policy.model_copy(update={"allow_delete": True, "kill_switch_engaged": False})
    if delete_req.batch_size > allowed.max_batch_size:
        assert evaluate_policy(delete_req, allowed).effect is Effect.DENY


# Feature: sentinelnow-mcp-gateway, Property 7: protected priority requires approval when configured
@given(
    req=write_requests,
    protected=st.lists(st.sampled_from(["1", "2", "3"]), max_size=3),
    current_priorities=st.lists(st.sampled_from(["1", "2", "3"]), max_size=3),
)
def test_protected_priority_requires_approval(
    req: WriteRequest, protected: list[str], current_priorities: list[str]
) -> None:
    """Property 7: touching a protected priority maps to NEEDS_APPROVAL when configured.

    The rule must fire when EITHER the record's CURRENT priority OR the target
    priority in ``req.fields`` is protected — so an agent cannot downgrade or
    modify a record that is currently a protected priority.

    Isolates the priority rule: kill switch off, deletes allowed, batch within limit.

    **Validates: Requirements 4.1, 4.4**
    """
    # Isolate precedence so the protected-priority rule is the deciding one.
    policy = Policy(
        max_batch_size=max(req.batch_size, 1),
        allow_delete=True,
        protected_priorities=protected,
        kill_switch_engaged=False,
    )
    decision = evaluate_policy(req, policy, current_priorities)
    target_priority = req.fields.get("priority")
    touches_protected = (target_priority in protected) or any(
        p in protected for p in current_priorities
    )

    if protected and touches_protected:
        assert decision.effect is Effect.NEEDS_APPROVAL
    elif not protected:
        # Empty protected set: never NEEDS_APPROVAL on the basis of protected priority.
        protected_reason = "Change touches a protected priority record"
        assert decision.reason != protected_reason
