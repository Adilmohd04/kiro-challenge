# Implementation Plan: SentinelNow MCP Gateway

## Overview

This plan finishes the SentinelNow MCP gateway on top of the existing scaffold in
`src/sentinelnow/`. It is ordered so that pure, side-effect-free decision logic and its
property-based tests come first, then the async I/O boundary (instance Protocol, mock,
gateway, real REST client), and finally the MCP server wiring.

The work produces the three artifacts the design emphasizes: the comprehensive
specification (already complete in `requirements.md` / `design.md`), a working
implementation that conforms to it, and a property-based test suite that proves
Correctness Properties 1–12. Property tests are written alongside or before the code they
verify, and each test references the design property and requirement clause it checks.

Convention reminders enforced throughout (per steering): policy logic stays pure and
synchronous; all instance I/O is `async def`; external input is validated with pydantic at
the boundary; typed exceptions come from `src/sentinelnow/errors.py`; audit entries and
logs never contain secret values or `WriteRequest.fields` contents.

## Tasks

- [x] 1. Dependencies, tooling, and typed errors (foundation)
  - [x] 1.1 Pin dependencies in `pyproject.toml`
    - Add `mcp` (MCP server SDK) to `[project].dependencies`, pinned to an exact released version (e.g. `mcp==1.x.y`).
    - Confirm `httpx==0.27.2` is pinned in `[project].dependencies` (already present).
    - Confirm `[project.optional-dependencies].dev` pins exact `pytest==8.3.3`, `hypothesis==6.112.1`, `ruff==0.6.9`, and verify `[tool.ruff]`, `[tool.pytest.ini_options]` (`testpaths`, `pythonpath=["src"]`) remain correct.
    - _Requirements: 11.2; Design: Testing Strategy > Tooling_

  - [x] 1.2 Extend `src/sentinelnow/errors.py` with new typed exceptions
    - Add `InvalidRequest`, `MissingCredential`, `InstanceUnreachable`, and `InstanceOperationFailed`, each subclassing `SentinelNowError`.
    - Keep existing `PolicyDenied`, `ApprovalRequired`, `KillSwitchEngaged` unchanged.
    - _Requirements: 1.4, 6.4, 3.5, 10.2, 11.4, 11.5; Design: Error Handling_

- [x] 2. Policy core property tests (pure logic, Properties 1–7)
  - [x] 2.1 Extend hypothesis strategies in `tests/test_policy_properties.py`
    - Extend the `write_requests` strategy so `fields["priority"]` can draw protected (`"1"`) and non-protected values, and `record_ids` spans 0–25 to cross the default batch limit.
    - Add a new `policies` strategy via `st.builds(Policy, max_batch_size=st.integers(1, 20), allow_delete=st.booleans(), protected_priorities=st.lists(st.sampled_from(["1","2","3"]), max_size=3), kill_switch_engaged=st.booleans())`.
    - _Requirements: 1.1, 2.1, 4.1, 8.1; Design: Correctness Properties > Generators / strategies_

  - [x]* 2.2 Write property test for Property 1 (risk score bounded)
    - **Property 1: Risk score is bounded** — `risk_score(req, policy)` returns an int in [0, 100].
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 1`.
    - **Validates: Requirements 1.1**

  - [x]* 2.3 Write property test for Property 2 (deterministic)
    - **Property 2: Risk score is deterministic** — two evaluations on identical inputs are equal.
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 2`.
    - **Validates: Requirements 1.2**

  - [x]* 2.4 Write property test for Property 3 (monotonic in operation severity)
    - **Property 3: Risk score is monotonic** — build operation variants with `model_copy(update={"operation": op})`; score is non-decreasing over CREATE ≤ UPDATE ≤ CLOSE ≤ DELETE.
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 3`.
    - **Validates: Requirements 1.3**

  - [x]* 2.5 Write property test for Property 4 (kill switch denies all)
    - **Property 4: Kill switch denies everything** — `evaluate_policy(req, Policy(kill_switch_engaged=True))` is always DENY. Strengthen the existing `test_kill_switch_denies_everything`.
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 4`.
    - **Validates: Requirements 3.1**

  - [x]* 2.6 Write property test for Property 5 (batch gating)
    - **Property 5: Batch size gates on the configured limit** — over-limit ⇒ DENY; within-limit ⇒ never denied on the basis of batch size (kill switch off). Strengthen the existing `test_bulk_over_limit_is_never_allowed` to assert DENY.
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 5`.
    - **Validates: Requirements 2.1, 2.3, 8.4**

  - [x]* 2.7 Write property test for Property 6 (delete-deny by default)
    - **Property 6: Deletes are denied unless explicitly allowed** — DELETE with `allow_delete=False` ⇒ DENY; `allow_delete=True` does not bypass the batch limit.
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 6`.
    - **Validates: Requirements 8.1, 8.2, 8.3**

  - [x]* 2.8 Write property test for Property 7 (protected-priority approval)
    - **Property 7: Protected priority requires approval exactly when configured** — isolate precedence (kill switch off, delete allowed/absent, within batch); matched protected priority ⇒ NEEDS_APPROVAL; empty set ⇒ never NEEDS_APPROVAL on that basis.
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 7`.
    - **Validates: Requirements 4.1, 4.4**

  - [x] 2.9 Reconcile `src/sentinelnow/policy.py` with the properties
    - Run the Property 1–7 tests; adjust `policy.py` ONLY if a property reveals a gap (keep functions pure and synchronous, no I/O). Document any change in a code comment.
    - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.3, 4.1, 4.4, 8.1, 8.2, 8.3, 8.4_

- [ ] 3. Checkpoint - pure policy layer
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Async instance Protocol and async mock
  - [x] 4.1 Introduce the async `ServiceNowInstance` Protocol
    - Add a `ServiceNowInstance` `Protocol` with `async def preview(self, req: WriteRequest) -> list[dict[str, str]]` and `async def apply(self, req: WriteRequest) -> int` (in `src/sentinelnow/mock_servicenow.py` or a new `src/sentinelnow/instance.py`).
    - _Requirements: 11.1; Design: Components and Interfaces > 1_

  - [x] 4.2 Make `MockServiceNow.preview` / `apply` async
    - Convert `preview` and `apply` to `async def` (bodies remain in-memory, no `await` needed); keep `seed`/`get` synchronous test helpers. Confirm it structurally satisfies `ServiceNowInstance`.
    - _Requirements: 11.1_

- [x] 5. Async gateway refactor
  - [x] 5.1 Make gateway I/O methods async and type to the Protocol
    - In `src/sentinelnow/gateway.py`, change `__init__` to accept `ServiceNowInstance`; make `preview_change` and `guarded_write` `async def` and `await` the instance; keep `policy_check` pure/synchronous.
    - Preserve precedence in `guarded_write`: kill switch ⇒ `KillSwitchEngaged` first, then DENY ⇒ `PolicyDenied`, then NEEDS_APPROVAL ⇒ `ApprovalRequired`, then ALLOW ⇒ `await apply` + audit `applied=True`; record exactly one audit entry (`applied=False` before raising on non-ALLOW).
    - _Requirements: 3.4, 5.1, 5.3, 6.1, 6.2, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 8.5_

  - [x] 5.2 Add actor + audit to kill-switch methods
    - Change `engage_kill_switch(actor: str) -> AuditEntry` and `release_kill_switch(actor: str) -> AuditEntry` to set state and record one `AuditEntry` capturing the actor, action, and result; return the entry.
    - _Requirements: 3.2, 3.3_

- [x] 6. Gateway and audit property tests (Properties 8–12)
  - [x] 6.1 Create `tests/test_gateway_properties.py` scaffolding
    - Add helpers to seed a fresh `MockServiceNow` per example and snapshot `_tables` (deep copy) for before/after comparison; add a driver to run async `guarded_write` via `asyncio.run` (or anyio); add an in-memory `StubServiceNowClient` implementing the async `ServiceNowInstance` Protocol for parity.
    - _Requirements: 11.1, 11.3; Design: Correctness Properties > Generators / strategies_

  - [x]* 6.2 Write property test for Property 8 (read-only tools never mutate)
    - **Property 8** — `preview_change` and `policy_check` leave instance state identical; `policy_check` returns an effect in {ALLOW, NEEDS_APPROVAL, DENY}.
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 8`.
    - **Validates: Requirements 6.1, 6.2, 6.3**

  - [x]* 6.3 Write property test for Property 9 (non-ALLOW never mutates; typed exceptions)
    - **Property 9** — non-ALLOW leaves state unchanged and raises the matching typed exception (`KillSwitchEngaged` taking precedence, `PolicyDenied`, `ApprovalRequired`); ALLOW mutates and returns `applied=True` with non-empty `audit_id`.
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 9`.
    - **Validates: Requirements 3.4, 7.2, 7.3, 7.4, 7.5, 8.5**

  - [x]* 6.4 Write property test for Property 10 (exactly one audit entry; applied flag matches)
    - **Property 10** — one `AuditEntry` per `guarded_write` (return or raise); `applied` true iff instance changed; `audit_id` values pairwise unique; log is append-only.
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 10`.
    - **Validates: Requirements 4.3, 5.1, 5.2, 5.3, 5.4, 5.5, 7.6**

  - [x]* 6.5 Write property test for Property 11 (audit excludes secrets and record contents)
    - **Property 11** — the `AuditEntry` serializes to exactly `audit_id, timestamp, agent_id, table, operation, record_ids, reason, effect, risk_score, applied` and contains none of `req.fields` keys/values.
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 11`.
    - **Validates: Requirements 5.6, 9.1, 9.2**

  - [x]* 6.6 Write property test for Property 12 (backend-independent decision)
    - **Property 12** — decision effect, `risk_score`, and resulting `AuditEntry` effect/risk are identical whether backed by `MockServiceNow` or `StubServiceNowClient`.
    - Tag: `# Feature: sentinelnow-mcp-gateway, Property 12`.
    - **Validates: Requirements 11.3**

- [ ] 7. Checkpoint - gateway and audit behavior
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Credential configuration (env-only)
  - [x] 8.1 Add `ServiceNowConfig` pydantic model
    - In a new `src/sentinelnow/config.py` (or `instance.py`), add `ServiceNowConfig` with `instance_url` (min_length 1), `user` (min_length 1), `password: SecretStr`, `timeout_seconds: float = Field(default=30.0, ge=1.0, le=60.0)`, and a `from_env()` classmethod reading `SN_INSTANCE`/`SN_USER`/`SN_PASSWORD`; missing/empty ⇒ raise `MissingCredential` naming the variable only (no value).
    - _Requirements: 10.1, 10.2, 10.3, 11.2; Design: Data Models > Credential config_

  - [x]* 8.2 Write unit tests for credential loading
    - In `tests/test_config.py`: env present ⇒ loads; each missing/empty var ⇒ `MissingCredential` naming the variable; assert `repr(config)` and `str(config.password)` never expose the secret value; assert timeout bounds (default 30, reject <1 and >60).
    - _Requirements: 10.1, 10.2, 10.3, 11.2_

- [x] 9. Real ServiceNow REST client
  - [x] 9.1 Implement async `ServiceNowClient` (httpx)
    - In `src/sentinelnow/instance.py`, implement `ServiceNowClient` satisfying `ServiceNowInstance`: construct from `ServiceNowConfig`; `preview` does read-only GET(s) to build before/after; `apply` does POST/PATCH/DELETE on the Table API; enforce per-request `httpx.Timeout` from `timeout_seconds`; raise `InstanceUnreachable` on connect failure and `InstanceOperationFailed` on post-connect failure (no partial change). Reference credentials by env var name only in any log line.
    - _Requirements: 11.2, 11.4, 11.5; Design: Components and Interfaces > 1, Security Considerations_

  - [x]* 9.2 Write integration tests with `httpx.MockTransport`
    - In `tests/test_servicenow_client.py` (example/integration tests, NOT property tests): 1–3 cases covering a successful preview via GET, connection failure ⇒ `InstanceUnreachable` with no mutation, and post-connect failure ⇒ `InstanceOperationFailed` with pre-operation state preserved.
    - _Requirements: 11.2, 11.4, 11.5_

- [x] 10. MCP tool models and server wiring
  - [x] 10.1 Add MCP request/response pydantic models
    - In `src/sentinelnow/models.py` (or a new `src/sentinelnow/tool_models.py`): `RecordChange(sys_id, before, after)`, `PreviewChangeRequest/Response`, `PolicyCheckRequest/Response`, `GuardedWriteRequest/Response`, `AuditLogRequest/Response`, `KillSwitchAction(str, Enum){ENGAGE, RELEASE}`, `KillSwitchRequest(action, actor: Field(min_length=1))`, `KillSwitchResponse(engaged, audit_id)`.
    - _Requirements: 3.5, 5.1, 6.1, 7.5; Design: Data Models > New models_

  - [x] 10.2 Register the five MCP tools in `src/sentinelnow/server.py`
    - Using the `mcp` package over stdio, register `preview_change`, `policy_check`, `guarded_write`, `audit_log`, and `kill_switch`, each binding to the matching gateway method (await the async ones), validating input with the tool models and returning the response models.
    - Replace the stub `main()` TODO; keep `build_gateway()` seeding.
    - _Requirements: 3.2, 3.3, 5.1, 6.1, 6.3, 7.1; Design: Components and Interfaces > 4_

  - [x] 10.3 Map typed exceptions to structured MCP tool error responses
    - Map `KillSwitchEngaged`, `PolicyDenied`, `ApprovalRequired`, `InvalidRequest`/`ValidationError`, `MissingCredential`, `InstanceUnreachable`, `InstanceOperationFailed` to structured tool error responses per the Error Handling table; validation errors name the invalid field and credential errors name the variable only (never a value).
    - _Requirements: 1.4, 3.4, 3.5, 4.2, 6.4, 7.3, 7.4, 10.2, 11.4, 11.5; Design: Error Handling_

- [x] 11. Boundary, example, and integration tests (wiring)
  - [ ]* 11.1 Write boundary validation unit tests
    - In `tests/test_boundary.py`: invalid `WriteRequest` (e.g. empty `agent_id`) and invalid `kill_switch` action ⇒ validation error naming the offending field, with no I/O performed (instance unchanged).
    - _Requirements: 1.4, 3.5, 6.4_

  - [ ]* 11.2 Write example tests for batch DENY reason and reason pass-through
    - Batch over-limit DENY reason contains both the numeric batch size and the numeric limit as distinct values (Req 2.2); `AuditEntry.reason == req.reason` (Req 9.3).
    - _Requirements: 2.2, 9.3_

  - [ ]* 11.3 Write example tests for kill-switch engage/release audit
    - Engage then release each produce exactly one `AuditEntry` capturing the actor and action; `kill_switch` state flips accordingly.
    - _Requirements: 3.2, 3.3_

  - [x] 11.4 Verify build, lint, and full test suite
    - Run `ruff check` / `ruff format --check` on `src/sentinelnow` and `tests`; run `python -m pytest`; fix any failures so the gateway is working and verifiable end to end.
    - _Requirements: 11.3; Design: Testing Strategy > Tooling_

- [ ] 12. Final checkpoint - working, verifiable gateway
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; they are the
  property, unit, and integration test sub-tasks. Core implementation sub-tasks are never
  marked optional.
- Property tests are placed next to the code they verify so correctness gaps surface
  early (Properties 1–7 against the pure policy; 8–12 against the async gateway).
- Each property sub-task references its design property number and the requirement
  clause it validates, for traceability.
- Checkpoints provide incremental validation points at the end of the pure layer, the
  gateway layer, and the full wiring.
- All instance I/O is async; policy logic stays pure and synchronous; credentials come
  only from env vars and are never logged or audited by value.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1", "4.1", "8.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "2.8", "4.2", "8.2", "9.1"] },
    { "id": 3, "tasks": ["2.9", "5.1", "9.2"] },
    { "id": 4, "tasks": ["5.2", "6.1", "10.1"] },
    { "id": 5, "tasks": ["6.2", "6.3", "6.4", "6.5", "6.6", "10.2"] },
    { "id": 6, "tasks": ["10.3", "11.1", "11.2", "11.3"] },
    { "id": 7, "tasks": ["11.4"] }
  ]
}
```
