"""Tests for the human-in-the-loop approval workflow (FEAT-003).

All data here is synthetic (security steering: synthetic/mock data only, no real
instance data, no secrets). These drive the gateway directly against a seeded
``MockServiceNow`` using the shared helpers from ``test_gateway_properties``.
"""

from __future__ import annotations

import pytest

from sentinelnow.approvals import ApprovalStore, fingerprint_request
from sentinelnow.errors import ApprovalRequired, InvalidRequest, KillSwitchEngaged, PolicyDenied
from sentinelnow.models import Operation, WriteRequest
from sentinelnow.policy import Policy
from test_gateway_properties import fresh_mock, make_gateway, run, snapshot


def _protected_update() -> WriteRequest:
    """An UPDATE to the seeded P1 incident INC0001 -> policy returns NEEDS_APPROVAL."""
    return WriteRequest(
        agent_id="agent-1",
        table="incident",
        operation=Operation.UPDATE,
        record_ids=["INC0001"],
        # "1" is a protected priority -> policy returns NEEDS_APPROVAL. A distinctive
        # synthetic sentinel field proves field VALUES never reach the summary.
        fields={"priority": "1", "note": "FIELDVALUE-SENTINEL"},
        reason="touch a P1 incident",
    )


def _hard_delete() -> WriteRequest:
    """A DELETE with the default policy (allow_delete False) -> hard DENY."""
    return WriteRequest(
        agent_id="agent-1",
        table="incident",
        operation=Operation.DELETE,
        record_ids=["INC0002"],
        fields={},
        reason="remove a resolved incident",
    )


def test_needs_approval_creates_one_pending_and_blocks() -> None:
    """(a) A needs_approval write records exactly one pending request and does not mutate."""
    gw = make_gateway(fresh_mock(seeded=True), Policy())
    req = _protected_update()

    before = snapshot(gw._instance)
    with pytest.raises(ApprovalRequired):
        run(gw.guarded_write(req))
    assert snapshot(gw._instance) == before  # no mutation

    pending = gw.list_pending_approvals()
    assert len(pending) == 1
    approval = pending[0]
    assert approval.status == "pending"
    assert approval.agent_id == "agent-1"
    assert approval.table == "incident"
    assert approval.operation == "update"
    assert approval.record_ids == ["INC0001"]
    # audit-safe: no field VALUES are present on the summary.
    assert "FIELDVALUE-SENTINEL" not in approval.model_dump_json()
    assert approval.fingerprint == fingerprint_request(req)

    # Re-issuing the same gated write reuses the pending request (dedupe).
    with pytest.raises(ApprovalRequired):
        run(gw.guarded_write(req))
    assert len(gw.list_pending_approvals()) == 1


def test_approve_then_identical_write_applies() -> None:
    """(b) After approve_request, re-issuing the exact same write applies."""
    gw = make_gateway(fresh_mock(seeded=True), Policy())
    req = _protected_update()

    with pytest.raises(ApprovalRequired):
        run(gw.guarded_write(req))
    approval_id = gw.list_pending_approvals()[0].approval_id

    gw.approve_request(approval_id, "operator")

    result = run(gw.guarded_write(req))
    assert result.applied is True
    assert gw._instance._tables["incident"]["INC0001"]["note"] == "FIELDVALUE-SENTINEL"
    # Approving clears it from the pending list.
    assert gw.list_pending_approvals() == []


def test_reject_keeps_write_blocked() -> None:
    """(c) After reject_request, the same write stays blocked and does not apply."""
    gw = make_gateway(fresh_mock(seeded=True), Policy())
    req = _protected_update()

    with pytest.raises(ApprovalRequired):
        run(gw.guarded_write(req))
    approval_id = gw.list_pending_approvals()[0].approval_id

    gw.reject_request(approval_id, "operator")

    before = snapshot(gw._instance)
    with pytest.raises(ApprovalRequired):
        run(gw.guarded_write(req))
    assert snapshot(gw._instance) == before
    assert "note" not in gw._instance._tables["incident"]["INC0001"]


def test_approval_never_overrides_kill_switch() -> None:
    """(d) With the kill switch engaged, approving does NOT let the write through."""
    gw = make_gateway(fresh_mock(seeded=True), Policy())
    req = _protected_update()

    # Create and approve the request while writes are still permitted.
    with pytest.raises(ApprovalRequired):
        run(gw.guarded_write(req))
    approval_id = gw.list_pending_approvals()[0].approval_id
    gw.approve_request(approval_id, "operator")

    # Engage the kill switch; the approved write must still be blocked.
    gw.engage_kill_switch("operator")
    before = snapshot(gw._instance)
    with pytest.raises(KillSwitchEngaged):
        run(gw.guarded_write(req))
    assert snapshot(gw._instance) == before


def test_hard_deny_is_never_turned_into_approval() -> None:
    """(e) A hard-deny request raises PolicyDenied and creates no pending approval."""
    gw = make_gateway(fresh_mock(seeded=True), Policy())
    req = _hard_delete()

    before = snapshot(gw._instance)
    with pytest.raises(PolicyDenied):
        run(gw.guarded_write(req))
    assert snapshot(gw._instance) == before
    assert gw.list_pending_approvals() == []


def test_approve_and_reject_each_audit_the_approver() -> None:
    """(f) approve and reject each append an audit entry naming the approver."""
    gw = make_gateway(fresh_mock(seeded=True), Policy())

    # First pending request -> approve.
    req1 = _protected_update()
    with pytest.raises(ApprovalRequired):
        run(gw.guarded_write(req1))
    id1 = gw.list_pending_approvals()[0].approval_id
    gw.approve_request(id1, "alice")

    # A distinct pending request -> reject.
    req2 = req1.model_copy(update={"record_ids": ["INC0002"], "fields": {"priority": "1"}})
    with pytest.raises(ApprovalRequired):
        run(gw.guarded_write(req2))
    id2 = next(a.approval_id for a in gw.list_pending_approvals())
    gw.reject_request(id2, "bob")

    log = gw.audit_log()
    approve_entries = [e for e in log if e.operation == "approve"]
    reject_entries = [e for e in log if e.operation == "reject"]
    assert len(approve_entries) == 1
    assert len(reject_entries) == 1
    assert approve_entries[0].agent_id == "alice"
    assert reject_entries[0].agent_id == "bob"


def test_unknown_approval_id_raises_invalid_request() -> None:
    """approve/reject on an unknown id raise the typed InvalidRequest error."""
    gw = make_gateway(fresh_mock(seeded=True), Policy())
    with pytest.raises(InvalidRequest):
        gw.approve_request("does-not-exist", "operator")
    with pytest.raises(InvalidRequest):
        gw.reject_request("does-not-exist", "operator")


def test_fingerprint_binds_to_exact_request() -> None:
    """The fingerprint changes when any field value changes (binds to exact request)."""
    store = ApprovalStore()
    req = _protected_update()
    store.create(req, risk_score=50)
    assert store.is_approved(fingerprint_request(req)) is False

    store.set_status(store.list_pending()[0].approval_id, "approved")
    assert store.is_approved(fingerprint_request(req)) is True

    # A different field value yields a different fingerprint -> not approved.
    other = req.model_copy(update={"fields": {"priority": "3"}})
    assert store.is_approved(fingerprint_request(other)) is False
