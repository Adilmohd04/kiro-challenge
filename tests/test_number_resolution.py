"""Tests for display-number -> sys_id resolution (FEAT-001).

Agents naturally pass ServiceNow display numbers (e.g. ``INC0000060``), which
are not sys_ids and 404 on the record endpoint. ``is_sys_id`` is a pure
classifier; ``ServiceNowClient`` resolves non-sys_id identifiers via a
read-only ``number=<value>`` query before any record GET/PATCH/DELETE.

Pure classifier tests need no network. MockTransport tests capture the exact
requests issued to prove: (a) a number triggers a resolution GET then the
record op, (b) a sys_id triggers no resolution, (c) an unresolvable number
raises InstanceOperationFailed and issues NO mutating verb.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from sentinelnow.errors import InstanceOperationFailed
from sentinelnow.instance import ServiceNowClient, ServiceNowConfig, is_sys_id
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
        reason="number resolution test",
    )


# --- Pure classifier -------------------------------------------------------


def test_is_sys_id_accepts_32_char_lowercase_hex() -> None:
    assert is_sys_id("a" * 32) is True
    assert is_sys_id("0123456789abcdef0123456789abcdef") is True


def test_is_sys_id_rejects_display_number() -> None:
    assert is_sys_id("INC0000060") is False


def test_is_sys_id_rejects_uppercase_hex() -> None:
    assert is_sys_id("A" * 32) is False
    assert is_sys_id("0123456789ABCDEF0123456789ABCDEF") is False


def test_is_sys_id_rejects_wrong_length() -> None:
    assert is_sys_id("a" * 31) is False
    assert is_sys_id("a" * 33) is False


def test_is_sys_id_rejects_non_hex_char() -> None:
    # 32 chars but one is not a hex digit.
    assert is_sys_id("g" + "a" * 31) is False


# --- Resolution wired into client ops --------------------------------------


def test_number_in_record_ids_resolves_before_patch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/now/table/incident":
            return httpx.Response(200, json={"result": [{"sys_id": "abc123"}]})
        return httpx.Response(200, json={"result": {"sys_id": "abc123"}})

    client = ServiceNowClient(_config(), transport=httpx.MockTransport(handler))
    req = _write(Operation.UPDATE, ["INC0000060"], {"priority": "2"})

    affected = asyncio.run(client.apply(req))

    assert affected == 1
    # First a read-only resolution query, then the PATCH on the resolved id.
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/now/table/incident"
    assert requests[0].url.params["sysparm_query"] == "number=INC0000060"
    assert requests[1].method == "PATCH"
    assert requests[1].url.path == "/api/now/table/incident/abc123"


def test_sys_id_in_record_ids_skips_resolution() -> None:
    requests: list[httpx.Request] = []
    sys_id = "0123456789abcdef0123456789abcdef"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": {"sys_id": sys_id}})

    client = ServiceNowClient(_config(), transport=httpx.MockTransport(handler))
    req = _write(Operation.UPDATE, [sys_id], {"priority": "2"})

    affected = asyncio.run(client.apply(req))

    assert affected == 1
    # No resolution query — only the direct PATCH on the sys_id.
    assert [r.method for r in requests] == ["PATCH"]
    assert requests[0].url.path == f"/api/now/table/incident/{sys_id}"


def test_unresolvable_number_raises_and_issues_no_mutation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # Resolution query returns no match.
        return httpx.Response(200, json={"result": []})

    client = ServiceNowClient(_config(), transport=httpx.MockTransport(handler))
    req = _write(Operation.UPDATE, ["INC9999999"], {"priority": "2"})

    with pytest.raises(InstanceOperationFailed):
        asyncio.run(client.apply(req))

    # Only the read-only resolution GET was issued; no mutating verb reached
    # the instance, proving no mutation occurred.
    assert [r.method for r in requests] == ["GET"]
    assert all(r.method not in ("POST", "PATCH", "DELETE") for r in requests)
