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

    async def get_record(self, table: str, sys_id: str) -> dict[str, str] | None:
        """Read a single record's current fields. Read-only; never mutates."""
        return self.get(table, sys_id)

    async def search(
        self, table: str, query: str, limit: int, fields: list[str] | None
    ) -> list[dict[str, str]]:
        """Search in-memory records matching ``query``. Read-only; never mutates.

        An empty/whitespace query matches all records. Otherwise the query is
        split on ``^`` into ``field=value`` clauses that are ANDed together: a
        record matches only when ``record.get(field) == value`` for every clause.
        The result is capped to at most 50 and projected to ``fields`` (keeping
        only keys that exist) when ``fields`` is provided.
        """
        records = list(self._tables.get(table, {}).values())
        if query.strip():
            clauses = [clause for clause in query.split("^") if clause]
            matched: list[dict[str, str]] = []
            for record in records:
                if all(
                    "=" in clause and record.get(clause.split("=", 1)[0]) == clause.split("=", 1)[1]
                    for clause in clauses
                ):
                    matched.append(record)
            records = matched
        capped = records[: min(max(limit, 1), 50)]
        if fields:
            return [{k: record[k] for k in fields if k in record} for record in capped]
        return [dict(record) for record in capped]

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
