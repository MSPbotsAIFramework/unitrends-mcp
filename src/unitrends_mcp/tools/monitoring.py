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
"""Alerts and backup success-rate reporting."""

import math
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import UnitrendsClient
from ._common import (
    READ_ONLY,
    ClientFactory,
    OptionalApplianceId,
    Since,
    Until,
    compact,
    date_window,
    numeric_id,
    rows,
    run_tool,
)

_COUNTERS = ("successes", "warnings", "failures", "in_progress")


def _rate(successes: int, warnings: int, failures: int) -> float | None:
    finished = successes + warnings + failures
    return round((successes + warnings) / finished * 100, 1) if finished else None


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def unitrends_list_alerts(
        applianceId: OptionalApplianceId = None,
        severity: Annotated[Literal["notice", "warning", "error"] | None, Field(description="Only this severity.")] = None,
        limit: Annotated[int | None, Field(description="Most recent alerts to return (default 100).", ge=1, le=1000)] = None,
    ) -> str:
        """List open alerts, newest first: message, severity, source and when it was raised or updated."""

        async def action(client: UnitrendsClient) -> dict:
            sid = numeric_id(applianceId, "applianceId") if applianceId else None
            path = f"alerts/open/{severity}" if severity else "alerts/open"
            payload = await client.get(path, compact({"sid": sid, "max_items": limit or 100}))
            items = rows(payload, "alerts", "data")
            more = payload.get("more") if isinstance(payload, dict) else None
            return compact({"alerts": items, "count": len(items), "has_more": more if isinstance(more, bool) else None})

        return await run_tool("unitrends_list_alerts", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def unitrends_get_success_rate(since: Since = None, until: Until = None, applianceId: OptionalApplianceId = None) -> str:
        """Backup success rate for a period (default 7 days): totals and per-asset success / failure counts.

        Assets with failures are listed first; rate = (successes + warnings) / finished backups.
        """

        async def action(client: UnitrendsClient) -> dict:
            start, end = date_window(since, until)
            days = max(1, math.ceil((end - start).total_seconds() / 86400))
            sid = numeric_id(applianceId, "applianceId") if applianceId else None
            payload = await client.get("summary/status", compact({"days": days, "end": int(end.timestamp()), "sid": sid}))
            per_asset: dict[str, dict] = {}
            for day in rows(payload, "days"):
                for entry in (day.get("day") or []) if isinstance(day, dict) else []:
                    if not isinstance(entry, dict):
                        continue
                    key = str(entry.get("id", entry.get("client_name")))
                    asset = per_asset.setdefault(
                        key, {"assetId": entry.get("id"), "name": entry.get("client_name"), **{c: 0 for c in _COUNTERS}}
                    )
                    for counter in _COUNTERS:
                        value = entry.get(counter)
                        asset[counter] += value if isinstance(value, int) else 0
            assets = list(per_asset.values())
            for asset in assets:
                asset["success_rate"] = _rate(asset["successes"], asset["warnings"], asset["failures"])
            totals = {c: sum(a[c] for a in assets) for c in _COUNTERS}
            assets.sort(key=lambda a: (-a["failures"], str(a["name"] or "")))
            return {
                "since": start.date().isoformat(),
                "until": end.date().isoformat(),
                "days": days,
                "totals": totals | {"success_rate": _rate(totals["successes"], totals["warnings"], totals["failures"])},
                "assets_with_failures": sum(1 for a in assets if a["failures"]),
                "assets": assets,
            }

        return await run_tool("unitrends_get_success_rate", client_factory, action)
