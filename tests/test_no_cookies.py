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
"""The HTTP client is shared by every tenant, so it must never keep or send cookies."""

import httpx

from unitrends_mcp import api_client


def _echo(request: httpx.Request) -> httpx.Response:
    tenant = request.headers.get("x-tenant", "?")
    return httpx.Response(
        200, json={"cookie": request.headers.get("cookie", "none")}, headers={"Set-Cookie": f"vendor_session={tenant}; Path=/"}
    )


async def test_cookies_set_for_one_tenant_are_never_sent_for_another():
    client = httpx.AsyncClient(cookies=api_client._no_cookies(), transport=httpx.MockTransport(_echo))
    seen = [
        (await client.get("https://api.example.com/items", headers={"x-tenant": tenant})).json()["cookie"]
        for tenant in ("tenant-a", "tenant-b", "tenant-a")
    ]
    await client.aclose()
    assert seen == ["none", "none", "none"]
    assert len(client.cookies.jar) == 0


async def test_shared_client_uses_the_cookie_rejecting_jar(monkeypatch):
    if hasattr(api_client, "_shared_http_client"):
        monkeypatch.setattr(api_client, "_shared_http_client", None)
    if hasattr(api_client, "_shared_clients"):
        monkeypatch.setattr(api_client, "_shared_clients", {})
    client = api_client._get_shared_http_client()
    try:
        assert isinstance(client.cookies.jar._policy, api_client._RejectAllCookies)
    finally:
        await client.aclose()
