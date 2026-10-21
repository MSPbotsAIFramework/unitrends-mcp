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
"""Async client for the Unitrends appliance REST API."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ._json import error_envelope
from .config import API_PATH

logger = logging.getLogger("unitrends_mcp.api")

_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

# Two connection pools for the process: one that verifies TLS and one for
# appliances with self-signed certificates. Neither holds credentials: the
# AuthToken header is built per request from the caller's own login.
_shared_clients: dict[bool, httpx.AsyncClient] = {}


def _get_shared_http_client(verify_tls: bool = True) -> httpx.AsyncClient:
    client = _shared_clients.get(verify_tls)
    if client is None:
        client = httpx.AsyncClient(timeout=_TIMEOUT, verify=verify_tls)
        _shared_clients[verify_tls] = client
    return client


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class UnitrendsError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Unitrends API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)


@dataclass(frozen=True)
class UnitrendsCredentials:
    base_url: str
    username: str
    password: str
    verify_tls: bool = True
    # Set when a header value is malformed: tools then return invalid_argument
    # instead of calling Unitrends, while tools/list still works.
    header_error: str | None = None

    def __repr__(self) -> str:  # never leak secrets into logs or tracebacks
        return f"UnitrendsCredentials(base_url={self.base_url!r}, verify_tls={self.verify_tls}, secrets=<redacted>)"


class UnitrendsClient:
    """Unitrends API wrapper for one caller's credentials.

    The AuthToken from /api/login is obtained lazily and kept only on this
    instance, which lives for a single MCP request.
    """

    def __init__(self, credentials: UnitrendsCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.header_error = credentials.header_error
        self.base_url = credentials.base_url.rstrip("/")
        self.api_url = f"{self.base_url}{API_PATH}"
        self._token: str | None = None

    @property
    def verify_tls(self) -> bool:
        return self._credentials.verify_tls

    def _client(self) -> httpx.AsyncClient:
        return self._http or _get_shared_http_client(self._credentials.verify_tls)

    async def _login(self) -> str:
        if self.header_error:  # never send credentials to an unchecked base URL
            raise UnitrendsError(400, self.header_error)
        creds = self._credentials
        try:
            resp = await self._client().post(
                f"{self.api_url}/login",
                json={"username": creds.username, "password": creds.password},
                headers={"Accept": "application/json"},
            )
        except httpx.ConnectError as e:
            hint = " (if the appliance uses a self-signed certificate, send X-Unitrends-Verify-TLS: false)" if "CERTIFICATE" in str(e).upper() else ""
            raise UnitrendsError(0, f"Could not connect to the Unitrends appliance{hint}") from e
        except httpx.RequestError as e:
            raise UnitrendsError(0, f"Network error logging in to Unitrends: {type(e).__name__}") from e
        logger.info("unitrends_api method=POST path=login status=%s", resp.status_code)
        if resp.status_code in (400, 401, 403):
            raise UnitrendsError(401, "Unitrends rejected the username or password (login failed)")
        if resp.status_code == 404:
            raise UnitrendsError(404, "No Unitrends API at this URL (/api/login not found); check X-Unitrends-Base-URL")
        if resp.status_code >= 400:
            raise UnitrendsError(resp.status_code, f"Unitrends login returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        token = data.get("auth_token") if isinstance(data, dict) else None
        if not token:
            raise UnitrendsError(502, "Unitrends login response had no auth_token")
        return token

    async def _auth_token(self, renew: bool = False) -> str:
        if renew or not self._token:
            self._token = await self._login()
        return self._token

    async def verify(self) -> None:
        """Log in, proving the URL and credentials work."""
        await self._auth_token()

    # ---- requests -------------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: Any, params: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", path, params=params, body=body)

    async def request(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        """Call ``{base}/api/{path}``; on 401 logs in again once and retries."""
        if self.header_error:
            raise UnitrendsError(400, self.header_error)
        resp = await self._send(method, path, params, body, await self._auth_token())
        if resp.status_code == 401:
            resp = await self._send(method, path, params, body, await self._auth_token(renew=True))
        if resp.status_code >= 400:
            raise UnitrendsError(resp.status_code, _error_message(resp, path))
        return _parse_body(resp)

    async def _send(self, method: str, path: str, params: dict[str, Any] | None, body: Any, token: str) -> httpx.Response:
        url = f"{self.api_url}/{path.strip('/')}"
        query = {k: _param_value(v) for k, v in (params or {}).items() if v is not None}
        headers = {"AuthToken": token, "Accept": "application/json"}
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            error: UnitrendsError | None = None
            try:
                resp = await self._client().request(method, url, params=query, json=body, headers=headers)
                status = resp.status_code
            except httpx.TimeoutException:
                status, error = 0, UnitrendsError(0, f"Timed out calling Unitrends ({method} {path})")
            except httpx.RequestError as e:
                status = 0
                error = UnitrendsError(0, f"Network error calling Unitrends ({method} {path}): {type(e).__name__}")

            logger.info(
                "unitrends_api method=%s path=%s status=%s ms=%d attempt=%d",
                method, path, status, (time.monotonic() - t0) * 1000, attempt,
            )

            # 429 is refused before processing, so any method may retry it; other
            # failures are retried only for reads, so a write never runs twice.
            retryable = status == 429 or (method == "GET" and (status == 0 or status >= 500))
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if error is not None:
                raise error
            assert resp is not None
            return resp


def _param_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt) * 2, _MAX_BACKOFF_SECONDS)


def _parse_body(resp: httpx.Response) -> Any:
    if not resp.content or not resp.content.strip():
        return None
    try:
        return resp.json()
    except ValueError:
        return {"raw_response": resp.text[:2000]}


def _error_message(resp: httpx.Response, path: str) -> str:
    """Unitrends puts error details in a "result" array of {code, message}."""
    fallback = f"Unitrends returned HTTP {resp.status_code} for {path}"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        return f"{fallback}: {text[:300]}" if text else fallback
    messages: list[str] = []
    if isinstance(payload, dict):
        for item in payload.get("result") or []:
            if isinstance(item, dict) and item.get("message"):
                messages.append(str(item["message"]))
        for key in ("message", "error"):
            if not messages and isinstance(payload.get(key), str):
                messages.append(payload[key])
    return f"{fallback}: {' - '.join(messages)[:500]}" if messages else fallback
