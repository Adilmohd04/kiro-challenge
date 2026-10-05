"""The gateway ties policy + mock instance + audit together. This is what the MCP
tools call. Pure decision logic lives in policy.py; this orchestrates I/O.
"""

from __future__ import annotations

from .audit import AuditEntry, AuditLog
from .errors import ApprovalRequired, KillSwitchEngaged, PolicyDenied
from .instance import ServiceNowInstance
from .models import Decision, Effect, WriteRequest, WriteResult
from .policy import Policy, evaluate_policy
from .tool_models import SearchRecordsRequest


class Gateway:
    def __init__(self, instance: ServiceNowInstance, policy: Policy) -> None:
        self._instance = instance
        self._policy = policy
        self._audit = AuditLog()

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
        return evaluate_policy(req, self._policy, current)

    # --- the only path that mutates ---
    async def guarded_write(self, req: WriteRequest) -> WriteResult:
        current = await self._current_priorities(req)
        decision = evaluate_policy(req, self._policy, current)

        if decision.effect is Effect.DENY:
            self._audit.record(req, decision, applied=False)
            if self._policy.kill_switch_engaged:
                raise KillSwitchEngaged(decision.reason)
            raise PolicyDenied(decision.reason)

        if decision.effect is Effect.NEEDS_APPROVAL:
            self._audit.record(req, decision, applied=False)
            raise ApprovalRequired(decision.reason)

        await self._instance.apply(req)
        entry = self._audit.record(req, decision, applied=True)
        return WriteResult(applied=True, decision=decision, audit_id=entry.audit_id)

    def engage_kill_switch(self, actor: str) -> AuditEntry:
        self._policy.kill_switch_engaged = True
        return self._audit.record_event(actor, "engage", "engaged")

    def release_kill_switch(self, actor: str) -> AuditEntry:
        self._policy.kill_switch_engaged = False
        return self._audit.record_event(actor, "release", "released")

    def audit_log(self) -> list[AuditEntry]:
        return self._audit.all()
