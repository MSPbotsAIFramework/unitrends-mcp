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
"""Entry point: serve /health and the MCP Streamable HTTP endpoint at /mcp."""

import logging

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .config import Settings, get_settings
from .server import GatewayCredentialsMiddleware, create_mcp_server, request_id_var


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(
        logging.Formatter(
            "ts=%(asctime)s level=%(levelname)s logger=%(name)s request_id=%(request_id)s %(message)s"
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def build_http_app(mcp: FastMCP) -> Starlette:
    async def health(_: Request) -> JSONResponse:
        # Purely local: never depends on the Unitrends appliance being reachable.
        return JSONResponse({"status": "ok"})

    mcp_app = mcp.streamable_http_app()

    # Mount() does not run a sub-app's lifespan, so hand the MCP app's lifespan
    # to the outer app; otherwise /mcp fails with "Task group is not initialized".
    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/", app=GatewayCredentialsMiddleware(mcp_app)),
        ],
        lifespan=lambda app: mcp_app.router.lifespan_context(app),
    )


def main() -> None:
    settings: Settings = get_settings()
    configure_logging(settings.log_level)
    app = build_http_app(create_mcp_server(settings))

    import uvicorn

    logging.getLogger("unitrends_mcp").info(
        "listening host=%s port=%s path=/mcp", settings.mcp_http_host, settings.mcp_http_port
    )
    uvicorn.run(app, host=settings.mcp_http_host, port=settings.mcp_http_port, log_config=None)


if __name__ == "__main__":
    main()
