# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Credential middleware: 401/400 on bad headers, per-request isolation."""

import asyncio

import pytest
from starlette.testclient import TestClient

from unitrends_mcp.__main__ import build_http_app
from unitrends_mcp.server import (
    GatewayCredentialsMiddleware,
    _credentials_var,
    create_mcp_server,
    get_client_from_context,
    normalize_base_url,
)

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CREDS = {
    "X-Unitrends-Base-URL": "https://backup.example.com",
    "X-Unitrends-Username": "root",
    "X-Unitrends-Password": "password",
}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize("drop", list(CREDS))
def test_each_required_header_is_required(drop):
    headers = {k: v for k, v in CREDS.items() if k != drop}
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert drop in resp.json()["message"]


def test_placeholder_values_count_as_missing():
    headers = MCP_HEADERS | CREDS | {"X-Unitrends-Password": "${UNITRENDS_PASSWORD}"}
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=headers)
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "extra",
    [{"X-Unitrends-Base-URL": "http://backup.example.com"}, {"X-Unitrends-Verify-TLS": "maybe"}],
)
def test_bad_values_return_400(extra):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS | extra)
    assert resp.status_code == 400


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert resp.status_code == 200
    for name in ("unitrends_list_appliances", "unitrends_queue_restore", "unitrends_get_success_rate"):
        assert name in resp.text


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://backup.example.com", "https://backup.example.com"),
        ("backup.example.com/", "https://backup.example.com"),
        ("https://backup.example.com/api/", "https://backup.example.com"),
        ("https://10.0.0.5:443/api", "https://10.0.0.5:443"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://backup.example.com", None),
        ("https://", None),
    ],
)
def test_normalize_base_url(value, expected):
    assert normalize_base_url(value) == expected


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
    return seen


def _raw(extra: dict[str, str] | None = None) -> list[tuple[bytes, bytes]]:
    return [(k.lower().encode(), v.encode()) for k, v in (CREDS | (extra or {})).items()]


def test_headers_reach_request_context_and_are_reset():
    seen = _run_middleware(_raw({"X-Unitrends-Base-URL": "backup.example.com/api"}))
    creds = seen["credentials"]
    assert (creds.username, creds.password, creds.verify_tls) == ("root", "password", True)
    assert seen["client"].api_url == "https://backup.example.com/api"
    assert _credentials_var.get() is None


@pytest.mark.parametrize("value,expected", [("false", False), ("0", False), ("TRUE", True), ("yes", True)])
def test_verify_tls_header(value, expected):
    assert _run_middleware(_raw({"X-Unitrends-Verify-TLS": value}))["credentials"].verify_tls is expected


def test_concurrent_requests_do_not_share_credentials():
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().username
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().username))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(user: str) -> dict:
        return {"type": "http", "path": "/mcp", "headers": _raw({"X-Unitrends-Username": user})}

    async def both():
        await asyncio.gather(middleware(scope("tenant-a"), receive, send), middleware(scope("tenant-b"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")]


def test_credentials_repr_hides_secrets():
    seen = _run_middleware(_raw({"X-Unitrends-Password": "very-secret-value", "X-Unitrends-Username": "user-value"}))
    text = repr(seen["credentials"])
    assert "very-secret-value" not in text and "user-value" not in text
