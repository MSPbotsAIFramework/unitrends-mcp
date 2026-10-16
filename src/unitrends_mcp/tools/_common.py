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
"""Shared argument types, date / response helpers and the error-handling wrapper."""

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import UnitrendsClient, UnitrendsError

logger = logging.getLogger("unitrends_mcp.tools")

ClientFactory = Callable[[], UnitrendsClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
RESTORE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured",
    "Unitrends credentials are missing for this request (X-Unitrends-Base-URL, X-Unitrends-Username, X-Unitrends-Password).",
    False,
)

ApplianceId = Annotated[str, Field(description="System (appliance) id from unitrends_list_appliances.", min_length=1)]
OptionalApplianceId = Annotated[
    str | None, Field(description="System (appliance) id from unitrends_list_appliances; omit for all systems.")
]
AssetId = Annotated[str, Field(description="Asset (client) id from unitrends_list_assets.", min_length=1)]
Since = Annotated[str | None, Field(description="ISO 8601 start, e.g. 2026-06-01 or 2026-06-01T00:00:00Z.")]
Until = Annotated[str | None, Field(description="ISO 8601 end (default now).")]

DEFAULT_WINDOW_DAYS = 7


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def numeric_id(value: str, name: str) -> str:
    """Ids go into URL paths and query strings; Unitrends ids are integers."""
    text = str(value).strip()
    if not text.lstrip("-").isdigit():
        raise ValueError(f"{name} must be a numeric Unitrends id (got {value!r}).")
    return text


def rows(payload: Any, *keys: str) -> list:
    """The record list of a Unitrends response ({"data": [...]}, {"clients": [...]}, ...)."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in (*keys, "data"):
        if isinstance(payload.get(key), list):
            return payload[key]
    lists = [v for v in payload.values() if isinstance(v, list)]
    return lists[0] if len(lists) == 1 else []


def parse_datetime(value: str, name: str) -> datetime:
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f'{name} must be an ISO 8601 date or datetime, e.g. "2026-06-01" (got "{value}").') from e
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def date_window(since: str | None, until: str | None, default_days: int = DEFAULT_WINDOW_DAYS) -> tuple[datetime, datetime]:
    end = parse_datetime(until, "until") if until else datetime.now(UTC)
    start = parse_datetime(since, "since") if since else end - timedelta(days=default_days)
    if start > end:
        raise ValueError("since must be before until.")
    return start, end


def api_date(value: datetime) -> str:
    """The MM/DD/YYYY form the Unitrends start_date / end_date filters take."""
    return value.strftime("%m/%d/%Y")


def result_ids(payload: Any) -> list[str]:
    """Ids from a {"result": [{"id": "827, 828"}]} write response."""
    ids: list[str] = []
    for item in payload.get("result") or [] if isinstance(payload, dict) else []:
        if isinstance(item, dict) and item.get("id") is not None:
            ids.extend(part.strip() for part in str(item["id"]).split(",") if part.strip())
    return ids


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[UnitrendsClient], Awaitable[Any]],
) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    started = time.monotonic()
    try:
        result = await action(client)
    except UnitrendsError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"result": None, "note": "Unitrends returned an empty response."}
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
