"""The gateway ties policy + mock instance + audit together. This is what the MCP
tools call. Pure decision logic lives in policy.py; this orchestrates I/O.
"""

from __future__ import annotations

import time
from collections import deque

from .approvals import ApprovalRequest, ApprovalStore, fingerprint_request
from .audit import AuditEntry, AuditLog, AuditStore
from .errors import ApprovalRequired, InvalidRequest, KillSwitchEngaged, PolicyDenied
from .instance import ServiceNowInstance
from .models import Decision, Effect, WriteRequest, WriteResult
from .policy import Policy, evaluate_policy
from .tool_models import SearchRecordsRequest


class Gateway:
    def __init__(
        self,
        instance: ServiceNowInstance,
        policy: Policy,
        audit: AuditStore | None = None,
    ) -> None:
        self._instance = instance
        self._policy = policy
        self._audit: AuditStore = audit if audit is not None else AuditLog()
        self._approvals = ApprovalStore()
        # Per-agent applied-write timestamps (epoch seconds), trimmed to the
        # trailing 60s. Pure in-memory bookkeeping — all timing stays in the
        # gateway so evaluate_policy receives only a plain count.
        self._write_times: dict[str, deque[float]] = {}

    _RATE_WINDOW_SECONDS: float = 60.0

    def _recent_write_count(self, agent_id: str, now: float) -> int:
        """Count writes this agent applied in the trailing 60s, trimming old ones."""
        times = self._write_times.get(agent_id)
        if times is None:
            return 0
        cutoff = now - self._RATE_WINDOW_SECONDS
        while times and times[0] < cutoff:
            times.popleft()
        return len(times)

    def _record_write_time(self, agent_id: str, now: float) -> None:
        """Append a timestamp for an actually-applied write by this agent."""
        self._write_times.setdefault(agent_id, deque()).append(now)

    async def _current_priorities(self, req: WriteRequest) -> list[str]:
        """Read the CURRENT priority of each targeted record. Read-only.

        Awaits the async instance Protocol for every ``sys_id`` in
        ``req.record_ids`` and collects the ``"priority"`` value when the record
        exists and has one (missing records / fields are skipped). The result is
        passed into the pure ``evaluate_policy`` so policy can gate on the
        record's existing state as well as the intended change.
        """
        priorities: list[str] = []
        for sys_id in req.record_ids:
            record = await self._instance.get_record(req.table, sys_id)
            if record is not None:
                priority = record.get("priority")
                if priority is not None:
                    priorities.append(priority)
        return priorities

    # --- read-only, never mutates ---
    async def preview_change(self, req: WriteRequest) -> list[dict[str, str]]:
        return await self._instance.preview(req)

    async def search_records(self, req: SearchRecordsRequest) -> list[dict[str, str]]:
        """Read-only record lookup. Never mutates; unaudited (mirrors preview_change)."""
        return await self._instance.search(req.table, req.query, req.limit, req.fields)

    async def policy_check(self, req: WriteRequest) -> Decision:
        current = await self._current_priorities(req)
        recent = self._recent_write_count(req.agent_id, time.monotonic())
        return evaluate_policy(req, self._policy, current, recent_write_count=recent)

    # --- the only path that mutates ---
    async def guarded_write(self, req: WriteRequest) -> WriteResult:
        current = await self._current_priorities(req)
        now = time.monotonic()
        recent = self._recent_write_count(req.agent_id, now)
        decision = evaluate_policy(req, self._policy, current, recent_write_count=recent)

        # Precedence is explicit and ordered: kill switch > deny > approval.
        # (1)+(2): a DENY always wins. An engaged kill switch produces a DENY
        # decision; an approval can NEVER override a kill switch or a hard deny.
        if decision.effect is Effect.DENY:
            self._audit.record(req, decision, applied=False)
            if self._policy.kill_switch_engaged:
                raise KillSwitchEngaged(decision.reason)
            raise PolicyDenied(decision.reason)

        # (3) NEEDS_APPROVAL: a prior approval of this EXACT request bypasses ONLY
        # the approval gate; otherwise record/reuse a pending request and block.
        if decision.effect is Effect.NEEDS_APPROVAL:
            if not self._approvals.is_approved(fingerprint_request(req)):
                self._approvals.create(req, decision.risk_score)
                self._audit.record(req, decision, applied=False)
                raise ApprovalRequired(decision.reason)

        # (4) ALLOW (or an approved NEEDS_APPROVAL) -> apply.
        await self._instance.apply(req)
        self._record_write_time(req.agent_id, now)
        entry = self._audit.record(req, decision, applied=True)
        return WriteResult(applied=True, decision=decision, audit_id=entry.audit_id)

    # --- approval workflow (human-in-the-loop) ---
    def list_pending_approvals(self) -> list[ApprovalRequest]:
        """Return every pending approval request (audit-safe summaries). Read-only."""
        return self._approvals.list_pending()

    def approve_request(self, approval_id: str, approver: str) -> ApprovalRequest:
        """Approve a pending request and audit the approver. Raises if id unknown."""
        try:
            approval = self._approvals.set_status(approval_id, "approved")
        except KeyError as exc:
            raise InvalidRequest(f"Unknown approval_id: {approval_id}") from exc
        self._audit.record_event(approver, "approve", approval_id)
        return approval

    def reject_request(self, approval_id: str, approver: str) -> ApprovalRequest:
        """Reject a pending request and audit the approver. Raises if id unknown."""
        try:
            approval = self._approvals.set_status(approval_id, "rejected")
        except KeyError as exc:
            raise InvalidRequest(f"Unknown approval_id: {approval_id}") from exc
        self._audit.record_event(approver, "reject", approval_id)
        return approval

    def engage_kill_switch(self, actor: str) -> AuditEntry:
        self._policy.kill_switch_engaged = True
        return self._audit.record_event(actor, "engage", "engaged")

    def release_kill_switch(self, actor: str) -> AuditEntry:
        self._policy.kill_switch_engaged = False
        return self._audit.record_event(actor, "release", "released")

    def audit_log(self) -> list[AuditEntry]:
        return self._audit.all()
