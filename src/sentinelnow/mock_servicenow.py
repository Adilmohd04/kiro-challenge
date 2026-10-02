"""An in-memory mock ServiceNow instance so the gateway runs and tests without a PDI.

Swap this for a real httpx-based REST client (ServiceNowClient) when SN credentials
are provided. The interface is intentionally small.
"""

from __future__ import annotations

from .models import Operation, WriteRequest


class MockServiceNow:
    """Minimal in-memory table store that mimics ServiceNow record operations."""

    def __init__(self) -> None:
        # table -> {sys_id: {field: value}}
        self._tables: dict[str, dict[str, dict[str, str]]] = {}

    def seed(self, table: str, sys_id: str, fields: dict[str, str]) -> None:
        self._tables.setdefault(table, {})[sys_id] = dict(fields)

    def get(self, table: str, sys_id: str) -> dict[str, str] | None:
        return self._tables.get(table, {}).get(sys_id)

    async def preview(self, req: WriteRequest) -> list[dict[str, str]]:
        """Return the before/after of what WOULD change. Never mutates."""
        changes: list[dict[str, str]] = []
        for sys_id in req.record_ids or ["<new>"]:
            before = self.get(req.table, sys_id) or {}
            after = {**before, **req.fields}
            changes.append({"sys_id": sys_id, "before": str(before), "after": str(after)})
        return changes

    async def apply(self, req: WriteRequest) -> int:
        """Apply the change for real (mock). Returns number of affected records."""
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
