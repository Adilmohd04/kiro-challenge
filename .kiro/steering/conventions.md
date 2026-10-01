# SentinelNow — Project Conventions

SentinelNow is an AI-agent safety gateway for ServiceNow. It sits between an AI agent
(Claude, Codex, Kiro, etc.) and a ServiceNow instance, and makes every write action
previewable, risk-scored, policy-gated, auditable, and reversible via a kill switch.

These conventions apply to all code Kiro generates in this project.

## Language & tooling
- Python 3.11+.
- Use `ruff` for linting/formatting.
- Use `pytest` for tests and `hypothesis` for property-based tests.
- Manage dependencies in `pyproject.toml`. Pin exact versions.

## Code style
- Prefer `async def` for all I/O-bound functions (ServiceNow REST calls). Never block the event loop.
- Full type hints on every function signature. No bare `Any` unless unavoidable.
- Validate all external input with `pydantic` models at the boundary.
- Small, single-responsibility functions. Pure functions for policy/risk logic so they are easy to property-test.
- Raise typed exceptions from `sentinelnow/errors.py`; never raise bare `Exception`.

Example of the expected style:

```python
async def guarded_write(req: WriteRequest, policy: Policy) -> WriteResult:
    decision = evaluate_policy(req, policy)  # pure, testable
    if decision.effect is Effect.DENY:
        raise PolicyDenied(decision.reason)
    ...
```

This ensures policy logic stays pure and testable, and I/O stays async.
