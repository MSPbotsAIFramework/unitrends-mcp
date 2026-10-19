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
"""Running jobs and job history."""

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
    api_date,
    compact,
    date_window,
    numeric_id,
    rows,
    run_tool,
)

JobType = Literal["backup", "restore", "replication", "archive", "ir", "flr", "certification", "backup-copy"]
JobTypeFilter = Annotated[JobType | None, Field(description="Only this kind of job (ir = instant recovery, flr = file-level recovery).")]


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def unitrends_list_running_jobs(applianceId: OptionalApplianceId = None, type: JobTypeFilter = None) -> str:
        """List jobs running or queued now: name, type, asset, percent complete, elapsed time and system."""

        async def action(client: UnitrendsClient) -> dict:
            sid = numeric_id(applianceId, "applianceId") if applianceId else None
            path = f"jobs/active/{type}" if type else "jobs/active"
            items = rows(await client.get(path, compact({"sid": sid})), "data")
            return {"jobs": items, "count": len(items)}

        return await run_tool("unitrends_list_running_jobs", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def unitrends_list_job_history(
        since: Since = None,
        until: Until = None,
        applianceId: OptionalApplianceId = None,
        type: JobTypeFilter = None,
        limit: Annotated[int | None, Field(description="Most recent jobs to return (1-1000, default 50).", ge=1, le=1000)] = None,
    ) -> str:
        """List finished jobs, newest first, with result, size, duration and asset (default: last 7 days)."""

        async def action(client: UnitrendsClient) -> dict:
            start, end = date_window(since, until)
            sid = numeric_id(applianceId, "applianceId") if applianceId else None
            size = limit or 50
            params = compact(
                {
                    "sid": sid,
                    "start_date": api_date(start),
                    "end_date": api_date(end),
                    # limit only works on the flattened, sorted listing.
                    "showFlat": True,
                    "sort": "desc",
                    "limit": size,
                }
            )
            path = f"jobs/history/{type}" if type else "jobs/history"
            items = rows(await client.get(path, params), "data")
            return {
                "since": start.date().isoformat(),
                "until": end.date().isoformat(),
                "jobs": items,
                "count": len(items),
                "has_more": len(items) >= size,
            }

        return await run_tool("unitrends_list_job_history", client_factory, action)
