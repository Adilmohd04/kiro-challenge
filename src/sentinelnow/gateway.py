"""The gateway ties policy + mock instance + audit together. This is what the MCP
tools call. Pure decision logic lives in policy.py; this orchestrates I/O.
"""

from __future__ import annotations

from .audit import AuditEntry, AuditLog
from .errors import ApprovalRequired, KillSwitchEngaged, PolicyDenied
from .instance import ServiceNowInstance
from .models import Decision, Effect, WriteRequest, WriteResult
from .policy import Policy, evaluate_policy


class Gateway:
    def __init__(self, instance: ServiceNowInstance, policy: Policy) -> None:
        self._instance = instance
        self._policy = policy
        self._audit = AuditLog()

    # --- read-only, never mutates ---
    async def preview_change(self, req: WriteRequest) -> list[dict[str, str]]:
        return await self._instance.preview(req)

    def policy_check(self, req: WriteRequest) -> Decision:
        return evaluate_policy(req, self._policy)

    # --- the only path that mutates ---
    async def guarded_write(self, req: WriteRequest) -> WriteResult:
        decision = evaluate_policy(req, self._policy)

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
