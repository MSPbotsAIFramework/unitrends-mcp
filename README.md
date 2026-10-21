# Unitrends MCP Server

An [MCP](https://modelcontextprotocol.io) server for the REST API of a
[Unitrends](https://www.unitrends.com) backup appliance (Recovery Series / Unitrends Backup).
It lets an AI agent read the systems an appliance manages, protected assets, running and
finished jobs, restorable backups, open alerts and backup success rates and, after explicit
confirmation, queue a restore.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: credentials arrive on every request as HTTP headers and are
  never stored; the appliance login token is obtained per request and discarded with it
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Unitrends-Base-URL` | Yes | string | none | https URL | Root URL of the appliance (a bare host name gets `https://`; a trailing `/api` is removed) | `https://backup.example.com` |
| `X-Unitrends-Username` | Yes | string | none | - | Appliance user name | `your-username` |
| `X-Unitrends-Password` | Yes | string | none | - | Password of that user | `your-password` |
| `X-Unitrends-Verify-TLS` | No | string | `true` | `true` / `false` (also `1` / `0`, `yes` / `no`) | Set `false` only for an appliance with a self-signed certificate | `false` |

Where to get them:

- Use (or create) a dedicated user in the appliance's user settings (menu names vary by
  appliance version). A role that can view systems, assets, jobs, backups and alerts covers
  the read tools; restores need a role that may run restores.
- The base URL is the address you open the appliance's web UI with. The appliance must be
  reachable over HTTPS from the environment that runs this server.
- Appliances ship with a self-signed certificate; either install a trusted certificate or send
  `X-Unitrends-Verify-TLS: false`.

How credentials are sent to Unitrends: the server calls `POST {base}/api/login` with the user
name and password, then sends every API call with the returned token in the `AuthToken`
header. A 401 triggers one fresh login. Values that look like unresolved templates (`${...}`)
are treated as missing.

A request to `/mcp` without the three required headers gets HTTP 401 naming the missing
ones. A non-https URL or an invalid `X-Unitrends-Verify-TLS` value does not block the request
(so `tools/list` works with any values); every tool call then returns an `invalid_argument`
error naming the header, and nothing is sent to the appliance. An invalid
`X-Unitrends-Verify-TLS` value never turns TLS verification off.

## Tools

All tools return a JSON string. `applianceId` is a system id from
`unitrends_list_appliances`; omitting it where it is optional covers every system the login
can see. Dates are ISO 8601 (`2026-06-01` or `2026-06-01T00:00:00Z`).

| Tool | Purpose | Parameters (required in **bold**) |
|---|---|---|
| `unitrends_list_appliances` | List the systems (appliances) this login manages: id, name, role, version, status, capacity | - |
| `unitrends_get_appliance` | One system's status, role, capacity and hardware details | **applianceId** |
| `unitrends_list_assets` | Protected assets (clients: servers, VMs, hosts) with OS, agent version and type | applianceId |
| `unitrends_get_asset` | One protected asset: OS, agent, applications | **applianceId**, **assetId** |
| `unitrends_list_running_jobs` | Jobs running or queued now, with percent complete | applianceId, type |
| `unitrends_list_job_history` | Finished jobs, newest first (default last 7 days) | since, until, applianceId, type, limit |
| `unitrends_list_recovery_points` | An asset's restorable backups: backup id, type, date, status, size, storage | **assetId**, applianceId, since, until |
| `unitrends_queue_restore` | Restore a backup onto a system (**write**, needs `confirm=true`) | **recoveryPointId**, targetAssetId, targetPath, applianceId, confirm |
| `unitrends_get_restore_status` | A restore job: running (percent complete) or finished (result) | **restoreId**, applianceId |
| `unitrends_list_alerts` | Open alerts, newest first | applianceId, severity (`notice`, `warning`, `error`), limit |
| `unitrends_get_success_rate` | Backup success rate for a period: totals and per-asset counts, failing assets first | since, until, applianceId |

`type` for jobs is one of `backup`, `restore`, `replication`, `archive`, `ir`, `flr`,
`certification`, `backup-copy`.

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "unauthorized", "message": "Unitrends rejected the username or password (login failed)", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`; messages from the appliance's `result` array are passed
through. Reads are retried with backoff on 429, 5xx and network errors; the restore request
is never retried.

## Run locally

```bash
uv sync
uv run python -m unitrends_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t unitrends-mcp:dev .
docker run --rm -p 8080:8080 unitrends-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials are never read
from the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Unitrends-Base-URL: https://backup.example.com" \
  -H "X-Unitrends-Username: your-username" \
  -H "X-Unitrends-Password: your-password" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# backup success rate for the last 7 days
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Unitrends-Base-URL: https://backup.example.com" \
  -H "X-Unitrends-Username: your-username" \
  -H "X-Unitrends-Password: your-password" \
  -H "X-Unitrends-Verify-TLS: false" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"unitrends_get_success_rate","arguments":{}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Built on the documented appliance API.** Every call uses the endpoints in the official
  Unitrends API documentation: `/api/login` with the `AuthToken` header, `/systems`,
  `/clients`, `/jobs/active`, `/jobs/history`, `/backups/catalog`, `/restore/full`,
  `/alerts/open` and `/summary/status`.
- **Tool count (11).** Tool names and required parameters are unchanged; some tools gained
  optional filters (`applianceId`, `type`, `limit`, `severity`).
- **Restore confirmation.** Older versions asked for confirmation with an interactive prompt,
  which a stateless gateway cannot relay, so restores could not run there. Now
  `unitrends_queue_restore` returns a preview unless called with `confirm=true`; the tool
  description tells the agent to set it only after the user approves that exact backup and
  target. It is the only write tool and is annotated destructive.
- **Result size exception (SOP 2.5).** `unitrends_list_job_history` and `unitrends_list_alerts`
  have no paging in the API, so they keep a maximum of 1000 (default 50); results are still cut
  to 20,000 characters with a truncation notice.
- **Date ranges** default to the last 7 days instead of prompting; job history honours the
  requested range.

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
