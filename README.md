# SentinelNow

An AI-agent safety gateway for ServiceNow.

ServiceNow already lets AI agents (Claude, Codex, Copilot, Kiro) connect to an instance
over MCP. The open problem in 2026 is **governance of what those agents actually do** —
an over-permissioned agent can bulk-close incidents, corrupt the CMDB, or delete records
with nobody watching. SentinelNow sits between the agent and ServiceNow and makes every
write **previewable, risk-scored, policy-gated, auditable, and reversible via a kill switch.**

## Core tools (exposed over MCP)
- `preview_change` — dry-run; show what would change. Never mutates.
- `policy_check` — risk-score an intended action -> allow / needs_approval / deny.
- `guarded_write` — apply a change only if it passes policy.
- `audit_log` — who / what / why / result for every action.
- `kill_switch` — instantly block all writes.

## Status
Runs **mock-first** against an in-memory ServiceNow so it works with no live instance.
Point it at a free Personal Developer Instance (PDI) later via `SN_INSTANCE` / `SN_USER`
/ `SN_PASSWORD` environment variables.

## Quickstart
```bash
pip install -e ".[dev]"
python -m pytest            # run tests (incl. property-based)
python -m sentinelnow.server  # initialize the gateway (mock mode)
```

## Kiro University Challenge — lesson map
- **L1 Specs** -> `.kiro/specs/sentinelnow/` (done in a Spec session)
- **L2 Steering** -> `.kiro/steering/security.md`, `conventions.md`
- **L3 Hooks** -> `.kiro/hooks/pytest-on-save.json`
- **L4 Property-based testing** -> `tests/test_policy_properties.py`
- **L5 Powers** -> install a power and use it
- **L6 MCP** -> `mcp.sample.json` (register via the Kiro MCP panel)
- **L7 Custom agent** -> `.kiro/agents/servicenow-safe-ops.json`
- **Bonus 2** -> package SentinelNow as a shareable power

> All code is original and uses synthetic/mock data only.
