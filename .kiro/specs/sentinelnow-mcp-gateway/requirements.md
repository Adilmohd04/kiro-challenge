# Requirements Document

## Introduction

SentinelNow is an AI-agent safety gateway for ServiceNow. It sits between an AI agent
(Claude, Codex, Kiro, etc.) and a ServiceNow instance and makes every intended write
previewable, risk-scored, policy-gated, auditable, and reversible through a kill switch.

The gateway exposes five tools to an AI agent over the Model Context Protocol (MCP):
`preview_change`, `policy_check`, `guarded_write`, `audit_log`, and `kill_switch`
(engage/release). Pure decision logic (risk scoring and policy evaluation) is separated
from I/O so it can be property-tested, while the gateway orchestrates the mock (and,
later, real) ServiceNow instance and the audit log.

This document specifies the behavior the gateway must guarantee. It aligns with the
existing scaffold in `src/sentinelnow/` (`models.py`, `policy.py`, `gateway.py`,
`mock_servicenow.py`, `audit.py`, `server.py`) and allows that surface to be extended
(for example, adding an async real-instance REST client alongside the mock).

## Glossary

- **Gateway**: The SentinelNow component that mediates every write between an AI agent and a ServiceNow instance; exposes the MCP tools and orchestrates policy evaluation, instance I/O, and auditing.
- **Agent**: An external AI client that invokes the Gateway's MCP tools to inspect or change ServiceNow records.
- **ServiceNow_Instance**: The backing record store the Gateway reads from and writes to. Implemented by `MockServiceNow` today and by a real REST client (`ServiceNowClient`) when credentials are supplied.
- **Write_Request**: A validated model (`WriteRequest`) describing an intended change: `agent_id`, `table`, `operation`, `record_ids`, `fields`, and `reason`.
- **Operation**: The kind of change in a Write_Request; one of CREATE, UPDATE, DELETE, or CLOSE.
- **Batch_Size**: The number of records a Write_Request targets, computed as `max(len(record_ids), 1)`.
- **Policy**: Configurable guardrails (`max_batch_size`, `allow_delete`, `protected_priorities`, `kill_switch_engaged`) that drive policy evaluation.
- **Protected_Priority**: A record priority value listed in `Policy.protected_priorities` (default `["1"]`, i.e. P1) that requires human approval before a write proceeds.
- **Risk_Score**: An integer in the range 0 to 100 produced by the pure `risk_score` function describing how risky a Write_Request is.
- **Decision**: The result of policy evaluation (`Decision`): an Effect, a human-readable reason, and the Risk_Score.
- **Effect**: One of ALLOW, NEEDS_APPROVAL, or DENY, produced by the pure `evaluate_policy` function.
- **Kill_Switch**: A Policy flag (`kill_switch_engaged`) that, when engaged, blocks all writes regardless of any other rule.
- **Audit_Log**: The append-only record of attempted and executed actions, holding `AuditEntry` records.
- **Audit_Entry**: A single Audit_Log record capturing who (agent_id), what (table, operation, record_ids), why (reason), and result (effect, risk_score, applied) plus an audit_id and timestamp.
- **preview_change**: The read-only MCP tool that returns the before/after of a Write_Request without mutating the ServiceNow_Instance.
- **policy_check**: The MCP tool that returns a Decision for a Write_Request without mutating the ServiceNow_Instance.
- **guarded_write**: The only MCP tool that mutates the ServiceNow_Instance, and only after policy evaluation permits it.
- **audit_log**: The MCP tool that returns the current list of Audit_Entry records.
- **kill_switch**: The MCP tool that engages or releases the Kill_Switch.
- **Allow_Rule**: An explicit Policy setting that permits an operation that otherwise defaults to DENY (for example, `allow_delete` for DELETE operations).

## Requirements

### Requirement 1: Risk Scoring

**User Story:** As an operator, I want every intended write scored for risk, so that I can gauge how dangerous a change is before it is applied.

#### Acceptance Criteria

1. WHEN an Agent submits a valid Write_Request, THE Gateway SHALL return a Risk_Score that is an integer in the inclusive range 0 to 100.
2. THE Gateway SHALL compute the Risk_Score deterministically, such that two evaluations with an identical Write_Request and identical Policy always produce the same Risk_Score.
3. WHEN two Write_Requests are identical in every field except Operation, THE Gateway SHALL assign a Risk_Score to the request whose Operation ranks higher in the ordering CREATE < UPDATE < CLOSE < DELETE that is greater than or equal to the Risk_Score of the request with the lower-ranked Operation.
4. IF a Write_Request fails boundary validation, THEN THE Gateway SHALL reject it without returning a Risk_Score and SHALL return an error indicating which field is invalid, leaving the ServiceNow instance unchanged.

### Requirement 2: Batch Size Limit

**User Story:** As an operator, I want oversized batches blocked, so that an agent cannot change more records at once than policy permits.

#### Acceptance Criteria

1. WHEN the Batch_Size of a Write_Request is strictly greater than the configured `max_batch_size` (a positive integer of at least 1), THE Gateway SHALL deny the write by returning a Decision with a DENY effect.
2. WHEN the Gateway denies a write for exceeding `max_batch_size`, THE Gateway SHALL include both the numeric Batch_Size and the numeric configured limit in the Decision reason as distinct values.
3. WHEN the Batch_Size of a Write_Request is less than or equal to the configured `max_batch_size`, THE Gateway SHALL NOT deny the write on the basis of batch size.
4. IF the Gateway denies a write for exceeding `max_batch_size`, THEN THE Gateway SHALL leave the ServiceNow instance unchanged, applying no record modifications from that Write_Request.

### Requirement 3: Kill Switch

**User Story:** As an operator, I want a single switch that halts all writes, so that I can immediately stop an agent during an incident.

#### Acceptance Criteria

1. WHILE the Kill_Switch is engaged, THE Gateway SHALL deny every write action regardless of any other Policy rule and without mutating the ServiceNow instance.
2. WHEN the `kill_switch` tool is invoked to engage, THE Gateway SHALL set the Kill_Switch to engaged within 1 second and record an audit entry capturing the invoking identity, the engage action, and the result.
3. WHEN the `kill_switch` tool is invoked to release, THE Gateway SHALL set the Kill_Switch to released within 1 second and record an audit entry capturing the invoking identity, the release action, and the result.
4. WHILE the Kill_Switch is engaged, WHEN a `guarded_write` is attempted, THE Gateway SHALL raise `KillSwitchEngaged`, leave the target record unchanged, and return an error indication identifying the Kill_Switch as the cause.
5. IF the `kill_switch` tool is invoked with a state value other than engage or release, THEN THE Gateway SHALL reject the invocation, leave the current Kill_Switch state unchanged, and return an error indication identifying the invalid state value.

### Requirement 4: Protected Priority Approval

**User Story:** As an operator, I want changes to high-priority records to require approval, so that critical incidents are not altered automatically.

#### Acceptance Criteria

1. WHEN a Write_Request targets a record whose priority field value is listed in the configured Protected_Priority set, THE Gateway SHALL classify the Decision Effect as NEEDS_APPROVAL.
2. IF a Write_Request requires approval WHEN a `guarded_write` is attempted, THEN THE Gateway SHALL raise `ApprovalRequired` with a protected-priority reason, apply no create/update/close/delete, and leave every targeted record byte-for-byte identical to its pre-invocation state.
3. WHEN the Gateway classifies a Write_Request as NEEDS_APPROVAL, THE Gateway SHALL record exactly one Audit_Entry capturing agent_id, table, Operation, record_ids, reason, the decision reason, and the applied flag set to false.
4. WHERE the configured Protected_Priority set is empty, THE Gateway SHALL NOT classify any Write_Request as NEEDS_APPROVAL on the basis of protected priority.

### Requirement 5: Audit Every Attempt

**User Story:** As an auditor, I want every write attempt recorded, so that I can review who did what, why, and with what result.

#### Acceptance Criteria

1. WHEN any write is attempted via guarded_write, THE Gateway SHALL record exactly one Audit_Entry capturing the agent_id (who); the table, Operation, and record_ids (what); the reason (why); and the Effect, risk_score, and applied flag (result).
2. WHEN the Gateway records an Audit_Entry, THE Gateway SHALL assign the Audit_Entry a unique audit_id and a UTC timestamp recorded to at least second precision.
3. IF a write is denied or requires approval, THEN THE Gateway SHALL record an Audit_Entry with the applied flag set to false before signaling the denial or approval requirement to the caller.
4. WHEN a write is applied, THE Gateway SHALL record an Audit_Entry with the applied flag set to true only after the underlying change succeeds, and SHALL return that Audit_Entry's audit_id in the WriteResult.
5. THE Gateway SHALL maintain the Audit_Entry collection as append-only, such that no previously recorded Audit_Entry is modified or removed for the lifetime of the Gateway.
6. WHEN the Gateway records an Audit_Entry, THE Gateway SHALL exclude credential values and full record field contents, capturing only record_ids and the fields enumerated in criterion 1.

### Requirement 6: Preview Is Read-Only

**User Story:** As an agent developer, I want to preview a change safely, so that I can inspect its effect without risking a mutation.

#### Acceptance Criteria

1. WHEN `preview_change` is invoked for a valid Write_Request, THE Gateway SHALL return, for each targeted record, the record id, the current before state, and the projected after state.
2. WHEN `preview_change` is invoked, THE Gateway SHALL perform no create, update, or delete against the ServiceNow_Instance.
3. WHEN `policy_check` is invoked, THE Gateway SHALL return a Decision whose Effect is one of ALLOW, NEEDS_APPROVAL, or DENY and SHALL leave the ServiceNow_Instance wholly unchanged, such that every record and all instance state are identical before and after the invocation.
4. IF `preview_change` or `policy_check` is invoked with a Write_Request that fails boundary validation, THEN THE Gateway SHALL return no preview or Decision, return an error indicating which field is invalid, and leave the ServiceNow_Instance unchanged.

### Requirement 7: Policy Check Precedes Guarded Write

**User Story:** As an operator, I want every mutation gated by policy evaluation, so that no write bypasses the guardrails.

#### Acceptance Criteria

1. WHEN `guarded_write` is invoked, THE Gateway SHALL evaluate the Policy for the Write_Request before mutating the ServiceNow_Instance.
2. WHILE the Kill_Switch is engaged, WHEN `guarded_write` is invoked, THE Gateway SHALL raise `KillSwitchEngaged` and leave every targeted record unchanged, taking precedence over any other Decision Effect.
3. IF the Decision Effect is DENY and the Kill_Switch is not engaged, THEN THE Gateway SHALL raise `PolicyDenied` and leave every targeted record unchanged, such that reading each targeted record returns the same field values as before the invocation.
4. IF the Decision Effect is NEEDS_APPROVAL, THEN THE Gateway SHALL raise `ApprovalRequired` and leave every targeted record unchanged, such that reading each targeted record returns the same field values as before the invocation.
5. WHERE the Decision Effect is ALLOW and the Kill_Switch is not engaged, THE Gateway SHALL apply the Write_Request to the ServiceNow_Instance and return a WriteResult with the applied flag set to true and a non-empty audit_id.
6. WHEN `guarded_write` completes with any outcome, THE Gateway SHALL record exactly one Audit_Entry describing the agent_id, table, Operation, record_ids, reason, Effect, and applied flag, excluding credential values and record field values.

### Requirement 8: Safe-by-Default for Destructive and Bulk Operations

**User Story:** As an operator, I want destructive and bulk operations denied unless explicitly allowed, so that the safe default is to refuse dangerous changes.

#### Acceptance Criteria

1. IF a Write_Request has the DELETE Operation AND the Policy `allow_delete` Allow_Rule is disabled, THEN THE Gateway SHALL deny the write and return a Decision with a DENY Effect and a reason indicating that deletes are not allowed.
2. THE Gateway SHALL treat the Policy `allow_delete` Allow_Rule as disabled by default, denying every DELETE Write_Request until the rule is explicitly enabled.
3. WHEN a Write_Request has the DELETE Operation AND the Policy `allow_delete` Allow_Rule is enabled, THE Gateway SHALL proceed to normal policy evaluation (batch-size, protected-priority, and risk checks) rather than applying the write unconditionally.
4. IF a Write_Request has a Batch_Size that exceeds the Policy `max_batch_size` limit (default 10, where Batch_Size is the greater of the number of target record IDs or 1), THEN THE Gateway SHALL deny the write regardless of Operation and return a Decision with a DENY Effect and a reason indicating the batch size exceeded the limit.
5. IF the Gateway denies a Write_Request under any of the above rules, THEN THE Gateway SHALL make no change to the ServiceNow instance for that request.

### Requirement 9: Audit Entries Exclude Secrets and Record Contents

**User Story:** As a security reviewer, I want audit entries free of secrets and record contents, so that the audit trail cannot leak sensitive data.

#### Acceptance Criteria

1. WHEN the Gateway records an Audit_Entry, THE Audit_Entry SHALL contain exactly the fields audit_id, timestamp (UTC ISO-8601), agent_id, table, operation, record_ids, reason, effect, risk_score (integer 0 to 100), and applied, and no additional keys.
2. WHEN the Gateway records an Audit_Entry, THE Gateway SHALL exclude every key and value of the Write_Request `fields` mapping and any credential or secret value from the serialized Audit_Entry.
3. WHEN the Gateway records an Audit_Entry, THE Gateway SHALL store the reason text as provided without copying Write_Request `fields` values or credential values into the reason.

### Requirement 10: Credentials From Environment Only

**User Story:** As a security reviewer, I want credentials sourced only from the environment, so that secrets are never hard-coded or logged.

#### Acceptance Criteria

1. WHEN the Gateway connects to a ServiceNow_Instance, THE Gateway SHALL read instance credentials only from environment variables (for example `SN_INSTANCE`, `SN_USER`, `SN_PASSWORD`) or a gitignored `.env` file, and SHALL NOT read credentials from any other source including command-line arguments, source code literals, or configuration files that are not gitignored.
2. IF a required credential environment variable (`SN_INSTANCE`, `SN_USER`, or `SN_PASSWORD`) is absent or resolves to an empty string, THEN THE Gateway SHALL abort the connection attempt, produce an error indicating which named credential variable is missing without including any credential value, and SHALL NOT attempt the connection using a default or fallback credential.
3. WHEN the Gateway logs an action that involved a credential, THE Gateway SHALL reference the credential by its environment variable name and SHALL exclude the credential value from the log entry, including partial values, masked substrings, and derived representations such as hashes or encodings of the value.

### Requirement 11: Real ServiceNow Client Alongside the Mock

**User Story:** As a developer, I want a real ServiceNow REST client interchangeable with the mock, so that the gateway can run against a live instance without changing policy logic.

#### Acceptance Criteria

1. THE Gateway SHALL operate against any ServiceNow_Instance that exposes a preview operation and an apply operation, whether backed by `MockServiceNow` or a real ServiceNow REST client, without any change to policy evaluation code.
2. WHERE a real ServiceNow REST client is configured, THE Gateway SHALL perform all instance read and write operations through asynchronous non-blocking I/O functions, applying a per-request connection timeout between 1 and 60 seconds (default 30 seconds).
3. WHEN a write request is processed through the real ServiceNow REST client, THE Gateway SHALL apply the identical policy evaluation, audit logging, and Kill_Switch gating that it applies when the request is processed through `MockServiceNow`, such that for identical inputs the resulting Decision Effect, Risk_Score, and Audit_Entry are the same.
4. IF the real ServiceNow REST client cannot establish a connection to the instance within the configured timeout, THEN THE Gateway SHALL abort the operation without applying any change, leave the instance unmodified, and return an error indicating that the instance is unreachable.
5. IF a read or write operation through the real ServiceNow REST client fails after a connection is established, THEN THE Gateway SHALL abort the operation without applying any partial change, preserve the pre-operation state, and return an error indicating that the operation failed.
