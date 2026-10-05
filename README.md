# SentinelNow

**An MCP server that puts a safety gateway in front of ServiceNow for any AI agent.**

ServiceNow increasingly lets AI agents act on an instance over the Model Context
Protocol (MCP). The open problem is **governance of what those agents actually do** —
an over-permissioned agent can bulk-close incidents, corrupt the CMDB, or delete
records with nobody watching. SentinelNow sits between the agent and ServiceNow and
makes every write **previewable, risk-scored, policy-gated, auditable, and reversible
via a kill switch.**

Because it speaks MCP over stdio, SentinelNow works with **any MCP client** — Claude
(Desktop/Code), ChatGPT / OpenAI, GitHub Copilot, Gemini, Cursor, and Kiro — without
any client-specific code. Point your agent at the SentinelNow server instead of
directly at ServiceNow, and every write it attempts flows through the guardrails.

## How it fits
SentinelNow is a **per-action guardrail in front of an external AI agent**, not a
replacement for ServiceNow's own controls. It is complementary to:

- **ServiceNow ACLs** — row/field permissions decide what a credential *can* touch.
  SentinelNow decides whether a specific *agent action* should proceed right now,
  previews it, risk-scores it, can require human approval, and records it.
- **ServiceNow AI Control Tower** — platform-side oversight of AI. SentinelNow is the
  thin, portable gate the agent itself calls, usable from any MCP client and in mock
  mode with no instance at all.

In short: ACLs gate the credential, SentinelNow gates the *action*.

## Tools (exposed over MCP)
SentinelNow registers nine MCP tools. Only `guarded_write` and `kill_switch` ever
change anything; everything else is read-only.

| Tool | Mutates? | Purpose |
|------|----------|---------|
| `preview_change` | no | Dry-run; show the before/after of an intended write. |
| `search_records` | no | Read-only lookup; find records by encoded query. |
| `policy_check` | no | Risk-score an intended action -> allow / needs_approval / deny. |
| `guarded_write` | **yes** | Apply a change only if it passes policy. The one mutating write tool. |
| `audit_log` | no | Who / what / why / result for every action taken. |
| `kill_switch` | state | Instantly block (or release) all writes; always audited. |
| `list_pending_approvals` | no | List writes awaiting human approval (audit-safe summaries). |
| `approve_request` | state | Approve a pending write (audited; names the approver). |
| `reject_request` | state | Reject a pending write (audited; names the approver). |

## Status
Runs **mock-first** against an in-memory ServiceNow, so it works with no live instance.
Point it at a free Personal Developer Instance (PDI) later via the `SN_*` environment
variables and `SENTINELNOW_MODE=live`.

## Configuration (environment)
Credentials are read **only** from the environment. For local use, copy the template
and fill in real values:

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

Every env var above is honored by `build_gateway()` at startup; absent the two
`SENTINELNOW_*` store/config vars, the gateway behaves exactly as the in-memory default.

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

### Audit persistence (`SENTINELNOW_AUDIT_DB`)
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

## Audit dashboard (read-only)
A small FastAPI app renders a live, **read-only** view of what agents have done:
a timeline of actions with their effect and risk score, the current kill-switch
status, and the list of writes awaiting approval.

```bash
python -m sentinelnow.dashboard   # serves on http://127.0.0.1:8787
```

It builds a gateway via the same `build_gateway()` as the MCP server, so it reflects
the same policy and audit store (set `SENTINELNOW_AUDIT_DB` to view a persistent
trail). The dashboard **never writes** — approving, rejecting, and the kill switch
remain MCP-only; the dashboard only *displays* pending approvals. It exposes:

- `GET /api/audit` — the audit entries (closed key set, no secrets).
- `GET /api/pending` — pending approval summaries (display only).
- `GET /api/killswitch` — `{"engaged": bool}`.
- `GET /` — a single inline HTML page (no build step) that polls the three endpoints.

## Quickstart
```bash
pip install -e ".[dev]"
python -m pytest                 # run tests (incl. property-based)
python -m sentinelnow.server     # start the MCP server (mock mode)
python -m sentinelnow.dashboard  # start the read-only audit dashboard
```

Register the server with your MCP client (for example in Claude Desktop's
`claude_desktop_config.json`, or any client's MCP settings) by running
`python -m sentinelnow.server` over stdio.

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
