"""MCP server entry point (Lesson 6 artifact).

Exposes the gateway as MCP tools an AI agent can call:
  - preview_change, policy_check, guarded_write, audit_log, kill_switch

This is a minimal, runnable stub. The Spec session will flesh out the full tool
schemas and wiring. Run with:  python -m sentinelnow.server
"""

from __future__ import annotations

from .gateway import Gateway
from .mock_servicenow import MockServiceNow
from .policy import Policy


def build_gateway() -> Gateway:
    instance = MockServiceNow()
    # Seed a couple of synthetic incidents for demos/tests.
    instance.seed("incident", "INC0001", {"priority": "1", "state": "2"})
    instance.seed("incident", "INC0002", {"priority": "3", "state": "2"})
    return Gateway(instance, Policy())


def main() -> None:
    gateway = build_gateway()
    # TODO (Spec session): register MCP tools over stdio using the `mcp` package and
    # bind them to gateway.preview_change / policy_check / guarded_write / audit_log /
    # engage_kill_switch. For now, prove the gateway is importable and constructs.
    print("SentinelNow gateway initialized (mock mode).")
    print(f"Seeded incidents: {len(gateway.audit_log())} audit entries so far.")


if __name__ == "__main__":
    main()
