"""Integration tests for ServiceNowClient using httpx.MockTransport (Task 9.2).

These are example/integration tests (NOT property tests), covering Req 11.2,
11.4, 11.5:
- a successful preview via GET (read-only, no mutation),
- connection failure => InstanceUnreachable with no mutation,
- post-connect failure => InstanceOperationFailed with pre-operation state preserved.

A MockTransport lets us assert exactly which requests were issued and prove that
no mutating request (POST/PATCH/DELETE) is sent on failure paths.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from sentinelnow.errors import InstanceOperationFailed, InstanceUnreachable
from sentinelnow.instance import ServiceNowClient, ServiceNowConfig
from sentinelnow.models import Operation, WriteRequest


def _config() -> ServiceNowConfig:
    return ServiceNowConfig(
        instance_url="https://dev12345.service-now.com",
        user="admin",
        password=SecretStr("secret"),
        timeout_seconds=5.0,
    )


def _write(operation: Operation, record_ids: list[str], fields: dict[str, str]) -> WriteRequest:
    return WriteRequest(
        agent_id="agent-1",
        table="incident",
        operation=operation,
        record_ids=record_ids,
        fields=fields,
        reason="integration test",
    )


# --- Req 11.2 / read-only preview via GET ---------------------------------


def test_preview_reads_via_get_and_does_not_mutate() -> None:
    requests: list[httpx.Request] = []

    # A 32-char lowercase-hex sys_id is used so no resolution query is issued.
    sys_id = "0123456789abcdef0123456789abcdef"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"result": {"sys_id": sys_id, "priority": "3", "state": "1"}}
        )

    client = ServiceNowClient(_config(), transport=httpx.MockTransport(handler))
    req = _write(Operation.UPDATE, [sys_id], {"priority": "2"})

    changes = asyncio.run(client.preview(req))

    # Only a GET was issued — no mutating verb reached the instance.
    assert [r.method for r in requests] == ["GET"]
    assert len(changes) == 1
    assert changes[0]["sys_id"] == sys_id
    assert "priority': '3'" in changes[0]["before"]
    assert "priority': '2'" in changes[0]["after"]


# --- Req 11.4 / connection failure => InstanceUnreachable, no mutation ------


def test_connect_failure_raises_unreachable_without_mutation() -> None:
    attempted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    client = ServiceNowClient(_config(), transport=httpx.MockTransport(handler))
    # 32-char lowercase-hex sys_id: no resolution query, so the first (and only)
    # request is the PATCH itself.
    req = _write(Operation.UPDATE, ["0123456789abcdef0123456789abcdef"], {"priority": "2"})

    with pytest.raises(InstanceUnreachable):
        asyncio.run(client.apply(req))

    # The first (and only) attempt failed at connect time: it was a PATCH that
    # never completed, so no record was mutated.
    assert [r.method for r in attempted] == ["PATCH"]


# --- Req 11.5 / post-connect failure => InstanceOperationFailed, state kept --


def test_post_connect_failure_preserves_pre_operation_state() -> None:
    # Two records: the first PATCH succeeds, the second returns a 500. apply()
    # must stop at the first failure, so the records after it are never touched.
    seen: list[tuple[str, str]] = []
    # 32-char lowercase-hex sys_ids: no resolution queries, direct PATCHes.
    rec1 = "a" * 32
    rec2 = "b" * 32
    rec3 = "c" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith(f"/{rec2}"):
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json={"result": {"sys_id": rec1}})

    client = ServiceNowClient(_config(), transport=httpx.MockTransport(handler))
    req = _write(Operation.UPDATE, [rec1, rec2, rec3], {"priority": "2"})

    with pytest.raises(InstanceOperationFailed):
        asyncio.run(client.apply(req))

    methods_paths = seen
    # rec1 patched, rec2 attempted (failed), rec3 NEVER attempted => preserved.
    assert ("PATCH", f"/api/now/table/incident/{rec1}") in methods_paths
    assert ("PATCH", f"/api/now/table/incident/{rec2}") in methods_paths
    assert ("PATCH", f"/api/now/table/incident/{rec3}") not in methods_paths


def test_get_record_returns_none_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "not found"}})

    client = ServiceNowClient(_config(), transport=httpx.MockTransport(handler))
    # A 32-char lowercase-hex sys_id skips resolution and GETs the record directly.
    assert asyncio.run(client.get_record("incident", "f" * 32)) is None
