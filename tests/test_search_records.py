"""Tests for the read-only search_records capability (FEAT-001).

Two layers are covered:

(a) ``MockServiceNow.search`` over synthetic incidents — field=value filtering,
    ANDed clauses, match-all, projection by ``fields``, and the hard cap of 50.
(b) ``ServiceNowClient.search`` via ``httpx.MockTransport`` — asserts exactly one
    GET is issued (no mutating verb) and the parsed rows come back.

All data is synthetic (security steering: no real instance data).
"""

from __future__ import annotations

import asyncio

import httpx
from pydantic import SecretStr

from sentinelnow.instance import ServiceNowClient, ServiceNowConfig
from sentinelnow.mock_servicenow import MockServiceNow


def _config() -> ServiceNowConfig:
    return ServiceNowConfig(
        instance_url="https://dev12345.service-now.com",
        user="admin",
        password=SecretStr("secret"),
        timeout_seconds=5.0,
    )


def _seeded_mock() -> MockServiceNow:
    mock = MockServiceNow()
    mock.seed("incident", "INC0001", {"number": "INC0001", "priority": "1", "state": "2"})
    mock.seed("incident", "INC0002", {"number": "INC0002", "priority": "3", "state": "2"})
    mock.seed("incident", "INC0003", {"number": "INC0003", "priority": "1", "state": "6"})
    return mock


# --- MockServiceNow.search --------------------------------------------------


def test_mock_field_value_filter() -> None:
    mock = _seeded_mock()
    rows = asyncio.run(mock.search("incident", "priority=1", 10, None))
    numbers = {row["number"] for row in rows}
    assert numbers == {"INC0001", "INC0003"}


def test_mock_anded_clauses() -> None:
    mock = _seeded_mock()
    rows = asyncio.run(mock.search("incident", "priority=1^state=2", 10, None))
    numbers = {row["number"] for row in rows}
    assert numbers == {"INC0001"}


def test_mock_match_all_on_empty_query() -> None:
    mock = _seeded_mock()
    rows = asyncio.run(mock.search("incident", "", 10, None))
    assert len(rows) == 3

    rows_ws = asyncio.run(mock.search("incident", "   ", 10, None))
    assert len(rows_ws) == 3


def test_mock_projection_by_fields() -> None:
    mock = _seeded_mock()
    rows = asyncio.run(mock.search("incident", "priority=1", 10, ["number", "missing"]))
    # Only existing keys are projected; "missing" is dropped.
    assert all(set(row.keys()) == {"number"} for row in rows)
    assert {row["number"] for row in rows} == {"INC0001", "INC0003"}


def test_mock_limit_capped_at_50() -> None:
    mock = MockServiceNow()
    for i in range(120):
        mock.seed("incident", f"INC{i:05d}", {"number": f"INC{i:05d}", "priority": "3"})
    rows = asyncio.run(mock.search("incident", "", 1000, None))
    assert len(rows) == 50


def test_mock_unknown_table_returns_empty() -> None:
    mock = _seeded_mock()
    assert asyncio.run(mock.search("problem", "", 10, None)) == []


# --- ServiceNowClient.search via httpx.MockTransport ------------------------


def test_client_search_issues_single_get_and_parses_rows() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "result": [
                    {"sys_id": "a" * 32, "number": "INC0001", "priority": "1"},
                    {"sys_id": "b" * 32, "number": "INC0003", "priority": "1"},
                ]
            },
        )

    client = ServiceNowClient(_config(), transport=httpx.MockTransport(handler))
    rows = asyncio.run(client.search("incident", "priority=1", 10, ["number", "priority"]))

    # Exactly one GET — no mutating verb reached the instance.
    assert [r.method for r in requests] == ["GET"]
    # Parameters are forwarded (query, capped limit, projected fields).
    url = requests[0].url
    assert url.params.get("sysparm_query") == "priority=1"
    assert url.params.get("sysparm_limit") == "10"
    assert url.params.get("sysparm_fields") == "number,priority"

    assert [row["number"] for row in rows] == ["INC0001", "INC0003"]
    # Values are coerced to str.
    assert all(isinstance(v, str) for row in rows for v in row.values())


def test_client_search_caps_limit_at_50() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": []})

    client = ServiceNowClient(_config(), transport=httpx.MockTransport(handler))
    asyncio.run(client.search("incident", "", 1000, None))

    assert requests[0].url.params.get("sysparm_limit") == "50"
    # No field projection param when fields is falsy.
    assert "sysparm_fields" not in requests[0].url.params
