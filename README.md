# SentinelNow

An AI-agent safety gateway for ServiceNow.

ServiceNow already lets AI agents (Claude, Codex, Copilot, Kiro) connect to an instance
over MCP. The open problem in 2026 is **governance of what those agents actually do** —
an over-permissioned agent can bulk-close incidents, corrupt the CMDB, or delete records
with nobody watching. SentinelNow sits between the agent and ServiceNow and makes every
write **previewable, risk-scored, policy-gated, auditable, and reversible via a kill switch.**

## Core tools (exposed over MCP)
- `preview_change` — dry-run; show what would change. Never mutates.
- `search_records` — read-only lookup; find records by encoded query. Never mutates.
- `policy_check` — risk-score an intended action -> allow / needs_approval / deny.
- `guarded_write` — apply a change only if it passes policy.
- `audit_log` — who / what / why / result for every action.
- `kill_switch` — instantly block all writes.
- `list_pending_approvals` — read-only; list writes awaiting human approval.
- `approve_request` — approve a pending write (audited; names the approver).
- `reject_request` — reject a pending write (audited; names the approver).

## Status
Runs **mock-first** against an in-memory ServiceNow so it works with no live instance.
Point it at a free Personal Developer Instance (PDI) later via `SN_INSTANCE` / `SN_USER`
/ `SN_PASSWORD` environment variables.

### Configuration (.env)
Credentials are read only from the environment. For local use, copy the template and
fill in real values:

```bash
cp .env.example .env   # then edit .env
```

`.env` is gitignored and must never be committed. `SN_PASSWORD` is held as a pydantic
`SecretStr` and is never logged, printed, or written to the audit log. If no `.env`
exists (or you leave `SENTINELNOW_MODE=mock`), the gateway runs in mock mode.

| Variable | Purpose |
|----------|---------|
| `SENTINELNOW_MODE` | `mock` (default, no instance) or `live` |
| `SN_INSTANCE` | Instance base URL, e.g. `https://dev12345.service-now.com` |
| `SN_USER` | Instance user with the needed Table API roles |
| `SN_PASSWORD` | Password/token for `SN_USER` (secret — never commit) |
| `SENTINELNOW_AUDIT_DB` | Optional SQLite path for a persistent audit trail (unset = in-memory) |
| `SENTINELNOW_POLICY` | Optional JSON/TOML policy file path (unset = safe defaults) |

### Configurable policy file (`SENTINELNOW_POLICY`)
By default the gateway uses the safe built-in guardrails (deletes denied, priority
`1` protected, batch limit of 10). Set `SENTINELNOW_POLICY` to a `.json` or `.toml`
file to override them. When the variable is unset the behavior is exactly as before.

```bash
export SENTINELNOW_POLICY=./sentinelnow-policy.toml
```

The file is parsed and validated **at the boundary** (`policy_config.py`); the pure
`evaluate_policy` only ever receives a `Policy` object and plain counts, so no I/O
leaks into the decision logic. Configurable fields:

- `max_batch_size`, `allow_delete`, `protected_priorities` — the base guardrails.
- `table_overrides` — per-table rules that override the base fields for one table
  only. Each override may set any of `max_batch_size`, `allow_delete`, or
  `protected_priorities`; omitted fields fall back to the base policy, and tables
  with no override keep the base rules.
- `max_writes_per_minute` — a per-agent rate limit. When an agent has already
  applied this many writes in the trailing minute, further writes return
  `needs_approval` (not a hard deny) so a human can authorize a legitimate burst.
  The rate limit is evaluated **after** the kill switch and hard-deny checks, so the
  precedence stays: kill switch > deny > rate-limit approval.

Example TOML:

```toml
max_batch_size = 10
allow_delete = false
protected_priorities = ["1"]
max_writes_per_minute = 30

[table_overrides.incident]
allow_delete = true
protected_priorities = ["1", "2"]
```

A missing or malformed config (bad JSON/TOML, or a field of the wrong type) raises
the typed `InvalidPolicyConfig`, naming the file and the offending field/section and
never echoing a secret value.

### Audit persistence (SQLite)
By default the audit log is held **in memory** and resets when the process exits.
Set `SENTINELNOW_AUDIT_DB` to a file path to persist the append-only audit trail to
SQLite instead:

```bash
export SENTINELNOW_AUDIT_DB=./sentinelnow-audit.db
```

The store records **exactly** the closed audit key set (`audit_id`, `timestamp`,
`agent_id`, `table`, `operation`, `record_ids`, `reason`, `effect`, `risk_score`,
`applied`). It never persists `WriteRequest.fields` contents or any secret/credential
value. It is append-only and survives restarts: reopening the same path replays the
full history. When the variable is unset or empty the gateway behaves exactly as
before, using the in-memory store.

### Approval workflow (human-in-the-loop)
When `policy_check` returns `needs_approval`, `guarded_write` does not silently fail:
it records a **pending** approval request and raises `approval_required` without
touching the instance. Each pending request is an **audit-safe summary** — IDs,
operation, reason, and risk score only. It never carries `WriteRequest.fields`
values or any secret.

An operator reviews and resolves it over MCP:

- `list_pending_approvals` returns the pending summaries (read-only).
- `approve_request(approval_id, approver)` approves it. A subsequent **identical**
  `guarded_write` (same agent, table, operation, records, reason, and field
  values) then applies — approval bypasses **only** the approval gate.
- `reject_request(approval_id, approver)` keeps the write blocked.

Every approve and reject appends an audit entry naming the approver. An approval
is bound to the exact request via a secret-free SHA-256 fingerprint, so a changed
field value needs a fresh approval.

**Precedence is strict: kill switch > deny > approval.** An approved request never
overrides an engaged kill switch or a hard policy deny (for example a disallowed
`delete`); those still raise `kill_switch_engaged` / `policy_denied` and are never
turned into a pending approval.

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
