"""Tests for the configurable policy file boundary (FEAT-004).

Covers load_policy parsing (JSON + TOML), per-table overrides applied through the
pure evaluate_policy, the per-agent rate limit tripping to NEEDS_APPROVAL, and
malformed-config handling that raises InvalidPolicyConfig naming the problem with
no secret value. All config files are synthetic temp files (security steering:
synthetic data only, no secrets, no real-instance data).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinelnow.errors import InvalidPolicyConfig
from sentinelnow.models import Effect, Operation, WriteRequest
from sentinelnow.policy import Policy, evaluate_policy
from sentinelnow.policy_config import load_policy


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_load_policy_none_returns_safe_defaults() -> None:
    """No path => the safe-default Policy, identical to today's bare Policy()."""
    policy = load_policy(None)
    assert policy == Policy()
    assert policy.allow_delete is False
    assert policy.max_batch_size == 10
    assert policy.protected_priorities == ["1"]
    assert policy.table_overrides == {}
    assert policy.max_writes_per_minute is None


def test_json_config_overrides_defaults(tmp_path: Path) -> None:
    """(a) A JSON config overrides defaults and load_policy reflects it."""
    path = _write(
        tmp_path / "policy.json",
        json.dumps(
            {
                "allow_delete": True,
                "max_batch_size": 25,
                "protected_priorities": ["1", "2"],
                "max_writes_per_minute": 5,
            }
        ),
    )
    policy = load_policy(path)
    assert policy.allow_delete is True
    assert policy.max_batch_size == 25
    assert policy.protected_priorities == ["1", "2"]
    assert policy.max_writes_per_minute == 5


def test_toml_config_loads_equivalently(tmp_path: Path) -> None:
    """(b) A TOML config loads to the same Policy as the equivalent JSON."""
    path = _write(
        tmp_path / "policy.toml",
        'allow_delete = true\nmax_batch_size = 25\nprotected_priorities = ["1", "2"]\n'
        "max_writes_per_minute = 5\n",
    )
    policy = load_policy(path)
    assert policy.allow_delete is True
    assert policy.max_batch_size == 25
    assert policy.protected_priorities == ["1", "2"]
    assert policy.max_writes_per_minute == 5


def test_per_table_override_applies_to_matching_table_only(tmp_path: Path) -> None:
    """(c) A per-table override changes the decision for that table only.

    The base policy denies deletes; the ``incident`` override allows them. A delete
    on ``incident`` is therefore no longer denied for being a delete, while a delete
    on another table stays denied.
    """
    path = _write(
        tmp_path / "policy.json",
        json.dumps(
            {
                "allow_delete": False,
                "table_overrides": {"incident": {"allow_delete": True}},
            }
        ),
    )
    policy = load_policy(path)
    assert policy.table_overrides["incident"].allow_delete is True

    incident_delete = WriteRequest(
        agent_id="agent-1",
        table="incident",
        operation=Operation.DELETE,
        record_ids=["INC0001"],
        fields={},
        reason="override lets this table delete",
    )
    other_delete = incident_delete.model_copy(update={"table": "change_request"})

    # The override table escapes the delete deny; the base table does not.
    assert evaluate_policy(incident_delete, policy).reason != "Deletes are not allowed"
    assert evaluate_policy(other_delete, policy).effect is Effect.DENY


def test_rate_limit_trips_to_needs_approval(tmp_path: Path) -> None:
    """(d) With a limit set and recent_write_count >= limit, result is NEEDS_APPROVAL."""
    path = _write(tmp_path / "policy.json", json.dumps({"max_writes_per_minute": 3}))
    policy = load_policy(path)

    req = WriteRequest(
        agent_id="agent-1",
        table="incident",
        operation=Operation.CREATE,
        record_ids=["INC0100"],
        fields={},
        reason="low-risk create to isolate the rate-limit rule",
    )
    # Under the limit: not gated by the rate limit.
    assert evaluate_policy(req, policy, recent_write_count=2).effect is Effect.ALLOW
    # At the limit: needs approval, with a clear reason.
    decision = evaluate_policy(req, policy, recent_write_count=3)
    assert decision.effect is Effect.NEEDS_APPROVAL
    assert "Rate limit" in decision.reason


def test_rate_limit_does_not_override_kill_switch_or_deny(tmp_path: Path) -> None:
    """Precedence holds: kill switch > hard deny > rate-limit approval."""
    path = _write(tmp_path / "policy.json", json.dumps({"max_writes_per_minute": 1}))
    policy = load_policy(path)

    delete_req = WriteRequest(
        agent_id="agent-1",
        table="incident",
        operation=Operation.DELETE,
        record_ids=["INC0001"],
        fields={},
        reason="delete should hard-deny before the rate limit is consulted",
    )
    # Hard deny (delete not allowed) wins over the rate limit.
    assert evaluate_policy(delete_req, policy, recent_write_count=99).effect is Effect.DENY
    # Kill switch wins over everything.
    killed = policy.model_copy(update={"kill_switch_engaged": True})
    denied = evaluate_policy(delete_req, killed, recent_write_count=99)
    assert denied.effect is Effect.DENY
    assert denied.reason == "Kill switch engaged"


def test_missing_file_raises_invalid_policy_config(tmp_path: Path) -> None:
    """A missing path raises InvalidPolicyConfig naming the file."""
    missing = str(tmp_path / "does_not_exist.json")
    with pytest.raises(InvalidPolicyConfig) as exc:
        load_policy(missing)
    assert missing in str(exc.value)


def test_malformed_json_raises_invalid_policy_config(tmp_path: Path) -> None:
    """(e) Malformed JSON raises InvalidPolicyConfig naming the problem, no secret."""
    path = _write(tmp_path / "policy.json", "{ not valid json ")
    with pytest.raises(InvalidPolicyConfig) as exc:
        load_policy(path)
    assert path in str(exc.value)


def test_malformed_toml_raises_invalid_policy_config(tmp_path: Path) -> None:
    """Malformed TOML raises InvalidPolicyConfig naming the file."""
    path = _write(tmp_path / "policy.toml", "allow_delete = = true")
    with pytest.raises(InvalidPolicyConfig) as exc:
        load_policy(path)
    assert path in str(exc.value)


def test_wrong_type_field_raises_naming_the_field_without_secret(tmp_path: Path) -> None:
    """(e) A field of the wrong type raises InvalidPolicyConfig naming the field.

    The config also carries a synthetic secret-looking value; it must NOT appear in
    the error message.
    """
    secret_sentinel = "S3CRET-SENTINEL-DO-NOT-LEAK"
    path = _write(
        tmp_path / "policy.json",
        json.dumps(
            {
                "max_batch_size": "not-an-int",
                "protected_priorities": [secret_sentinel],
            }
        ),
    )
    with pytest.raises(InvalidPolicyConfig) as exc:
        load_policy(path)
    message = str(exc.value)
    assert "max_batch_size" in message
    assert secret_sentinel not in message
