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
"""Systems (appliances) and the protected assets (clients) on them."""

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..api_client import UnitrendsClient, UnitrendsError
from ._common import (
    READ_ONLY,
    ApplianceId,
    AssetId,
    ClientFactory,
    OptionalApplianceId,
    compact,
    numeric_id,
    rows,
    run_tool,
)


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def unitrends_list_appliances() -> str:
        """List the systems (appliances) this login manages: id, name, role, version, status and capacity."""

        async def action(client: UnitrendsClient) -> dict:
            items = rows(await client.get("systems", {"include_pending": 0}), "systems")
            return {"appliances": items, "count": len(items)}

        return await run_tool("unitrends_list_appliances", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def unitrends_get_appliance(applianceId: ApplianceId) -> str:
        """Get one system's status, role, capacity and hardware details (version, OS, CPU, memory)."""

        async def action(client: UnitrendsClient) -> dict:
            sid = numeric_id(applianceId, "applianceId")
            listing, details = await asyncio.gather(
                client.get("systems", {"sid": sid}),
                client.get("systems/details", {"sid": sid}),
                return_exceptions=True,
            )
            if isinstance(listing, BaseException):
                raise listing
            match = [s for s in rows(listing, "systems") if isinstance(s, dict) and str(s.get("id")) == sid]
            if not match:
                raise UnitrendsError(404, f"No system with id {sid}; use unitrends_list_appliances for valid ids.")
            record: dict[str, Any] = dict(match[0])
            if not isinstance(details, BaseException):
                appliance = rows(details, "appliance")
                if appliance and isinstance(appliance[0], dict):
                    record["details"] = appliance[0]
            return record

        return await run_tool("unitrends_get_appliance", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def unitrends_list_assets(applianceId: OptionalApplianceId = None) -> str:
        """List protected assets (clients: servers, VMs, hosts) with OS, agent version and type."""

        async def action(client: UnitrendsClient) -> dict:
            sid = numeric_id(applianceId, "applianceId") if applianceId else None
            items = rows(await client.get("clients", compact({"sid": sid})), "clients")
            return compact({"applianceId": sid, "assets": items, "count": len(items)})

        return await run_tool("unitrends_list_assets", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def unitrends_get_asset(applianceId: ApplianceId, assetId: AssetId) -> str:
        """Get one protected asset (client): OS, agent, applications and credentials info."""

        async def action(client: UnitrendsClient) -> Any:
            sid = numeric_id(applianceId, "applianceId")
            payload = await client.get(f"clients/{numeric_id(assetId, 'assetId')}", {"sid": sid})
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                return payload["data"]
            found = rows(payload, "clients", "client")
            return found[0] if len(found) == 1 else payload

        return await run_tool("unitrends_get_asset", client_factory, action)
