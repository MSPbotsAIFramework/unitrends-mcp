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
"""Recovery points (restorable backups), restores and their status."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import UnitrendsClient, UnitrendsError
from ._common import (
    READ_ONLY,
    RESTORE,
    AssetId,
    ClientFactory,
    OptionalApplianceId,
    Since,
    Until,
    api_date,
    compact,
    date_window,
    numeric_id,
    result_ids,
    rows,
    run_tool,
)


async def _local_system_id(client: UnitrendsClient) -> str:
    systems = [s for s in rows(await client.get("systems", {"include_pending": 0}), "systems") if isinstance(s, dict)]
    local = [s for s in systems if s.get("local") is True] or (systems if len(systems) == 1 else [])
    if not local or local[0].get("id") is None:
        raise ValueError("Could not tell which system holds the backup; pass applianceId.")
    return str(local[0]["id"])


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def unitrends_list_recovery_points(
        assetId: AssetId,
        applianceId: OptionalApplianceId = None,
        since: Since = None,
        until: Until = None,
    ) -> str:
        """List an asset's restorable backups (recovery points): backup id, type, date, status, size, storage."""

        async def action(client: UnitrendsClient) -> dict:
            params: dict[str, Any] = {
                "view": "instance",
                "cid": numeric_id(assetId, "assetId"),
                "sid": numeric_id(applianceId, "applianceId") if applianceId else None,
            }
            if since or until:
                start, end = date_window(since, until)
                params |= {"start_date": api_date(start), "end_date": api_date(end)}
            instances = rows(await client.get("backups/catalog", compact(params)), "catalog")
            count = sum(len(i.get("backups") or []) for i in instances if isinstance(i, dict))
            return {"assetId": params["cid"], "instances": instances, "recovery_points": count}

        return await run_tool("unitrends_list_recovery_points", client_factory, action)

    @mcp.tool(annotations=RESTORE)
    async def unitrends_queue_restore(
        recoveryPointId: Annotated[str, Field(description="Backup id from unitrends_list_recovery_points.", min_length=1)],
        targetAssetId: Annotated[str | None, Field(description="Asset (client) to restore to; default the original.")] = None,
        targetPath: Annotated[str | None, Field(description="File-level restores: directory to restore into.")] = None,
        applianceId: OptionalApplianceId = None,
        confirm: Annotated[bool | None, Field(description="Must be true to start the restore; otherwise only a preview is returned.")] = None,
    ) -> str:
        """Restore a backup onto a system. Overwrites data on the target: needs confirm=true.

        Without confirm=true this only returns what would happen. Set confirm
        only after the user explicitly asks for that exact backup and target.
        Track it with unitrends_get_restore_status.
        """

        async def action(client: UnitrendsClient) -> dict:
            backup_id = numeric_id(recoveryPointId, "recoveryPointId")
            target_client = numeric_id(targetAssetId, "targetAssetId") if targetAssetId else None
            path = (targetPath or "").strip() or None
            plan = compact(
                {
                    "recoveryPointId": backup_id,
                    "targetAssetId": target_client or "original asset",
                    "targetPath": path or "original location",
                }
            )
            if confirm is not True:
                return {
                    "status": "not_started",
                    "restore": plan,
                    "message": "Nothing was restored. This restore writes data back onto the target and can overwrite "
                    "files. After the user explicitly approves it, call again with confirm=true.",
                }
            sid = numeric_id(applianceId, "applianceId") if applianceId else await _local_system_id(client)
            body = compact(
                {
                    "backup_id": int(backup_id),
                    "client_id": int(target_client) if target_client else None,
                    "includes": [],
                    "excludes": [],
                    "before_cmd": "",
                    "after_cmd": "",
                    "target": {"directory": path} if path else None,
                }
            )
            payload = await client.post("restore/full", body, {"sid": sid})
            ids = result_ids(payload)
            return compact(
                {
                    "status": "queued",
                    "restore": plan,
                    "applianceId": sid,
                    "restoreIds": ids or None,
                    "result": None if ids else payload,
                    "message": "Restore job queued; check it with unitrends_get_restore_status." if ids else None,
                }
            )

        return await run_tool("unitrends_queue_restore", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def unitrends_get_restore_status(
        restoreId: Annotated[str, Field(description="Restore job id from unitrends_queue_restore.", min_length=1)],
        applianceId: OptionalApplianceId = None,
    ) -> str:
        """Check a restore job: running (with percent complete) or finished (with its result)."""

        async def action(client: UnitrendsClient) -> dict:
            job_id = numeric_id(restoreId, "restoreId")
            sid = numeric_id(applianceId, "applianceId") if applianceId else None
            active = rows(await client.get("jobs/active/restore", compact({"sid": sid})), "data")
            match = next((j for j in active if isinstance(j, dict) and str(j.get("id")) == job_id), None)
            if match:
                return {"restoreId": job_id, "state": "running", "job": match}
            start, end = date_window(None, None, default_days=30)
            history = rows(
                await client.get(
                    "jobs/history/restore",
                    compact({"sid": sid, "start_date": api_date(start), "end_date": api_date(end), "showFlat": True, "sort": "desc", "limit": 500}),
                ),
                "data",
            )
            match = next((j for j in history if isinstance(j, dict) and str(j.get("id")) == job_id), None)
            if match:
                return {"restoreId": job_id, "state": "finished", "job": match}
            raise UnitrendsError(404, f"No running or recent (30 days) restore job with id {job_id}.")

        return await run_tool("unitrends_get_restore_status", client_factory, action)
