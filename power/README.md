# SentinelNow Power

A Kiro power that bundles the SentinelNow safety gateway for AI agents acting on
ServiceNow. Installing it gives any agent the five guarded tools plus a skill that
enforces the safe operating procedure.

## What's inside
- `plugin.json` — the power manifest (name, version, keywords that auto-activate it).
- `mcp.json` — registers the `sentinelnow` MCP server (the gateway).
- `skills/safe-servicenow-ops/SKILL.md` — the safe operating procedure the agent follows.

## Tools provided (via the MCP server)
`preview_change`, `policy_check`, `guarded_write`, `audit_log`, `kill_switch`.

## Prerequisites
The gateway runs as `python -m sentinelnow.server`. Install the package first:

```bash
pip install -e ".[dev]"   # from the repo root
```

Runs mock-first (no live instance needed). To point at a real ServiceNow Personal
Developer Instance, set `SN_INSTANCE`, `SN_USER`, `SN_PASSWORD` as environment variables.

## Keywords
Mentioning "servicenow", "incident", "cmdb", "change request", or "guardrails" in a
session activates this power on demand.

> This power is original work for the Kiro University Challenge. It uses synthetic/mock
> data only.
