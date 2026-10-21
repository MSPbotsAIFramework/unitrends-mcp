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
"""Parameter types stay compatible with the previous version agents already call.

contract_types.json holds the previous version's parameter types. Every value an
old caller could send must still validate: same type (or one pydantic coerces,
such as a whole number for an integer), and a free-text parameter must still
accept any string even where we now advertise a list of known values.
"""

import asyncio
import json
import pathlib

import pytest

from unitrends_mcp.server import create_mcp_server

BASELINE = json.loads((pathlib.Path(__file__).parent / "contract_types.json").read_text(encoding="utf-8"))
ACCEPTS = {
    "string": {"string", "integer", "number"},  # numeric strings are coerced
    "number": {"number", "integer"},
    "integer": {"integer", "number"},
    "boolean": {"boolean"},
    "array": {"array"},
    "object": {"object"},
}


def _types(prop: dict, defs: dict) -> tuple[set[str], bool]:
    """Types a property accepts, and whether it accepts any string ($ref resolved through $defs)."""
    kinds: set[str] = set()
    free_string = False
    for option in prop.get("anyOf") or prop.get("oneOf") or [prop]:
        if not isinstance(option, dict):
            continue
        if "$ref" in option:
            option = defs.get(option["$ref"].rsplit("/", 1)[-1], {})
        kind = option.get("type")
        restricted = "enum" in option or "const" in option
        for k in kind if isinstance(kind, list) else ([kind] if kind else (["string"] if restricted else [])):
            kinds.add(k)
            if k == "string" and not restricted:
                free_string = True
    return kinds - {"null"}, free_string


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


CASES = [(tool, name, info) for tool, params in BASELINE.items() for name, info in params.items() if info["type"]]


@pytest.mark.parametrize("tool,name,info", CASES, ids=[f"{t}.{n}" for t, n, _ in CASES])
def test_parameter_still_accepts_previous_types(tools, tool, name, info):
    if tool not in tools:
        pytest.skip("tool checked by the name contract")
    prop = (tools[tool].inputSchema.get("properties") or {}).get(name)
    if prop is None:
        pytest.skip("parameter checked by the name contract")
    kinds, free_string = _types(prop, tools[tool].inputSchema.get("$defs") or {})
    if not kinds:
        return  # untyped: accepts anything
    for old in info["type"]:
        assert ACCEPTS.get(old, {old}) & kinds, f"{tool}.{name}: previous type {old} no longer accepted ({sorted(kinds)})"
    if "string" in info["type"] and not info["enum"] and "string" in kinds:
        assert free_string, f"{tool}.{name}: previous free-text value is now limited to a fixed list"
