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
"""Invalid tool arguments reported as the standard invalid_argument envelope."""

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from ._json import error_envelope


class EnvelopeFastMCP(FastMCP):
    """FastMCP whose argument-validation errors use the error envelope like every other tool error.

    FastMCP checks arguments (required, type, range) before the tool runs and otherwise answers
    with its own "Error executing tool ..." text.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as error:
            if not isinstance(error.__cause__, ValidationError):
                raise
            problems = "; ".join(_describe(e) for e in error.__cause__.errors())
            envelope = error_envelope("invalid_argument", f"Invalid arguments for {name}: {problems}.", False)
            return self._tool_manager.get_tool(name).fn_metadata.convert_result(envelope)


def _describe(error: Any) -> str:
    # Names the argument and what is wrong with it; the rejected value itself is not echoed.
    where = ".".join(str(part) for part in error.get("loc", ())) or "arguments"
    return f"{where}: {error.get('msg', 'invalid value')}"
