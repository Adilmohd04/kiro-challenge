"""Starter property-based tests (Lesson 4 preview).

These encode rules that must ALWAYS hold, regardless of input. The Spec session will
extend these from the requirements. Run with:  python -m pytest
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from sentinelnow.models import Operation, WriteRequest
from sentinelnow.policy import Policy, evaluate_policy, risk_score

# Strategy that builds arbitrary, valid write requests.
write_requests = st.builds(
    WriteRequest,
    agent_id=st.text(min_size=1, max_size=10),
    table=st.sampled_from(["incident", "change_request", "cmdb_ci"]),
    operation=st.sampled_from(list(Operation)),
    record_ids=st.lists(st.text(min_size=1, max_size=6), max_size=25),
    fields=st.dictionaries(
        st.sampled_from(["priority", "state", "category"]),
        st.sampled_from(["1", "2", "3", "open", "closed"]),
        max_size=3,
    ),
    reason=st.text(min_size=1, max_size=20),
)


@given(req=write_requests)
def test_risk_score_always_in_range(req: WriteRequest) -> None:
    """Property: a risk score is always between 0 and 100."""
    assert 0 <= risk_score(req, Policy()) <= 100


@given(req=write_requests)
def test_bulk_over_limit_is_never_allowed(req: WriteRequest) -> None:
    """Property: a batch larger than the limit is never outright ALLOWED."""
    policy = Policy(max_batch_size=5)
    decision = evaluate_policy(req, policy)
    if req.batch_size > policy.max_batch_size:
        assert decision.effect.value != "allow"


@given(req=write_requests)
def test_kill_switch_denies_everything(req: WriteRequest) -> None:
    """Property: when the kill switch is engaged, nothing is ever allowed."""
    policy = Policy(kill_switch_engaged=True)
    decision = evaluate_policy(req, policy)
    assert decision.effect.value == "deny"
