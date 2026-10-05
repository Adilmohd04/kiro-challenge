"""The async backend interface the gateway talks to.

The gateway and the entire policy layer depend only on this narrow structural
interface, never on a concrete backend. Both the in-memory ``MockServiceNow``
(async-adapted) and a future httpx-based ``ServiceNowClient`` satisfy it, so the
gateway runs unchanged against either backend (Requirement 11.1).

Both methods are ``async`` because the real client is network-bound; the mock
adapts its in-memory body to the same signature.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field, SecretStr

from .errors import InstanceOperationFailed, InstanceUnreachable, MissingCredential
from .models import Operation, WriteRequest

# Environment variable names that supply ServiceNow credentials. Referenced by
# name only — never log or embed their values (Req 10.2, 10.3; security steering).
ENV_INSTANCE = "SN_INSTANCE"
ENV_USER = "SN_USER"
ENV_PASSWORD = "SN_PASSWORD"

# Characters that make up a lowercase-hex ServiceNow sys_id.
_SYS_ID_CHARS = frozenset("0123456789abcdef")


def is_sys_id(identifier: str) -> bool:
    """Return True iff ``identifier`` is a 32-char lowercase-hex sys_id.

    Pure classifier — no I/O, no exceptions. Agents naturally pass display
    numbers (e.g. ``INC0000060``); those are NOT sys_ids and must be resolved.
    An uppercase-hex string of length 32 is NOT a sys_id either.
    """
    return len(identifier) == 32 and all(c in _SYS_ID_CHARS for c in identifier)


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

    async def search(
        self, table: str, query: str, limit: int, fields: list[str] | None
    ) -> list[dict[str, str]]:
        """Search records matching ``query``. Read-only; MUST NOT mutate.

        Returns up to ``limit`` matching records as field dicts. ``query`` uses
        ServiceNow encoded-query syntax (``field=value`` clauses ANDed on ``^``);
        an empty query matches all. When ``fields`` is provided the result is
        projected to only those field names.
        """
        ...

    async def preview(self, req: WriteRequest) -> list[dict[str, str]]:
        """Return the before/after of what WOULD change. MUST NOT mutate. (Req 6.2)"""
        ...

    async def apply(self, req: WriteRequest) -> int:
        """Apply the change for real. Returns the number of affected records."""
        ...


class ServiceNowConfig(BaseModel):
    """ServiceNow connection config, loaded ONLY from environment variables.

    The password is a :class:`~pydantic.SecretStr`, so it is excluded from
    ``repr``/``str`` and structured logs by default (Req 10.3). ``from_env`` is
    the only supported loader — there is no literal, CLI, or non-gitignored file
    fallback (Req 10.1). Missing/empty variables raise :class:`MissingCredential`
    naming the variable, never a value (Req 10.2).
    """

    instance_url: str = Field(..., min_length=1)  # from SN_INSTANCE
    user: str = Field(..., min_length=1)  # from SN_USER
    password: SecretStr = Field(...)  # from SN_PASSWORD
    # Per-request timeout bound: default 30s, clamped to [1, 60] (Req 11.2).
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=60.0)

    @classmethod
    def from_env(cls) -> ServiceNowConfig:
        """Read ``SN_INSTANCE`` / ``SN_USER`` / ``SN_PASSWORD`` from the environment.

        A missing OR empty (whitespace-only) variable raises
        :class:`MissingCredential` whose message names only the offending
        variable — never its value, even masked or hashed (Req 10.2).
        """
        instance_url = os.environ.get(ENV_INSTANCE, "")
        user = os.environ.get(ENV_USER, "")
        password = os.environ.get(ENV_PASSWORD, "")

        # Check each required variable; report by NAME only.
        if not instance_url.strip():
            raise MissingCredential(ENV_INSTANCE)
        if not user.strip():
            raise MissingCredential(ENV_USER)
        if not password.strip():
            raise MissingCredential(ENV_PASSWORD)

        return cls(
            instance_url=instance_url,
            user=user,
            password=SecretStr(password),
        )


class ServiceNowClient:
    """Async httpx-based ServiceNow REST client satisfying ``ServiceNowInstance``.

    Built from a :class:`ServiceNowConfig`. ``preview`` performs read-only GETs
    to build before/after without mutating the instance; ``apply`` performs
    POST/PATCH/DELETE on the ServiceNow Table API. Every request carries a
    per-request :class:`httpx.Timeout` derived from ``config.timeout_seconds``.

    Error contract:

    - A connection-level failure (the request never reaches the instance) raises
      :class:`InstanceUnreachable`, so no mutation can have occurred (Req 11.4).
    - A failure AFTER a connection is established (bad status, read error,
      timeout mid-exchange) raises :class:`InstanceOperationFailed`. ``apply``
      issues one HTTP operation per record and stops at the first failure, so a
      failure means the pre-operation state of every not-yet-processed record is
      preserved (Req 11.5). Credentials appear in logs by env var NAME only.
    """

    def __init__(
        self, config: ServiceNowConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._timeout = httpx.Timeout(config.timeout_seconds)
        # transport is injectable for tests (httpx.MockTransport); None uses the
        # default network transport in production.
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        """Build an AsyncClient bound to the instance with basic auth + timeout."""
        return httpx.AsyncClient(
            base_url=self._config.instance_url.rstrip("/"),
            auth=(self._config.user, self._config.password.get_secret_value()),
            timeout=self._timeout,
            transport=self._transport,
            headers={"Accept": "application/json"},
        )

    @staticmethod
    def _record_url(table: str, sys_id: str) -> str:
        return f"/api/now/table/{table}/{sys_id}"

    @staticmethod
    def _table_url(table: str) -> str:
        return f"/api/now/table/{table}"

    async def _resolve_sys_id(self, client: httpx.AsyncClient, table: str, identifier: str) -> str:
        """Resolve a display number (e.g. ``INC0000060``) to its sys_id.

        If ``identifier`` is already a sys_id (:func:`is_sys_id`), return it
        unchanged with no network call. Otherwise issue a single read-only GET
        querying ``number=<identifier>`` and return the matched record's sys_id.
        Never mutates. The not-found message names the table and number only —
        never any field value (security steering).
        """
        if is_sys_id(identifier):
            return identifier
        try:
            resp = await client.get(
                self._table_url(table),
                params={
                    "sysparm_query": f"number={identifier}",
                    "sysparm_fields": "sys_id",
                    "sysparm_limit": "1",
                },
            )
        except httpx.ConnectError as exc:
            raise InstanceUnreachable(
                f"could not connect to ServiceNow instance ({ENV_INSTANCE})"
            ) from exc
        except httpx.TransportError as exc:
            raise InstanceOperationFailed(f"resolve {table} number failed") from exc

        if resp.is_error:
            raise InstanceOperationFailed(f"resolve {table} number returned {resp.status_code}")
        result = resp.json().get("result", [])
        if not result:
            raise InstanceOperationFailed(f"no {table} record found for number {identifier}")
        return str(result[0]["sys_id"])

    async def get_record(self, table: str, sys_id: str) -> dict[str, str] | None:
        """Read a single record's current fields. Read-only; never mutates.

        Returns the record's field dict, ``None`` when the record is absent
        (HTTP 404). Any other failure is surfaced as a typed error.
        """
        async with self._client() as client:
            resolved = await self._resolve_sys_id(client, table, sys_id)
            try:
                resp = await client.get(self._record_url(table, resolved))
            except httpx.ConnectError as exc:
                raise InstanceUnreachable(
                    f"could not connect to ServiceNow instance ({ENV_INSTANCE})"
                ) from exc
            except httpx.TransportError as exc:
                raise InstanceOperationFailed(f"GET {table}/{resolved} failed") from exc

            if resp.status_code == 404:
                return None
            if resp.is_error:
                raise InstanceOperationFailed(f"GET {table}/{resolved} returned {resp.status_code}")
            result = resp.json().get("result", {})
            return {str(k): str(v) for k, v in result.items()}

    async def search(
        self, table: str, query: str, limit: int, fields: list[str] | None
    ) -> list[dict[str, str]]:
        """Search records matching ``query``. Read-only; never mutates.

        Issues a single read-only GET to the table URL with the encoded query,
        a hard-capped limit (at most 50), and an optional field projection. No
        POST/PATCH/DELETE is ever issued.
        """
        params: dict[str, str] = {
            "sysparm_query": query,
            "sysparm_limit": str(min(max(limit, 1), 50)),
        }
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        async with self._client() as client:
            try:
                resp = await client.get(self._table_url(table), params=params)
            except httpx.ConnectError as exc:
                raise InstanceUnreachable(
                    f"could not connect to ServiceNow instance ({ENV_INSTANCE})"
                ) from exc
            except httpx.TransportError as exc:
                raise InstanceOperationFailed(f"search {table} failed") from exc

            if resp.is_error:
                raise InstanceOperationFailed(f"search {table} returned {resp.status_code}")
            return [
                {str(k): str(v) for k, v in row.items()} for row in resp.json().get("result", [])
            ]

    async def preview(self, req: WriteRequest) -> list[dict[str, str]]:
        """Return the before/after of what WOULD change. Read-only (Req 6.2).

        Issues one GET per targeted record to read its current state, then
        computes the hypothetical ``after`` in memory. No write is performed.
        """
        changes: list[dict[str, str]] = []
        async with self._client() as client:
            for sys_id in req.record_ids or ["<new>"]:
                before: dict[str, str] = {}
                if sys_id != "<new>":
                    resolved = await self._resolve_sys_id(client, req.table, sys_id)
                    try:
                        resp = await client.get(self._record_url(req.table, resolved))
                    except httpx.ConnectError as exc:
                        raise InstanceUnreachable(
                            f"could not connect to ServiceNow instance ({ENV_INSTANCE})"
                        ) from exc
                    except httpx.TransportError as exc:
                        raise InstanceOperationFailed(f"GET {req.table}/{resolved} failed") from exc
                    if resp.status_code != 404:
                        if resp.is_error:
                            raise InstanceOperationFailed(
                                f"GET {req.table}/{resolved} returned {resp.status_code}"
                            )
                        result = resp.json().get("result", {})
                        before = {str(k): str(v) for k, v in result.items()}
                if req.operation is Operation.DELETE:
                    after: dict[str, str] = {}
                else:
                    after = {**before, **req.fields}
                changes.append({"sys_id": sys_id, "before": str(before), "after": str(after)})
        return changes

    async def apply(self, req: WriteRequest) -> int:
        """Apply the change for real. Returns the number of affected records.

        Issues one HTTP operation per record and stops at the first failure. A
        connect failure before any record is processed raises
        :class:`InstanceUnreachable`; a post-connect failure raises
        :class:`InstanceOperationFailed`, leaving every record not yet processed
        in its pre-operation state (Req 11.5).
        """
        affected = 0
        async with self._client() as client:
            if req.operation is Operation.DELETE:
                for sys_id in req.record_ids:
                    resolved = await self._resolve_sys_id(client, req.table, sys_id)
                    resp = await self._send(client, "DELETE", self._record_url(req.table, resolved))
                    if resp.status_code == 404:
                        continue
                    if resp.is_error:
                        raise InstanceOperationFailed(
                            f"DELETE {req.table}/{resolved} returned {resp.status_code}"
                        )
                    affected += 1
                return affected

            if req.operation is Operation.CREATE and not req.record_ids:
                resp = await self._send(client, "POST", self._table_url(req.table), json=req.fields)
                if resp.is_error:
                    raise InstanceOperationFailed(f"POST {req.table} returned {resp.status_code}")
                return 1

            for sys_id in req.record_ids:
                resolved = await self._resolve_sys_id(client, req.table, sys_id)
                resp = await self._send(
                    client, "PATCH", self._record_url(req.table, resolved), json=req.fields
                )
                if resp.is_error:
                    raise InstanceOperationFailed(
                        f"PATCH {req.table}/{resolved} returned {resp.status_code}"
                    )
                affected += 1
        return affected

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        json: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue one request, mapping transport errors to typed exceptions.

        A :class:`httpx.ConnectError` means the request never reached the
        instance → :class:`InstanceUnreachable` (no mutation). Any other
        transport-level error happened after/while connected →
        :class:`InstanceOperationFailed`.
        """
        try:
            return await client.request(method, url, json=json)
        except httpx.ConnectError as exc:
            raise InstanceUnreachable(
                f"could not connect to ServiceNow instance ({ENV_INSTANCE})"
            ) from exc
        except httpx.TransportError as exc:
            raise InstanceOperationFailed(f"{method} {url} failed") from exc
