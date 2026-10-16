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
"""Login, retries and tool request shapes against a mocked Unitrends appliance."""

import json

import httpx
import pytest

from unitrends_mcp import api_client
from unitrends_mcp.api_client import UnitrendsClient, UnitrendsCredentials, UnitrendsError
from unitrends_mcp.server import _credentials_var, create_mcp_server

BASE = "https://backup.example.com"
CREDS = UnitrendsCredentials(base_url=BASE, username="root", password="pw")
SYSTEMS = {"systems": [{"id": 1, "name": "UB-1", "local": True, "role": "Backup System"}, {"id": 7, "name": "UB-7", "local": False}]}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


def login_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": 32, "auth_token": "tok-1", "superuser": True})


class Recorder:
    """Mock transport: replies by "METHOD path" or path below /api/."""

    def __init__(self, routes=None, default=None):
        self.requests: list[httpx.Request] = []
        self.routes = {"login": login_ok} | (routes or {})
        self.default = default if default is not None else {"data": []}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/").strip("/")
        reply = self.routes.get(f"{request.method} {path}", self.routes.get(path, self.default))
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, path: str, method: str | None = None) -> list[httpx.Request]:
        return [
            r for r in self.requests
            if r.url.path.removeprefix("/api/").strip("/") == path and (method is None or r.method == method)
        ]


def body(request: httpx.Request):
    return json.loads(request.content) if request.content else None


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict, credentials: UnitrendsCredentials = CREDS):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda verify=True: http)
    ctx = _credentials_var.set(credentials)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _client(recorder: Recorder) -> UnitrendsClient:
    return UnitrendsClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


# ---- authentication --------------------------------------------------------


async def test_login_and_auth_token_header():
    rec = Recorder({"systems": SYSTEMS})
    await _client(rec).get("systems")
    login, call = rec.requests
    assert str(login.url) == f"{BASE}/api/login"
    assert body(login) == {"username": "root", "password": "pw"}
    assert call.headers["AuthToken"] == "tok-1"
    assert "Authorization" not in call.headers


async def test_401_logs_in_again_once():
    tokens = iter(["tok-1", "tok-2"])
    rec = Recorder(
        {
            "login": lambda r: httpx.Response(200, json={"auth_token": next(tokens)}),
            "systems": lambda r: httpx.Response(200 if r.headers["AuthToken"] == "tok-2" else 401, json=SYSTEMS),
        }
    )
    assert await _client(rec).get("systems") == SYSTEMS
    assert len(rec.calls("login")) == 2


async def test_bad_login_is_unauthorized_without_leaking_secrets(monkeypatch):
    rec = Recorder({"login": lambda r: httpx.Response(401, json={"result": [{"message": "Invalid"}]})})
    creds = UnitrendsCredentials(base_url=BASE, username="user-value-1", password="pw-value-2")
    result = await _call(monkeypatch, rec, "unitrends_list_appliances", {}, creds)
    assert result["error"]["code"] == "unauthorized"
    assert "user-value-1" not in json.dumps(result) and "pw-value-2" not in json.dumps(result)


async def test_wrong_url_explains_missing_api(monkeypatch):
    rec = Recorder({"login": lambda r: httpx.Response(404, text="Not Found")})
    result = await _call(monkeypatch, rec, "unitrends_list_appliances", {})
    assert result["error"]["code"] == "not_found"
    assert "X-Unitrends-Base-URL" in result["error"]["message"]


async def test_error_result_messages_are_surfaced():
    error = {"result": [{"code": 1345, "message": "The license could not be updated."}]}
    rec = Recorder({"systems": lambda r: httpx.Response(500, json=error)})
    with pytest.raises(UnitrendsError) as err:
        await _client(rec).get("systems")
    assert "license could not be updated" in err.value.message
    assert len(rec.calls("systems")) == 3  # GET retried twice


async def test_post_is_not_retried_on_5xx():
    rec = Recorder({"POST restore/full": lambda r: httpx.Response(500, json={})})
    with pytest.raises(UnitrendsError):
        await _client(rec).post("restore/full", {"backup_id": 1}, {"sid": 1})
    assert len(rec.calls("restore/full", "POST")) == 1


# ---- tools -----------------------------------------------------------------


async def test_list_appliances(monkeypatch):
    rec = Recorder({"systems": SYSTEMS})
    result = await _call(monkeypatch, rec, "unitrends_list_appliances", {})
    assert result["count"] == 2
    assert rec.calls("systems")[0].url.params["include_pending"] == "0"


async def test_get_appliance_merges_details(monkeypatch):
    details = {"appliance": [{"name": "UB-7", "version": "10.7", "memory": "64 GB"}], "timestamp": 1}
    rec = Recorder({"systems": SYSTEMS, "systems/details": details})
    result = await _call(monkeypatch, rec, "unitrends_get_appliance", {"applianceId": "7"})
    assert result["name"] == "UB-7" and result["details"]["memory"] == "64 GB"
    assert rec.calls("systems/details")[0].url.params["sid"] == "7"


async def test_get_unknown_appliance(monkeypatch):
    rec = Recorder({"systems": SYSTEMS, "systems/details": {}})
    result = await _call(monkeypatch, rec, "unitrends_get_appliance", {"applianceId": "99"})
    assert result["error"]["code"] == "not_found"


async def test_non_numeric_ids_are_rejected_before_calling(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "unitrends_get_asset", {"applianceId": "1", "assetId": "../systems"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_assets_and_asset(monkeypatch):
    rec = Recorder({"clients": {"clients": [{"id": 4, "name": "FS01"}]}, "clients/4": {"data": {"id": 4, "name": "FS01"}}})
    listing = await _call(monkeypatch, rec, "unitrends_list_assets", {"applianceId": "1"})
    one = await _call(monkeypatch, rec, "unitrends_get_asset", {"applianceId": "1", "assetId": "4"})
    assert listing["assets"] == [{"id": 4, "name": "FS01"}]
    assert one == {"id": 4, "name": "FS01"}
    assert rec.calls("clients")[0].url.params["sid"] == "1"


async def test_job_history_uses_date_window_and_flat_listing(monkeypatch):
    rec = Recorder({"jobs/history/backup": {"data": [{"id": 1}]}})
    args = {"since": "2026-06-01", "until": "2026-06-08T12:00:00Z", "type": "backup", "limit": 50}
    result = await _call(monkeypatch, rec, "unitrends_list_job_history", args)
    params = rec.calls("jobs/history/backup")[0].url.params
    assert (params["start_date"], params["end_date"]) == ("06/01/2026", "06/08/2026")
    assert (params["showFlat"], params["sort"], params["limit"]) == ("true", "desc", "50")
    assert result["count"] == 1 and result["has_more"] is False


async def test_bad_dates_are_rejected(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "unitrends_list_job_history", {"since": "last tuesday"})
    assert result["error"]["code"] == "invalid_argument"
    reversed_window = await _call(monkeypatch, rec, "unitrends_list_job_history", {"since": "2026-06-08", "until": "2026-06-01"})
    assert reversed_window["error"]["code"] == "invalid_argument"


async def test_running_jobs_by_type(monkeypatch):
    rec = Recorder({"jobs/active/restore": {"data": [{"id": 827, "percent_complete": 40}]}})
    result = await _call(monkeypatch, rec, "unitrends_list_running_jobs", {"type": "restore"})
    assert result == {"jobs": [{"id": 827, "percent_complete": 40}], "count": 1}


async def test_recovery_points_from_catalog(monkeypatch):
    catalog = {"catalog": [{"client_id": 4, "instance_id": 402, "backups": [{"id": 304}, {"id": 305}]}]}
    rec = Recorder({"backups/catalog": catalog})
    result = await _call(monkeypatch, rec, "unitrends_list_recovery_points", {"assetId": "4"})
    params = rec.calls("backups/catalog")[0].url.params
    assert (params["view"], params["cid"]) == ("instance", "4")
    assert result["recovery_points"] == 2


async def test_restore_without_confirm_is_only_a_preview(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "unitrends_queue_restore", {"recoveryPointId": "304"})
    assert result["status"] == "not_started"
    assert rec.requests == []


async def test_confirmed_restore_posts_to_local_system(monkeypatch):
    rec = Recorder({"systems": SYSTEMS, "POST restore/full": lambda r: httpx.Response(201, json={"result": [{"id": "827, 828"}]})})
    args = {"recoveryPointId": "304", "targetAssetId": "5", "targetPath": "C:\\Restore", "confirm": True}
    result = await _call(monkeypatch, rec, "unitrends_queue_restore", args)
    request = rec.calls("restore/full", "POST")[0]
    assert request.url.params["sid"] == "1"
    assert body(request) == {
        "backup_id": 304,
        "client_id": 5,
        "includes": [],
        "excludes": [],
        "before_cmd": "",
        "after_cmd": "",
        "target": {"directory": "C:\\Restore"},
    }
    assert result["status"] == "queued" and result["restoreIds"] == ["827", "828"]


async def test_restore_status_running_then_finished(monkeypatch):
    rec = Recorder({"jobs/active/restore": {"data": [{"id": 827, "percent_complete": 10}]}})
    running = await _call(monkeypatch, rec, "unitrends_get_restore_status", {"restoreId": "827"})
    assert running["state"] == "running"
    rec = Recorder({"jobs/active/restore": {"data": []}, "jobs/history/restore": {"data": [{"id": 828, "status": "Success"}]}})
    finished = await _call(monkeypatch, rec, "unitrends_get_restore_status", {"restoreId": "828"})
    assert finished["state"] == "finished"
    missing = await _call(monkeypatch, rec, "unitrends_get_restore_status", {"restoreId": "999"})
    assert missing["error"]["code"] == "not_found"


async def test_open_alerts(monkeypatch):
    rec = Recorder({"alerts/open/error": {"alerts": [{"id": 1, "severity": "error"}], "more": True}})
    result = await _call(monkeypatch, rec, "unitrends_list_alerts", {"severity": "error", "limit": 1})
    assert result == {"alerts": [{"id": 1, "severity": "error"}], "count": 1, "has_more": True}
    assert rec.calls("alerts/open/error")[0].url.params["max_items"] == "1"


async def test_success_rate_aggregates_days(monkeypatch):
    summary = {
        "days": [
            {"date": "Jun 07 2026", "day": [{"id": 1, "client_name": "FS01", "successes": 3, "failures": 1, "warnings": 0, "in_progress": 0},
                                            {"id": 2, "client_name": "DC01", "successes": 4, "failures": 0, "warnings": 0, "in_progress": 0}]},
            {"date": "Jun 08 2026", "day": [{"id": 1, "client_name": "FS01", "successes": 0, "failures": 2, "warnings": 1, "in_progress": 1}]},
        ]
    }
    rec = Recorder({"summary/status": summary})
    result = await _call(monkeypatch, rec, "unitrends_get_success_rate", {"since": "2026-06-07", "until": "2026-06-09"})
    params = rec.calls("summary/status")[0].url.params
    assert params["days"] == "2"
    assert result["totals"] == {"successes": 7, "warnings": 1, "failures": 3, "in_progress": 1, "success_rate": 72.7}
    assert result["assets"][0]["name"] == "FS01" and result["assets"][0]["success_rate"] == 57.1
    assert result["assets_with_failures"] == 1
