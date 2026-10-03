"""MCP server entry point.

Exposes the SentinelNow gateway as five MCP tools an AI agent can call over stdio:
  - preview_change  (read-only)
  - policy_check    (read-only; reads current record state to evaluate policy)
  - guarded_write   (the only mutating tool)
  - audit_log       (read-only)
  - kill_switch     (state change, audited)

Run with:  python -m sentinelnow.server

Each tool validates its input via the pydantic tool models in .tool_models and
returns a typed response model; FastMCP derives the input/output schema from the
type hints. Pure decision logic lives in policy.py; the gateway orchestrates I/O.

Error mapping: the typed SentinelNow exceptions (errors.py) raised inside the
handlers are translated into structured FastMCP ``ToolError`` responses with a
stable category prefix (see ``_EXCEPTION_PREFIX`` / ``_to_tool_error``), so the
calling AI agent receives a clear, categorized failure instead of a raw
traceback. Boundary (input-shape) validation is handled by FastMCP itself: it
converts a pydantic ``ValidationError`` on tool inputs into a tool error naming
the invalid field BEFORE the handler body runs, so no gateway/instance I/O
occurs for malformed input (Req 1.4, 6.4, 3.5) — we do not re-catch it here.
"""

from __future__ import annotations

from typing import Awaitable, Callable, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .errors import (
    ApprovalRequired,
    InstanceOperationFailed,
    InstanceUnreachable,
    InvalidRequest,
    KillSwitchEngaged,
    MissingCredential,
    PolicyDenied,
    SentinelNowError,
)
from .gateway import Gateway
from .mock_servicenow import MockServiceNow
from .policy import Policy
from .tool_models import (
    AuditLogResponse,
    GuardedWriteResponse,
    KillSwitchAction,
    KillSwitchResponse,
    PolicyCheckResponse,
    PreviewChangeResponse,
    RecordChange,
)
from .models import WriteRequest


def build_gateway() -> Gateway:
    instance = MockServiceNow()
    # Seed a couple of synthetic incidents for demos/tests.
    instance.seed("incident", "INC0001", {"priority": "1", "state": "2"})
    instance.seed("incident", "INC0002", {"priority": "3", "state": "2"})
    return Gateway(instance, Policy())


# Module-level MCP server and a single shared gateway instance.
mcp = FastMCP("sentinelnow")
gateway = build_gateway()


# --- typed exception -> structured MCP tool error mapping (Design: Error Handling) ---
#
# Each SentinelNow domain exception maps to a stable, machine-readable category
# prefix. The exception's own message is already built from safe sources
# (decision.reason, or a credential VAR name) — never from secret values or raw
# WriteRequest field contents — so we pass it through unchanged behind the prefix.
_EXCEPTION_PREFIX: dict[type[SentinelNowError], str] = {
    KillSwitchEngaged: "kill_switch_engaged",  # Req 3.4
    PolicyDenied: "policy_denied",  # Req 7.3
    ApprovalRequired: "approval_required",  # Req 4.2, 7.4
    InvalidRequest: "invalid_request",  # Req 1.4, 6.4, 3.5
    MissingCredential: "missing_credential",  # Req 10.2 (names the VAR, not the value)
    InstanceUnreachable: "instance_unreachable",  # Req 11.4
    InstanceOperationFailed: "instance_operation_failed",  # Req 11.5
}

_T = TypeVar("_T")


def _to_tool_error(exc: SentinelNowError) -> ToolError:
    """Translate a typed SentinelNow exception into a categorized FastMCP ToolError.

    The category prefix is resolved by walking the exception's MRO so subclasses
    of a mapped type still categorize correctly. The exception message is
    secret-free by construction and is forwarded verbatim after the prefix.
    """
    prefix = next(
        (p for cls, p in _EXCEPTION_PREFIX.items() if isinstance(exc, cls)),
        "error",
    )
    return ToolError(f"{prefix}: {exc}")


async def _guard(call: Callable[[], Awaitable[_T]]) -> _T:
    """Run an async tool body, converting SentinelNow errors into ToolError.

    Only the project's typed exceptions are caught (never a bare ``Exception``),
    in keeping with the error-handling convention. Pydantic input validation is
    handled by FastMCP before the handler runs, so it is intentionally not caught
    here.
    """
    try:
        return await call()
    except SentinelNowError as exc:
        raise _to_tool_error(exc) from exc


@mcp.tool()
async def preview_change(request: WriteRequest) -> PreviewChangeResponse:
    """Preview the before/after of an intended write. Read-only; never mutates.

    Once a real ServiceNow client backs the gateway, this can raise
    ``InstanceUnreachable`` / ``InstanceOperationFailed``; both are mapped to a
    structured ToolError (no mutation occurs on either).
    """

    async def _run() -> PreviewChangeResponse:
        result = await gateway.preview_change(request)
        return PreviewChangeResponse(changes=[RecordChange(**d) for d in result])

    return await _guard(_run)


@mcp.tool()
async def policy_check(request: WriteRequest) -> PolicyCheckResponse:
    """Evaluate policy for an intended write without applying it. Read-only."""
    decision = await gateway.policy_check(request)
    return PolicyCheckResponse(decision=decision)


@mcp.tool()
async def guarded_write(request: WriteRequest) -> GuardedWriteResponse:
    """Apply a write only if policy allows it. The only mutating tool.

    Maps the gateway's typed refusals to structured tool errors:
    ``KillSwitchEngaged`` -> ``kill_switch_engaged``, ``PolicyDenied`` ->
    ``policy_denied``, ``ApprovalRequired`` -> ``approval_required`` (and, with a
    real client, the instance errors). On every refusal the gateway has already
    recorded the audit entry (applied=False) before raising.
    """

    async def _run() -> GuardedWriteResponse:
        result = await gateway.guarded_write(request)
        return GuardedWriteResponse(result=result)

    return await _guard(_run)


@mcp.tool()
async def audit_log() -> AuditLogResponse:
    """Return the append-only audit log of every action taken."""
    return AuditLogResponse(entries=gateway.audit_log())


@mcp.tool()
async def kill_switch(action: KillSwitchAction, actor: str) -> KillSwitchResponse:
    """Engage or release the kill switch. State change only; always audited."""
    if action is KillSwitchAction.ENGAGE:
        entry = gateway.engage_kill_switch(actor)
        engaged = True
    else:
        entry = gateway.release_kill_switch(actor)
        engaged = False
    return KillSwitchResponse(engaged=engaged, audit_id=entry.audit_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
