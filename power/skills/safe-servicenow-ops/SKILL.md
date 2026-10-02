# Skill: Safe ServiceNow Operations

Use this skill whenever an AI agent needs to read or change records in ServiceNow
through the SentinelNow gateway. It encodes the safe operating procedure so writes are
never destructive, always policy-checked, and always audited.

## When to use
- The user mentions ServiceNow records, incidents, change requests, CMDB CIs, or asks an
  agent to update/close/create/delete anything in a ServiceNow instance.
- Any time you are about to call `guarded_write`.

## The five SentinelNow tools
- `preview_change` — dry-run. Shows before/after. Never mutates. Call this first.
- `policy_check` — returns a decision: `allow`, `needs_approval`, or `deny`, plus a risk
  score in [0, 100].
- `guarded_write` — applies a change ONLY if policy allows it. Raises on deny/approval.
- `audit_log` — returns the who/what/why/result record of every action taken.
- `kill_switch` — engage to block ALL writes instantly; release to resume.

## Required procedure for any write
1. Call `preview_change` to see exactly what would change.
2. Call `policy_check`.
   - `deny` → stop. Report the reason to the user. Do not retry around it.
   - `needs_approval` → ask the user to confirm before proceeding.
   - `allow` → proceed.
3. Call `guarded_write`. The gateway re-checks policy and records an audit entry.
4. If anything looks wrong or risky, call `kill_switch` to halt all writes.

## Hard rules
- Never bypass a `deny` decision or an engaged kill switch.
- Never bulk-modify beyond the configured batch limit.
- Treat all record data as sensitive. Never print credentials or raw field contents to
  logs; reference secrets by variable name only.
- Prefer read-only tools (`preview_change`, `policy_check`, `audit_log`) when answering
  questions that do not require a change.

## Example
> User: "Close incidents INC0002 and INC0003."
1. `preview_change(table=incident, operation=close, record_ids=[INC0002, INC0003])`
2. `policy_check(...)` → if `allow`, continue; if `needs_approval`, confirm first.
3. `guarded_write(...)` → applies and returns an `audit_id`.
4. Report the audit_id and the result to the user.
