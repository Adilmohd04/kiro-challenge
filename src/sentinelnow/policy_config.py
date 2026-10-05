"""Boundary parsing for a configurable policy file. This is the ONLY place that
reads policy config I/O — ``policy.py`` stays pure and synchronous.

``load_policy`` reads a JSON or TOML file (path from ``SENTINELNOW_POLICY``),
validates it through the pydantic ``PolicyConfig`` boundary model, and builds the
pure ``Policy`` object the gateway passes into ``evaluate_policy``. When no path is
given it returns ``Policy()`` with today's safe defaults. Any missing/malformed
config is wrapped in the typed ``InvalidPolicyConfig`` naming the problem — the
message carries the file path and offending field/section only, never a secret.
"""

from __future__ import annotations

import json
import tomllib

from pydantic import BaseModel, ValidationError

from .errors import InvalidPolicyConfig
from .policy import Policy, TableRule


class TableRuleConfig(BaseModel):
    """Boundary model for a per-table override. Mirrors ``TableRule``."""

    model_config = {"extra": "forbid"}

    max_batch_size: int | None = None
    allow_delete: bool | None = None
    protected_priorities: list[str] | None = None


class PolicyConfig(BaseModel):
    """Boundary model mirroring the configurable Policy fields.

    ``kill_switch_engaged`` is intentionally NOT configurable here: the kill switch
    is runtime state toggled via the gateway, not a file setting.
    """

    model_config = {"extra": "forbid"}

    max_batch_size: int = 10
    allow_delete: bool = False
    protected_priorities: list[str] = ["1"]
    table_overrides: dict[str, TableRuleConfig] = {}
    max_writes_per_minute: int | None = None


def load_policy(path: str | None) -> Policy:
    """Load a :class:`Policy` from ``path`` (JSON or TOML), or safe defaults if None.

    Parsing is selected by file extension: ``.toml`` via stdlib ``tomllib`` (binary
    read), anything else as JSON. ``FileNotFoundError``, decode errors, and pydantic
    ``ValidationError`` are all wrapped in :class:`InvalidPolicyConfig` naming the
    file and the offending field/section — never a secret value.
    """
    if path is None:
        return Policy()

    try:
        if path.lower().endswith(".toml"):
            with open(path, "rb") as handle:
                raw = tomllib.load(handle)
        else:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
    except FileNotFoundError as exc:
        raise InvalidPolicyConfig(f"Policy config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise InvalidPolicyConfig(f"Malformed TOML in policy config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InvalidPolicyConfig(f"Malformed JSON in policy config {path}: {exc}") from exc

    try:
        config = PolicyConfig.model_validate(raw)
    except ValidationError as exc:
        fields = ", ".join(".".join(str(loc) for loc in err["loc"]) for err in exc.errors())
        raise InvalidPolicyConfig(
            f"Invalid policy config {path}: problem with field(s) {fields}"
        ) from exc

    return Policy(
        max_batch_size=config.max_batch_size,
        allow_delete=config.allow_delete,
        protected_priorities=config.protected_priorities,
        table_overrides={
            table: TableRule(
                max_batch_size=rule.max_batch_size,
                allow_delete=rule.allow_delete,
                protected_priorities=rule.protected_priorities,
            )
            for table, rule in config.table_overrides.items()
        },
        max_writes_per_minute=config.max_writes_per_minute,
    )
