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
"""tools/list snapshot and error-envelope mapping. No network access."""

import asyncio
import json

import pytest

from unitrends_mcp.api_client import UnitrendsError
from unitrends_mcp.server import INSTRUCTIONS, create_mcp_server

# Tool names are a public contract: existing agents and tenant connections
# depend on them. Changing this snapshot is a breaking change.
EXPECTED_TOOLS: dict[str, set[str]] = {
    "unitrends_list_appliances": set(),
    "unitrends_get_appliance": {"applianceId"},
    "unitrends_list_assets": set(),
    "unitrends_get_asset": {"applianceId", "assetId"},
    "unitrends_list_running_jobs": set(),
    "unitrends_list_job_history": set(),
    "unitrends_list_recovery_points": {"assetId"},
    "unitrends_queue_restore": {"recoveryPointId"},
    "unitrends_get_restore_status": {"restoreId"},
    "unitrends_list_alerts": set(),
    "unitrends_get_success_rate": set(),
}


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


def test_tool_names_are_stable(tools):
    assert set(tools) == set(EXPECTED_TOOLS)


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_required_params(tools, name):
    assert set(tools[name].inputSchema.get("required", [])) == EXPECTED_TOOLS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_description_budget(tools, name):
    description = (tools[name].description or "").strip()
    assert description, f"{name} has no description"
    assert len(description) <= 500
    assert len(description.splitlines()[0]) <= 100


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_only_restore_writes(tools, name):
    annotations = tools[name].annotations
    assert annotations is not None
    is_restore = name == "unitrends_queue_restore"
    assert annotations.readOnlyHint is (not is_restore)
    assert bool(annotations.destructiveHint) is is_restore


def test_restore_requires_explicit_confirmation(tools):
    tool = tools["unitrends_queue_restore"]
    assert "confirm" in tool.inputSchema["properties"]
    assert "confirm=true" in tool.description


def test_instructions_budget():
    assert len(INSTRUCTIONS) <= 1500


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (400, "invalid_argument", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (0, "upstream_error", True),
    ],
)
def test_error_envelope(status, code, retryable):
    envelope = json.loads(UnitrendsError(status, "boom").to_envelope())
    assert envelope == {"error": {"code": code, "message": "boom", "retryable": retryable}}


def test_tool_without_credentials_returns_not_configured():
    content, _ = asyncio.run(create_mcp_server().call_tool("unitrends_list_appliances", {}))
    assert json.loads(content[0].text)["error"]["code"] == "not_configured"
