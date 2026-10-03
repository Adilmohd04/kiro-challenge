"""The async backend interface the gateway talks to.

The gateway and the entire policy layer depend only on this narrow structural
interface, never on a concrete backend. Both the in-memory ``MockServiceNow``
(async-adapted) and a future httpx-based ``ServiceNowClient`` satisfy it, so the
gateway runs unchanged against either backend (Requirement 11.1).

Both methods are ``async`` because the real client is network-bound; the mock
adapts its in-memory body to the same signature.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import WriteRequest


@runtime_checkable
class ServiceNowInstance(Protocol):
    """Structural interface for a ServiceNow backend (mock or real client)."""

    async def get_record(self, table: str, sys_id: str) -> dict[str, str] | None:
        """Read a single record's current fields. Read-only; MUST NOT mutate.

        Returns the record's field dict, or ``None`` when the record is absent.
        The gateway uses this to learn the record's CURRENT state (e.g. its
        current priority) so policy can gate on it.
        """
        ...

    async def preview(self, req: WriteRequest) -> list[dict[str, str]]:
        """Return the before/after of what WOULD change. MUST NOT mutate. (Req 6.2)"""
        ...

    async def apply(self, req: WriteRequest) -> int:
        """Apply the change for real. Returns the number of affected records."""
        ...
