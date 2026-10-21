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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import UnitrendsClient, UnitrendsCredentials
from .config import Settings

HEADER_BASE_URL = "X-Unitrends-Base-URL"
HEADER_USERNAME = "X-Unitrends-Username"
HEADER_PASSWORD = "X-Unitrends-Password"
HEADER_VERIFY_TLS = "X-Unitrends-Verify-TLS"
REQUIRED_HEADERS = [HEADER_BASE_URL, HEADER_USERNAME, HEADER_PASSWORD]

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")
_FALSE = re.compile(r"^(0|false|no|off)$", re.IGNORECASE)
_TRUE = re.compile(r"^(1|true|yes|on)$", re.IGNORECASE)
_API_SUFFIX = re.compile(r"/api/?$", re.IGNORECASE)
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}
# Stand-in for a rejected base URL; the .invalid TLD never resolves, and the
# client refuses every request anyway while header_error is set.
_UNUSABLE_BASE_URL = "https://base-url.invalid"

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[UnitrendsCredentials | None] = contextvars.ContextVar(
    "unitrends_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("unitrends_request_id", default="-")

INSTRUCTIONS = (
    "Unitrends is an MSP backup appliance (Recovery Series / Unitrends Backup). "
    "One appliance can manage other systems (replication targets, managed "
    "appliances); each system has a numeric id used as applianceId.\n"
    "Usage:\n"
    "- unitrends_list_appliances: systems this login manages and their ids.\n"
    "- unitrends_list_assets(applianceId): protected clients (servers, VMs); "
    "unitrends_get_asset for one.\n"
    "- Backup health: unitrends_get_success_rate(since, until) for success / "
    "failure counts per asset, unitrends_list_job_history for job detail, "
    "unitrends_list_running_jobs for jobs in progress, unitrends_list_alerts "
    "for open alerts.\n"
    "- Restore: unitrends_list_recovery_points(assetId) gives backup ids; "
    "unitrends_queue_restore first returns a preview and only starts the "
    "restore when called again with confirm=true; follow it with "
    "unitrends_get_restore_status.\n"
    "A restore writes data back onto a live system and can overwrite files: "
    "call unitrends_queue_restore with confirm=true only after the user has "
    "explicitly approved that exact backup and target."
)


def get_client_from_context() -> UnitrendsClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return UnitrendsClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


def normalize_base_url(value: str) -> str | None:
    """https root of the appliance (no trailing /api), or None when unusable."""
    text = value.strip()
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if not parts.hostname:
        return None
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS):
        return None
    path = _API_SUFFIX.sub("", parts.path.rstrip("/"))
    return f"{parts.scheme}://{parts.netloc}{path}".rstrip("/")


class GatewayCredentialsMiddleware:
    """Reads the Unitrends headers on /mcp requests into the request context.

    Needs X-Unitrends-Base-URL, X-Unitrends-Username and X-Unitrends-Password;
    otherwise 401. A malformed base URL or Verify-TLS value does not block the
    request (tools/list must work with any values); tools report it instead and
    never contact the appliance. A malformed Verify-TLS keeps TLS verification on.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        def header(name: str) -> str | None:
            return _clean(request.headers.get(name))

        values = {name: header(name) for name in REQUIRED_HEADERS}
        missing = [name for name, value in values.items() if not value]
        if missing:
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s): {', '.join(missing)}",
                    "required_headers": REQUIRED_HEADERS,
                    "optional_headers": [HEADER_VERIFY_TLS],
                },
                status_code=401,
            )(scope, receive, send)
            return

        base_url = normalize_base_url(values[HEADER_BASE_URL] or "")
        verify_raw = header(HEADER_VERIFY_TLS)
        errors: list[str] = []
        if base_url is None:
            errors.append(f"{HEADER_BASE_URL} must be an https URL such as https://backup.example.com")
            base_url = _UNUSABLE_BASE_URL
        if verify_raw and not (_TRUE.match(verify_raw) or _FALSE.match(verify_raw)):
            errors.append(f"{HEADER_VERIFY_TLS} must be true or false")

        credentials = UnitrendsCredentials(
            base_url=base_url,
            username=values[HEADER_USERNAME] or "",
            password=values[HEADER_PASSWORD] or "",
            # Only an explicit false turns verification off; a malformed value keeps it on.
            verify_tls=not (verify_raw and _FALSE.match(verify_raw)),
            header_error="; ".join(errors) or None,
        )
        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(credentials)
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = FastMCP(
        name="unitrends-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], UnitrendsClient | None] = get_client_from_context

    from .tools import appliances, jobs, monitoring, restores

    for module in (appliances, jobs, restores, monitoring):
        module.register(mcp, client_factory)

    return mcp
