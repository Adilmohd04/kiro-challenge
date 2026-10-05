"""Tests for the read-only FastAPI audit dashboard (FEAT-005).

All data here is synthetic (security steering: synthetic/mock data only, no real
instance data, no secrets). The gateway is seeded by driving it directly against a
seeded ``MockServiceNow`` via the shared helpers from ``test_gateway_properties``.

The central guarantee under test is that the dashboard is READ-ONLY: every GET
only reads gateway state, and the audit log length is unchanged after the GETs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sentinelnow.dashboard import create_app
from sentinelnow.errors import ApprovalRequired
from sentinelnow.gateway import Gateway
from sentinelnow.models import Operation, WriteRequest
from sentinelnow.policy import Policy
from test_gateway_properties import fresh_mock, make_gateway, run

# The closed audit key set every entry must carry (mirrors AuditEntry / Property 11).
_AUDIT_KEYS = {
    "audit_id",
    "timestamp",
    "agent_id",
    "table",
    "operation",
    "record_ids",
    "reason",
    "effect",
    "risk_score",
    "applied",
}

# Fields the pending-approval summary exposes for review (audit-safe; no values).
_APPROVAL_KEYS = {
    "approval_id",
    "created_at",
    "status",
    "fingerprint",
    "agent_id",
    "table",
    "operation",
    "record_ids",
    "reason",
    "risk_score",
}


def _allowed_update() -> WriteRequest:
    """An UPDATE to a non-protected incident -> policy ALLOWs and it applies."""
    return WriteRequest(
        agent_id="agent-ok",
        table="incident",
        operation=Operation.UPDATE,
        record_ids=["INC0002"],
        fields={"state": "3"},
        reason="advance a routine incident",
    )


def _protected_update() -> WriteRequest:
    """An UPDATE to the seeded P1 incident -> policy returns NEEDS_APPROVAL."""
    return WriteRequest(
        agent_id="agent-needs",
        table="incident",
        operation=Operation.UPDATE,
        record_ids=["INC0001"],
        # "1" is protected -> NEEDS_APPROVAL; the sentinel proves field VALUES
        # never surface through any dashboard endpoint.
        fields={"priority": "1", "note": "FIELDVALUE-SENTINEL"},
        reason="touch a P1 incident",
    )


def _seeded_gateway() -> Gateway:
    """A gateway with one applied write, one pending approval, and the kill switch on."""
    gw = make_gateway(fresh_mock(seeded=True), Policy())
    # One applied (ALLOW) write -> an applied audit entry.
    run(gw.guarded_write(_allowed_update()))
    # One NEEDS_APPROVAL write -> a pending approval + a not-applied audit entry.
    with pytest.raises(ApprovalRequired):
        run(gw.guarded_write(_protected_update()))
    # A kill-switch engage -> engaged state + an audited event.
    gw.engage_kill_switch("operator-1")
    return gw


def test_api_audit_returns_closed_key_set() -> None:
    gw = _seeded_gateway()
    client = TestClient(create_app(gw))

    resp = client.get("/api/audit")
    assert resp.status_code == 200
    entries = resp.json()
    assert isinstance(entries, list)
    assert entries, "expected seeded audit entries"
    for entry in entries:
        assert isinstance(entry, dict)
        # Exactly the closed key set — no extra keys could leak fields/secrets.
        assert set(entry.keys()) == _AUDIT_KEYS
    # Field VALUES never appear in the audit JSON.
    assert "FIELDVALUE-SENTINEL" not in resp.text


def test_api_pending_returns_approval_summaries() -> None:
    gw = _seeded_gateway()
    client = TestClient(create_app(gw))

    resp = client.get("/api/pending")
    assert resp.status_code == 200
    pending = resp.json()
    assert isinstance(pending, list)
    assert len(pending) == 1
    approval = pending[0]
    assert set(approval.keys()) == _APPROVAL_KEYS
    assert approval["status"] == "pending"
    assert approval["table"] == "incident"
    assert approval["operation"] == "update"
    # audit-safe: field VALUES never surface in the pending summary.
    assert "FIELDVALUE-SENTINEL" not in resp.text


def test_api_killswitch_reports_state() -> None:
    gw = _seeded_gateway()
    client = TestClient(create_app(gw))

    resp = client.get("/api/killswitch")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"engaged": True}
    assert isinstance(body["engaged"], bool)


def test_index_returns_html() -> None:
    gw = _seeded_gateway()
    client = TestClient(create_app(gw))

    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "SentinelNow" in resp.text


def test_dashboard_performs_no_writes() -> None:
    """Every endpoint is read-only: the audit log length is unchanged after GETs."""
    gw = _seeded_gateway()
    client = TestClient(create_app(gw))

    before = len(gw.audit_log())
    pending_before = len(gw.list_pending_approvals())
    killswitch_before = gw.kill_switch_engaged()

    # Hit every endpoint, repeatedly, and confirm nothing mutates.
    for _ in range(3):
        client.get("/api/audit")
        client.get("/api/pending")
        client.get("/api/killswitch")
        client.get("/")

    assert len(gw.audit_log()) == before
    assert len(gw.list_pending_approvals()) == pending_before
    assert gw.kill_switch_engaged() == killswitch_before
