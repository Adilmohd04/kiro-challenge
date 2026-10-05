"""SQLite-backed persistent audit store.

A drop-in replacement for the in-memory :class:`~sentinelnow.audit.AuditLog` that
persists the append-only audit trail to a SQLite database file (or ``:memory:``).
It satisfies the :class:`~sentinelnow.audit.AuditStore` Protocol and builds entries
through the shared ``build_entry`` / ``build_event_entry`` helpers so it cannot
drift from the in-memory store.

Security rule: the table columns are EXACTLY the closed :class:`AuditEntry` key set.
``WriteRequest.fields`` contents and any credential/secret value are never read or
persisted here — only IDs and the audit fields.
"""

from __future__ import annotations

import json
import sqlite3

from .audit import AuditEntry, build_entry, build_event_entry
from .models import Decision, WriteRequest

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS audit_entries (
    audit_id TEXT PRIMARY KEY,
    timestamp TEXT,
    agent_id TEXT,
    table_name TEXT,
    operation TEXT,
    record_ids TEXT,
    reason TEXT,
    effect TEXT,
    risk_score INTEGER,
    applied INTEGER
)
"""

_INSERT = """
INSERT INTO audit_entries (
    audit_id, timestamp, agent_id, table_name, operation,
    record_ids, reason, effect, risk_score, applied
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_ALL = """
SELECT audit_id, timestamp, agent_id, table_name, operation,
       record_ids, reason, effect, risk_score, applied
FROM audit_entries
ORDER BY rowid
"""


class SqliteAuditStore:
    """Append-only audit store backed by SQLite. Satisfies the AuditStore Protocol."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    def _insert(self, entry: AuditEntry) -> AuditEntry:
        self._conn.execute(
            _INSERT,
            (
                entry.audit_id,
                entry.timestamp,
                entry.agent_id,
                entry.table,
                entry.operation,
                json.dumps(entry.record_ids),
                entry.reason,
                entry.effect,
                entry.risk_score,
                int(entry.applied),
            ),
        )
        self._conn.commit()
        return entry

    def record(self, req: WriteRequest, decision: Decision, applied: bool) -> AuditEntry:
        return self._insert(build_entry(req, decision, applied))

    def record_event(self, actor: str, action: str, result: str) -> AuditEntry:
        return self._insert(build_event_entry(actor, action, result))

    def all(self) -> list[AuditEntry]:
        rows = self._conn.execute(_SELECT_ALL).fetchall()
        return [
            AuditEntry(
                audit_id=row[0],
                timestamp=row[1],
                agent_id=row[2],
                table=row[3],
                operation=row[4],
                record_ids=json.loads(row[5]),
                reason=row[6],
                effect=row[7],
                risk_score=row[8],
                applied=bool(row[9]),
            )
            for row in rows
        ]
