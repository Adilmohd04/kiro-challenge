"""Shared scaffolding for the gateway property-based tests (Properties 8-12).

This module holds ONLY the strategies, fixtures/helpers, and the ``StubServiceNowClient``
backend used by the gateway property tests. The actual property test functions
(Properties 8-12) are added by tasks 6.2-6.6 into this same file.

All backends and data here are synthetic / in-memory (security steering: synthetic
mock data only, no real instance data, no secrets). Async I/O is driven through a
small ``run`` helper so the sync Hypothesis bodies can call the async gateway.

**Validates: Requirements 11.1, 11.3**
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest
from hypothesis import given, settings

# Reuse the request/policy strategies from the policy property tests to avoid
# duplication. The tests directory is placed on sys.path by pytest (the test file's
# own directory is inserted during collection), so this import resolves at runtime.
from test_policy_properties import policies, write_requests  # noqa: F401  (re-exported)

from sentinelnow.errors import ApprovalRequired, KillSwitchEngaged, PolicyDenied
from sentinelnow.gateway import Gateway
from sentinelnow.instance import ServiceNowInstance
from sentinelnow.mock_servicenow import MockServiceNow
from sentinelnow.models import Effect, Operation, WriteRequest
from sentinelnow.policy import Policy

__all__ = [
    "policies",
    "write_requests",
    "fresh_mock",
    "make_gateway",
    "snapshot",
    "run",
    "StubServiceNowClient",
]


def fresh_mock(seeded: bool = True) -> MockServiceNow:
    """Build a FRESH ``MockServiceNow`` per example.

    Property tests must not share mutable backend state between examples, so each
    example gets its own instance. When ``seeded`` is true a couple of synthetic
    incidents are planted so update/delete paths have records to touch. All data is
    synthetic (security steering).
    """
    mock = MockServiceNow()
    if seeded:
        mock.seed("incident", "INC0001", {"priority": "1", "state": "2"})
        mock.seed("incident", "INC0002", {"priority": "3", "state": "2"})
    return mock


def make_gateway(instance: ServiceNowInstance, policy: Policy) -> Gateway:
    """Convenience wrapper to construct a ``Gateway`` over any backend + policy."""
    return Gateway(instance, policy)


def snapshot(instance: Any) -> dict[str, dict[str, dict[str, str]]]:
    """Return a deep copy of a backend's internal table state for mutation detection.

    This deliberately reaches into the backend's private ``_tables`` dict: it is a
    test-only before/after comparison to assert that read-only paths never mutate and
    that denied/approval paths leave the instance unchanged. Both ``MockServiceNow`` and
    ``StubServiceNowClient`` expose ``_tables``, so the same snapshot works for either.
    """
    return copy.deepcopy(instance._tables)


def run(coro: Any) -> Any:
    """Drive an awaitable to completion from a synchronous Hypothesis test body."""
    return asyncio.run(coro)


class StubServiceNowClient:
    """A second in-memory backend satisfying the async ``ServiceNowInstance`` Protocol.

    Used for backend-parity tests (Property 12): the gateway's ``Decision`` must be
    identical whether it runs against ``MockServiceNow`` or this stub. It mirrors
    ``MockServiceNow``'s preview/apply semantics and keeps the same ``_tables`` shape so
    ``snapshot()`` works uniformly. ``isinstance(StubServiceNowClient(), ServiceNowInstance)``
    is True because the Protocol is ``runtime_checkable`` and this class implements both
    async methods. Synthetic in-memory data only (security steering).
    """

    def __init__(self) -> None:
        # table -> {sys_id: {field: value}}, identical shape to MockServiceNow.
        self._tables: dict[str, dict[str, dict[str, str]]] = {}

    def seed(self, table: str, sys_id: str, fields: dict[str, str]) -> None:
        self._tables.setdefault(table, {})[sys_id] = dict(fields)

    def get(self, table: str, sys_id: str) -> dict[str, str] | None:
        return self._tables.get(table, {}).get(sys_id)

    async def preview(self, req: WriteRequest) -> list[dict[str, str]]:
        """Return before/after of what WOULD change. Never mutates (Req 6.2)."""
        changes: list[dict[str, str]] = []
        for sys_id in req.record_ids or ["<new>"]:
            before = self.get(req.table, sys_id) or {}
            after = {**before, **req.fields}
            changes.append({"sys_id": sys_id, "before": str(before), "after": str(after)})
        return changes

    async def apply(self, req: WriteRequest) -> int:
        """Apply the change for real (stub). Returns number of affected records."""
        table = self._tables.setdefault(req.table, {})
        affected = 0
        if req.operation is Operation.DELETE:
            for sys_id in req.record_ids:
                if table.pop(sys_id, None) is not None:
                    affected += 1
            return affected
        for sys_id in req.record_ids or [f"new_{len(table)}"]:
            table[sys_id] = {**table.get(sys_id, {}), **req.fields}
            affected += 1
        return affected


def test_stub_satisfies_protocol() -> None:
    """Sanity check: the stub is a structural ServiceNowInstance and snapshots round-trip."""
    assert isinstance(StubServiceNowClient(), ServiceNowInstance)

    mock = fresh_mock()
    snap = snapshot(mock)
    assert snap == mock._tables  # equal by value...
    assert snap is not mock._tables  # ...but an independent deep copy
    assert snap["incident"]["INC0001"]["priority"] == "1"


# Feature: sentinelnow-mcp-gateway, Property 8: preview_change and policy_check never mutate the instance
# deadline disabled: driving async calls via asyncio.run has first-call warm-up timing
# jitter that is unrelated to the mutation property being verified.
@settings(deadline=None)
@given(req=write_requests, policy=policies)
def test_read_only_tools_never_mutate(req: WriteRequest, policy: Policy) -> None:
    """Property 8: the read-only tools leave the whole instance identical.

    ``preview_change`` and ``policy_check`` are the non-mutating paths: for any
    request and any policy, the backend's table state must be byte-for-byte
    identical before and after calling them (clarified Req 6.3), and
    ``policy_check`` must return a Decision whose effect is one of the three
    defined effects.

    **Validates: Requirements 6.1, 6.2, 6.3**
    """
    gw = make_gateway(fresh_mock(seeded=True), policy)
    before = snapshot(gw._instance)

    changes = run(gw.preview_change(req))
    decision = gw.policy_check(req)

    after = snapshot(gw._instance)
    assert before == after  # whole-instance state unchanged
    assert isinstance(changes, list)
    assert decision.effect in {Effect.ALLOW, Effect.NEEDS_APPROVAL, Effect.DENY}


# Feature: sentinelnow-mcp-gateway, Property 9: non-ALLOW guarded_write leaves the instance unchanged and raises the matching typed exception
# deadline disabled: asyncio.run warm-up timing jitter, unrelated to the property.
@settings(deadline=None)
@given(req=write_requests, policy=policies)
def test_non_allow_guarded_write_does_not_mutate(req: WriteRequest, policy: Policy) -> None:
    """Property 9: non-ALLOW writes raise the matching typed exception and never mutate.

    Precedence: an engaged kill switch raises ``KillSwitchEngaged`` and takes
    priority; otherwise a DENY decision raises ``PolicyDenied`` and a
    NEEDS_APPROVAL decision raises ``ApprovalRequired``. In every non-ALLOW case
    the instance is left unchanged. On ALLOW, the write succeeds, returns
    ``applied=True`` with a non-empty ``audit_id``, and the instance MAY change.

    **Validates: Requirements 3.4, 7.2, 7.3, 7.4, 7.5, 8.5**
    """
    gw = make_gateway(fresh_mock(seeded=True), policy)
    decision = gw.policy_check(req)
    before = snapshot(gw._instance)

    if policy.kill_switch_engaged:
        with pytest.raises(KillSwitchEngaged):
            run(gw.guarded_write(req))
        assert snapshot(gw._instance) == before
    elif decision.effect is Effect.DENY:
        with pytest.raises(PolicyDenied):
            run(gw.guarded_write(req))
        assert snapshot(gw._instance) == before
    elif decision.effect is Effect.NEEDS_APPROVAL:
        with pytest.raises(ApprovalRequired):
            run(gw.guarded_write(req))
        assert snapshot(gw._instance) == before
    else:  # ALLOW
        result = run(gw.guarded_write(req))
        assert result.applied is True
        assert result.audit_id is not None and result.audit_id != ""


# Feature: sentinelnow-mcp-gateway, Property 10: each guarded_write records exactly one audit entry whose applied flag matches whether the instance changed
# deadline disabled: asyncio.run warm-up timing jitter, unrelated to the property.
@settings(deadline=None)
@given(req=write_requests, policy=policies)
def test_guarded_write_records_exactly_one_audit_entry(req: WriteRequest, policy: Policy) -> None:
    """Property 10: exactly one audit entry per call; applied flag matches the ALLOW path.

    Whether ``guarded_write`` returns or raises, exactly one ``AuditEntry`` is
    appended. The new entry's ``applied`` flag is True exactly on the ALLOW path
    (the call returns a ``WriteResult``) and False on every non-ALLOW path (the
    call raises a typed exception). A genuine state change therefore implies
    ``applied is True``; the converse need not hold because an ALLOW write can be
    idempotent (e.g. re-writing identical fields) and leave the bytes unchanged
    while still being applied. Across a short sequence of calls the log is
    append-only (earlier entries never change) and every ``audit_id`` is pairwise
    unique.

    **Validates: Requirements 4.3, 5.1, 5.2, 5.3, 5.4, 5.5, 7.6**
    """
    gw = make_gateway(fresh_mock(seeded=True), policy)

    # Drive a short sequence of writes to strengthen uniqueness + append-only checks.
    sequence = [
        req,
        req.model_copy(update={"operation": Operation.CREATE}),
        req.model_copy(update={"reason": req.reason + "-again"}),
    ]

    for item in sequence:
        before_log = gw.audit_log()
        before_count = len(before_log)
        before_snap = snapshot(gw._instance)

        applied_path = False
        try:
            run(gw.guarded_write(item))
            applied_path = True  # ALLOW path returned a WriteResult
        except (KillSwitchEngaged, PolicyDenied, ApprovalRequired):
            applied_path = False  # non-ALLOW path raised a typed exception

        after_log = gw.audit_log()
        # Exactly one entry appended, whether the call returned or raised.
        assert len(after_log) == before_count + 1
        # Append-only: the earlier prefix is unchanged.
        assert after_log[:before_count] == before_log

        # applied flag reflects the ALLOW path exactly.
        assert after_log[-1].applied is applied_path
        # Any genuine mutation can only happen on the applied (ALLOW) path.
        changed = snapshot(gw._instance) != before_snap
        if changed:
            assert after_log[-1].applied is True

    # Audit ids are pairwise unique across the whole log.
    ids = [entry.audit_id for entry in gw.audit_log()]
    assert len(ids) == len(set(ids))


# Feature: sentinelnow-mcp-gateway, Property 11: audit entries contain only the closed key set and never leak WriteRequest.fields values
# deadline disabled: asyncio.run warm-up timing jitter, unrelated to the property.
@settings(deadline=None)
@given(req=write_requests, policy=policies)
def test_audit_entry_excludes_secrets_and_closed_key_set(req: WriteRequest, policy: Policy) -> None:
    """Property 11: audit entries use the closed key set and never leak secret values.

    The request is augmented with obvious synthetic sentinel secrets in its
    ``fields``. After a ``guarded_write``, the last audit entry must serialize to
    exactly the closed key set, and neither sentinel value may appear anywhere in
    the serialized entry (proving ``WriteRequest.fields`` contents are not logged).

    **Validates: Requirements 5.6, 9.1, 9.2**
    """
    expected_keys = {
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
    sentinels = ("S3CRET-SENTINEL", "AKIA-SENTINEL")
    req2 = req.model_copy(
        update={"fields": {**req.fields, "password": sentinels[0], "api_key": sentinels[1]}}
    )

    gw = make_gateway(fresh_mock(seeded=True), policy)
    before_count = len(gw.audit_log())
    try:
        run(gw.guarded_write(req2))
    except (KillSwitchEngaged, PolicyDenied, ApprovalRequired):
        pass

    log = gw.audit_log()
    assert len(log) == before_count + 1
    entry = log[-1]

    # (a) exactly the closed key set
    assert set(entry.model_dump().keys()) == expected_keys

    # (b) no sentinel secret value leaks into the serialized entry
    serialized = entry.model_dump_json()
    for sentinel in sentinels:
        assert sentinel not in serialized


# Feature: sentinelnow-mcp-gateway, Property 12: the Decision is identical whether the gateway is backed by MockServiceNow or StubServiceNowClient
# deadline disabled: asyncio.run warm-up timing jitter, unrelated to the property.
@settings(deadline=None)
@given(req=write_requests, policy=policies)
def test_decision_identical_across_backends(req: WriteRequest, policy: Policy) -> None:
    """Property 12: policy decisions are backend-independent.

    A gateway backed by ``MockServiceNow`` and one backed by
    ``StubServiceNowClient`` (seeded identically) must produce an identical
    ``Decision`` for the same request and policy. ``guarded_write`` must raise the
    same exception type (or both succeed), and the resulting audit entry's
    policy-derived fields (effect, risk_score) must match. Backend-dependent fields
    (audit_id, timestamp) are intentionally not compared.

    **Validates: Requirements 11.3**
    """
    stub = StubServiceNowClient()
    stub.seed("incident", "INC0001", {"priority": "1", "state": "2"})
    stub.seed("incident", "INC0002", {"priority": "3", "state": "2"})

    gw_a = make_gateway(fresh_mock(seeded=True), policy)
    gw_b = make_gateway(stub, policy)

    decision_a = gw_a.policy_check(req)
    decision_b = gw_b.policy_check(req)
    assert decision_a == decision_b

    def attempt(gw: Gateway) -> tuple[type[Exception] | None, Any]:
        try:
            result = run(gw.guarded_write(req))
            return None, result
        except (KillSwitchEngaged, PolicyDenied, ApprovalRequired) as exc:
            return type(exc), None

    exc_a, _ = attempt(gw_a)
    exc_b, _ = attempt(gw_b)
    assert exc_a is exc_b  # same exception type, or both None (success)

    last_a = gw_a.audit_log()[-1]
    last_b = gw_b.audit_log()[-1]
    assert last_a.effect == last_b.effect
    assert last_a.risk_score == last_b.risk_score
