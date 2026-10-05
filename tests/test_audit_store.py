"""Tests for the SQLite-backed persistent audit store (FEAT-002).

All data here is synthetic (security steering: synthetic/mock data only, no real
instance data, no secrets). The sentinel test mirrors the Property 11 approach to
prove ``WriteRequest.fields`` contents never reach the DB.
"""

from __future__ import annotations

from pathlib import Path

from sentinelnow.audit import AuditStore
from sentinelnow.audit_store import SqliteAuditStore
from sentinelnow.models import Decision, Effect, Operation, WriteRequest

# The closed AuditEntry key set — must match audit.AuditEntry exactly.
EXPECTED_KEYS = {
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


def _req(reason: str = "synthetic change", **overrides: object) -> WriteRequest:
    base: dict[str, object] = {
        "agent_id": "agent-1",
        "table": "incident",
        "operation": Operation.UPDATE,
        "record_ids": ["INC0001"],
        "fields": {"state": "3"},
        "reason": reason,
    }
    base.update(overrides)
    return WriteRequest(**base)  # type: ignore[arg-type]


def _decision() -> Decision:
    return Decision(effect=Effect.ALLOW, reason="allowed", risk_score=10)


def test_store_satisfies_protocol() -> None:
    assert isinstance(SqliteAuditStore(":memory:"), AuditStore)


def test_record_then_all_returns_entry() -> None:
    store = SqliteAuditStore(":memory:")
    entry = store.record(_req(), _decision(), applied=True)

    entries = store.all()
    assert len(entries) == 1
    assert entries[0] == entry
    assert entries[0].applied is True
    assert entries[0].record_ids == ["INC0001"]


def test_record_event_round_trips() -> None:
    store = SqliteAuditStore(":memory:")
    entry = store.record_event("operator", "engage", "engaged")

    (stored,) = store.all()
    assert stored == entry
    assert stored.table == "__killswitch__"
    assert stored.operation == "engage"
    assert stored.applied is True


def test_persistence_across_reopen(tmp_path: Path) -> None:
    db_path = str(tmp_path / "audit.db")

    writer = SqliteAuditStore(db_path)
    entry = writer.record(_req(), _decision(), applied=True)

    reopened = SqliteAuditStore(db_path)
    entries = reopened.all()
    assert len(entries) == 1
    assert entries[0] == entry


def test_append_only_preserves_earlier_entries(tmp_path: Path) -> None:
    db_path = str(tmp_path / "audit.db")
    store = SqliteAuditStore(db_path)

    first = store.record(_req("first"), _decision(), applied=True)
    before = store.all()
    assert before == [first]

    second = store.record(_req("second"), _decision(), applied=False)
    third = store.record_event("operator", "release", "released")

    after = store.all()
    # Insertion order preserved and earlier entries unchanged.
    assert after[0] == first
    assert after == [first, second, third]
    # audit_ids are unique.
    ids = [e.audit_id for e in after]
    assert len(ids) == len(set(ids))


def test_closed_key_set_and_no_secret_leak(tmp_path: Path) -> None:
    """Entries carry exactly the closed key set and no fields value reaches the DB."""
    db_path = str(tmp_path / "audit.db")
    store = SqliteAuditStore(db_path)

    sentinels = ("S3CRET-SENTINEL", "AKIA-SENTINEL")
    req = _req(fields={"password": sentinels[0], "api_key": sentinels[1]})
    entry = store.record(req, _decision(), applied=True)

    # (a) exactly the closed key set
    assert set(entry.model_dump().keys()) == EXPECTED_KEYS

    # (b) no sentinel appears in the serialized entry or anywhere in the raw DB.
    assert sentinels[0] not in entry.model_dump_json()
    assert sentinels[1] not in entry.model_dump_json()

    raw = Path(db_path).read_bytes()
    for sentinel in sentinels:
        assert sentinel.encode() not in raw
