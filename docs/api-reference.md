# API Reference

The subnet-acl-provision-api exposes a REST API via FastAPI. By default it listens on host port `8875` (container port `8000`).

Long-running operations (register, rebuild, remove) are **asynchronous by default** —
they return a `task_id` immediately and the work runs in a background thread pool.
Poll `GET /tasks/{task_id}` for progress.  To block until completion (legacy behaviour),
add `?sync=true` to any mutable endpoint.

---

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `POST` | `/users` | Register a user and start their containers (async → `task_id`) |
| `GET` | `/users` | Status of all registered users |
| `GET` | `/users/{user_name}` | Status of one user |
| `DELETE` | `/users/{user_name}/services/{service_name}/{label}` | Stop and deregister a service (async → `task_id`) |
| `POST` | `/users/{user_name}/services/{service_name}/{label}/rebuild` | Rebuild and restart containers (async → `task_id`) |
| `POST` | `/users/{user_name}/services/{service_name}/{label}/up` | Start a service's containers |
| `POST` | `/users/{user_name}/services/{service_name}/{label}/down` | Stop a service's containers |
| `PUT` | `/users/{user_name}/services/{service_name}/{label}/password` | Change a user's password |
| `GET` | `/users/{user_name}/services/{service_name}/{label}/containers/{container}/logs` | Get container logs |
| `GET` | `/tasks` | List all tasks in the pool |
| `GET` | `/tasks/{task_id}` | Query task status / result |
| `GET` | `/tasks/{task_id}/log` | SSE stream of build log output |
| `DELETE` | `/tasks/{task_id}` | Cancel a pending or running task |
| `GET` | `/docker/ps` | List all Docker containers |
| `GET` | `/docker/stats` | Per-container resource stats snapshot |
| `GET` | `/docker/info` | Docker host info (container counts) |
| `GET` | `/host/stats` | Host-level CPU/memory/disk usage |
| `GET` | `/docker/container/{container}/exists` | Check if container exists |
| `GET` | `/docker/container/{container}/running` | Check if container is running |
| `POST` | `/docker/network/{network}/connect/{container}` | Connect container to network |
| `POST` | `/docker/nginx/reload` | Reload subnet-acl-nginx |
| `GET` | `/nginx/connections` | Nginx connection state (networks, confs, upstreams) |
| `POST` | `/nginx/reconnect-all` | Reconnect nginx to all user networks and reload |
| `POST` | `/reconcile` | Run live nginx upstream reconciliation |
| `GET` | `/reconcile/status` | Live nginx state snapshot (networks, containers, confs) |
| `GET` | `/nginx-state` | Same as `/reconcile/status` — live state snapshot |
| `GET` | `/container-stats` | Container-level statistics (registry-scoped) |
| `GET` | `/service-stats` | Service-level health summary (registry-scoped) |
| `GET` | `/ssl-certs` | List available SSL certificate domains |
| `POST` | `/ssl-certs` | Upload SSL certificates for a domain |
| `POST` | `/ssl-certs/{domain}/refresh` | Refresh certs from original source path |
| `DELETE` | `/ssl-certs/{domain}` | Delete SSL certificates for a domain |
| `GET` | `/services/{service_name}/check-missing-files` | Check which essential deployment files are missing (`?recipe_path=` checks a recipe subdirectory) |
| `GET` | `/subnet-pool` | Subnet pool usage statistics (enabled/pools/overall/allocations/headroom) |

---

## Async vs Sync

All three mutable endpoints (`POST /users`, `DELETE /users/...`, `POST .../rebuild`) behave
differently depending on the `?sync` query parameter:

| Mode | Query | HTTP status | Response body |
|---|---|---|---|
| **Async** (default) | _(none)_ | `202 Accepted` | `{"task_id": "...", "status": "pending", "type": "...", "message": "..."}` |
| **Sync** | `?sync=true` | `201` / `200` | legacy response (`{"status": "registered", ...}` etc.) |

In async mode, errors that can be detected before queuing (validation, not-found, permission)
return immediately as `4xx`.  Runtime errors (docker build failures, etc.) are stored in the
task's `error` field and surfaced when you poll `GET /tasks/{task_id}`.

---

## `GET /health`

Liveness probe — does not touch Docker.

**Response `200`**
```json
{ "status": "ok" }
```

---

## `POST /users` — Register

Registers a user and starts their isolated service containers.

| Mode | Method | Status | Response |
|---|---|---|---|
| Async (default) | `POST /users` | `202` | `{"task_id": "...", "status": "pending"}` |
| Sync | `POST /users?sync=true` | `202` | `{"status": "registered", "entry": {...}, "copied_env": "..."}` |

**Request body**

| Field | Type | Required | Description |
|---|---|---|---|
| `user_name` | string | ✓ | Alphanumeric + underscore + hyphen |
| `service_name` | string | ✓ | Alphanumeric + underscore + hyphen |
| `project_root` | string | — | Base directory for this service. Accepts a **bare name** (`"myapp"`), a relative path, or an absolute path. A bare name (no `/`, doesn't exist as a dir) resolves to `SOURCE_PROJECTS_DIR/myapp` — which is `$PROVISION_DIR/source_projects/myapp` by default. Equivalent to `-pr` in the CLI. Returns `404` if the resolved directory does not exist. |
| `compose_file_path` | string | † | Filename (when `project_root` set) or absolute path inside the container to a **plain** `docker-compose.yml`; auto-converted to a `.j2` template on every registration |
| `compose_template_path` | string | † | Filename (when `project_root` set) or absolute path inside the container to an existing `.j2` compose template |
| `nginx_conf_file_path` | string | — | Filename (when `project_root` set) or absolute path inside the container to a **plain** nginx conf; auto-converted to a `.j2` template |
| `nginx_conf_template_path` | string | — | Filename (when `project_root` set) or absolute path inside the container to an existing `.j2` nginx conf template |
| `env_file_path` | string | — | Filename (when `project_root` set) or absolute path to a `.env` file. Copied as `.env.{user_name}.{label}` next to the generated compose file. Any `env_file: .env` directives in service definitions are automatically replaced with this per-user file name. |
| `label` | string | — | Digits only; default `"0"` |
| `domain` | string | — | Domain for nginx `server_name`; default `"localhost"` |
| `passwd` | string | — | Plain-text password; default `"123456"`. Hashed with bcrypt before storage. Pass `""` to disable auth entirely (no `.htpasswd` written, `auth_basic` lines stripped from nginx conf) |
| `volumes` | object | — | `{ "template_vol_key": "/host/path", ... }` |
| `build_args` | object | — | `{ "HTTP_PROXY": "http://proxy:8080", ... }` — passed as `--build-arg` to `docker compose build` (run before `compose up` when provided). Stored in registry for future rebuilds. |
| `https` | bool | — | Enable HTTPS (default `false`). Requires `fullchain` and `privkey`. |
| `fullchain` | string | — | Path or bare filename to the certificate file. Full path → copied to `$SSL_DIR/{domain}/fullchain.pem`. Bare filename → used directly from `$SSL_DIR/{domain}/`. |
| `privkey` | string | — | Path or bare filename to the private key file. Same resolution rules as `fullchain`. |

> † Exactly one of `compose_file_path` or `compose_template_path` must be provided.

**Example — async (default)**

```bash
curl -X POST http://localhost:8875/users \
  -H 'Content-Type: application/json' \
  -d '{
    "user_name": "alice",
    "service_name": "myapp",
    "project_root": "myapp",
    "compose_file_path": "docker-compose.yml",
    "domain": "example.com",
    "passwd": "secret"
  }'
```

**Response `202`**
```json
{
  "task_id": "a1b2c3d4e5f6",
  "status": "pending",
  "type": "register",
  "message": "Registration queued.  Poll GET /tasks/a1b2c3d4e5f6 for status."
}
```

**Example — sync (blocking)**

```bash
curl -X POST "http://localhost:8875/users?sync=true" \
  -H 'Content-Type: application/json' \
  -d '{...}'
```

**Response `201` (sync only)**
```json
{
  "status": "registered",
  "entry": {
    "user_name": "alice",
    "service_name": "myapp",
    "label": "0",
    "network_name": "myapp-user_alice-0",
    "compose_file_path": "/srv/provision_subnet_acl/source_projects/myapp/docker-compose.user-alice.0.yml",
    "nginx_conf_path": null,
    "htpasswd_path": null,
    "env_file_path": "/srv/provision_subnet_acl/source_projects/myapp/.env.alice.0",
    "volumes": { "app_data": "/srv/provision_subnet_acl/user-data/alice/app" },
    "subnet": "100.96.0.0/29",
    "gateway": "100.96.0.1"
  },
  "volume_warnings": { "missing": [], "extra": [] },
  "copied_env": "/srv/provision_subnet_acl/source_projects/myapp/.env.alice.0"
}
```

**Error codes (immediate — both modes)**

| Code | Cause |
|---|---|
| `404` | Template/env file not found, or bare `project_root` not found under `SOURCE_PROJECTS_DIR` |
| `422` | Validation error on `user_name`, `service_name`, or `label` format |

**Error codes (sync only)**

| Code | Cause |
|---|---|
| `409` | The `user_name` + `service_name` + `label` combination is already registered |
| `500` | `docker compose up` failed; error message includes stderr output |

**Example — HTTPS registration**

```bash
# Full path — certs are copied to $SSL_DIR/example.com/
curl -X POST "http://localhost:8875/users?sync=true" \
  -H 'Content-Type: application/json' \
  -d '{
    "user_name": "alice",
    "service_name": "myapp",
    "project_root": "myapp",
    "compose_file_path": "docker-compose.yml",
    "nginx_conf_file_path": "nginx.conf",
    "domain": "example.com",
    "https": true,
    "fullchain": "/etc/letsencrypt/live/example.com/fullchain.pem",
    "privkey": "/etc/letsencrypt/live/example.com/privkey.pem"
  }'

# Bare filename — certs already in $SSL_DIR/example.com/
curl -X POST "http://localhost:8875/users?sync=true" \
  -H 'Content-Type: application/json' \
  -d '{
    "user_name": "alice",
    "service_name": "myapp",
    "project_root": "myapp",
    "compose_file_path": "docker-compose.yml",
    "nginx_conf_file_path": "nginx.conf",
    "domain": "example.com",
    "https": true,
    "fullchain": "fullchain.pem",
    "privkey": "privkey.pem"
  }'
```

> In **async mode**, duplicate-registration and runtime errors appear in the task's `error` field
> (poll `GET /tasks/{task_id}`) rather than as HTTP error responses.

---

## `DELETE /users/{user_name}/services/{service_name}/{label}` — Remove a Service

Runs `docker compose down` then removes **one** registry entry — the specific user + service + label combination. Other services registered by the same user are not affected.

| Mode | Method | Status | Response |
|---|---|---|---|
| Async (default) | `DELETE /users/...` | `202` | `{"task_id": "...", "status": "pending"}` |
| Sync | `DELETE /users/...?sync=true` | `200` | `{"status": "removed", ...}` |

**Example — async**
```bash
curl -X DELETE http://localhost:8875/users/alice/services/myapp/0
```

**Response `202`**
```json
{
  "task_id": "b2c3d4e5f6a7",
  "status": "pending",
  "type": "remove",
  "message": "Removal queued.  Poll GET /tasks/b2c3d4e5f6a7 for status."
}
```

**Response `200` (sync only)**
```json
{ "status": "removed", "user_name": "alice", "service_name": "myapp", "label": "0" }
```

---

## `POST /users/{user_name}/services/{service_name}/{label}/rebuild`

Runs `docker compose build` then `docker compose up -d`.

| Mode | Method | Status | Response |
|---|---|---|---|
| Async (default) | `POST .../rebuild` | `202` | `{"task_id": "...", "status": "pending"}` |
| Sync | `POST .../rebuild?sync=true` | `200` | `{"status": "rebuilt", ...}` |

**Request body** (optional)

| Field | Type | Default | Description |
|---|---|---|---|
| `no_cache` | bool | `false` | Pass `--no-cache` to `docker compose build` |
| `build_args` | object | — | `{ "HTTP_PROXY": "http://proxy:8080", ... }` — passed as `--build-arg` to `docker compose build`. Overrides registry-stored values when provided. |

**Example — async**
```bash
curl -X POST http://localhost:8875/users/alice/services/myapp/0/rebuild \
  -H 'Content-Type: application/json' \
  -d '{"no_cache": true, "build_args": {"HTTP_PROXY": "http://proxy:8080"}}'
```

**Response `202`**
```json
{
  "task_id": "c3d4e5f6a7b8",
  "status": "pending",
  "type": "rebuild",
  "message": "Rebuild queued.  Poll GET /tasks/c3d4e5f6a7b8 for status."
}
```

**Response `200` (sync only)**
```json
{ "status": "rebuilt", "user_name": "alice", "service_name": "myapp", "label": "0" }
```

---

## `GET /tasks` — List All Tasks

Returns all tasks in the pool, newest first.  Completed/failed/cancelled tasks are
auto-cleaned after 1 hour.

**Response `200`**
```json
{
  "count": 2,
  "tasks": [
    {
      "task_id": "c3d4e5f6a7b8",
      "type": "rebuild",
      "status": "running",
      "created_at": 1717800000.0,
      "updated_at": 1717800001.0,
      "result": null,
      "error": null
    },
    {
      "task_id": "a1b2c3d4e5f6",
      "type": "register",
      "status": "completed",
      "created_at": 1717799900.0,
      "updated_at": 1717799905.0,
      "result": {"status": "registered", "entry": {...}},
      "error": null
    }
  ]
}
```

---

## `GET /tasks/{task_id}` — Query Task

Poll for task progress.  Task statuses: `pending` → `running` → `completed` | `failed` | `cancelled`.

**Response `200`**
```json
{
  "task_id": "a1b2c3d4e5f6",
  "type": "register",
  "status": "completed",
  "created_at": 1717799900.0,
  "updated_at": 1717799905.0,
  "result": {"status": "registered", "entry": {...}},
  "error": null
}
```

**Error codes**

| Code | Cause |
|---|---|
| `404` | Task not found (never existed or cleaned up) |

---

## `DELETE /tasks/{task_id}` — Cancel Task

Cancels a pending or running task.  Already-completed tasks return `409`.

**Response `200`**
```json
{ "task_id": "a1b2c3d4e5f6", "status": "cancelled" }
```

**Error codes**

| Code | Cause |
|---|---|
| `404` | Task not found |
| `409` | Task already in terminal state (`completed` / `failed` / `cancelled`) |

---

## `GET /users` — All Users Status

Returns the health of every registered user's services.

**Response `200`** — see [Status Response Schema](#status-response-schema).

---

## `GET /users/{user_name}` — Single User Status

**Error codes**

| Code | Cause |
|---|---|
| `404` | No registrations found for that user |

**Response `200`** — see [Status Response Schema](#status-response-schema).

---

## Status Response Schema

```
GET /users/alice

{
  "user_status": [
    {
      "user_name": "alice",
      "summary": {
        "expected_services_#": 1,
        "healthy_services_#": 1,
        "unhealthy_services_#": 0
      },
      "healthy_services": [
        {
          "service_name": "myapp",
          "label": "0",
          "compose_file_path": "...",
          "container_names": ["myapp-user_alice-0-web"],
          "healthy_containers":   { "myapp-user_alice-0-web": "Up 3 hours" },
          "unhealthy_containers": {},
          "missing_containers":   {},
          "volumes": {},
          "subnet": "100.96.0.0/29"
        }
      ],
      "unhealthy_services": [],
      "missing_services": []
    }
  ]
}
```

A service is **healthy** when all containers declared in its compose file are running with status `Up`.
A service is **missing** when its compose file does not exist (e.g. was deleted externally).

---

## `POST /users/{user_name}/services/{service_name}/{label}/up` — Start Service

Starts a stopped service's containers (`docker compose up -d`).

**Response `200`**
```json
{ "message": "Service started.", "status": "up" }
```

**Error codes**

| Code | Cause |
|---|---|
| `404` | No registration found, or compose file missing |
| `500` | `docker compose up` failed |

---

## `POST /users/{user_name}/services/{service_name}/{label}/down` — Stop Service

Stops a service's containers without removing them (`docker compose stop`).

**Response `200`**
```json
{ "message": "Service stopped.", "status": "down" }
```

**Error codes**

| Code | Cause |
|---|---|
| `404` | No registration found, or compose file missing |
| `500` | `docker compose stop` failed |

---

## `PUT /users/{user_name}/services/{service_name}/{label}/password` — Change Password

Re-hashes the password, updates the `.htpasswd` file and registry, then reloads nginx.

**Request body**
```json
{ "passwd": "newsecret" }
```

**Response `200`**
```json
{
  "message": "Password updated. Nginx reloaded.",
  "user_name": "alice",
  "service_name": "myapp",
  "label": "0"
}
```

**Error codes**

| Code | Cause |
|---|---|
| `404` | No registration found, or htpasswd file missing |

---

## `GET /users/{user_name}/services/{service_name}/{label}/containers/{container}/logs` — Container Logs

Returns the last N lines of a container's logs. The `{container}` path parameter is the
**short service name** from the compose file (e.g., `web`, `db`), not the full container name.

**Query parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tail` | int | `100` | Number of log lines to return |

**Response `200`**
```json
{
  "container": "myapp-user_alice-0-web",
  "tail": 100,
  "logs": ["line1", "line2", "..."]
}
```

**Error codes**

| Code | Cause |
|---|---|
| `404` | No registration found, or container does not exist |

---

## `GET /tasks/{task_id}/log` — SSE Build Log Streaming

Streams the per-task build log via Server-Sent Events. Each task writes its own
isolated log file at `$TASK_LOG_DIR/task-{task_id}.log`. The SSE endpoint reads
from that file and streams lines as `data:` events.

Used by the dashboard to show real-time progress during register/rebuild/remove tasks.

**Query parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `tail` | int | `200` | Number of recent lines to send first |
| `follow` | bool | `true` | Keep streaming new lines (set `false` for one-shot) |

**Response** — `Content-Type: text/event-stream`

```
data: + docker compose -f ... up -d
data: + docker network connect myapp-user_alice-0 subnet-acl-nginx
data: + docker exec subnet-acl-nginx nginx -s reload
event: done
data: {}
```

Per-task log files are stored in `$TASK_LOG_DIR` (default: `$GENERATED_DIR/task_logs`).
Finished tasks are cleaned up after `TASK_TTL_SECONDS` (default 7 days), along with
their log files. At most `TASK_MAX_COUNT` tasks are retained (default 1000); the
oldest are evicted when the limit is exceeded.

**Environment variables**

| Variable | Default | Description |
|---|---|---|
| `TASK_LOG_DIR` | `$GENERATED_DIR/task_logs` | Directory for per-task `.log` files |
| `TASK_TTL_SECONDS` | `604800` (7 days) | How long finished tasks + logs are kept |
| `TASK_MAX_COUNT` | `1000` | Maximum number of tasks retained in memory |

---

## `GET /docker/ps` — List Containers

Returns all Docker containers (`docker ps -a`).

**Response `200`**
```json
[
  { "name": "subnet-acl-nginx", "status": "Up 3 hours", "image": "nginx:alpine" },
  { "name": "myapp-user_alice-0-web", "status": "Up 2 hours", "image": "myapp:latest" }
]
```

---

## `GET /docker/stats` — Container Resource Stats

Returns a snapshot of per-container CPU/memory usage (`docker stats --no-stream`).

**Response `200`**
```json
[
  { "name": "subnet-acl-nginx", "cpu": "0.05%", "mem": "10.5MiB / 1.94GiB" }
]
```

---

## `GET /docker/info` — Docker Host Info

Returns container counts from `docker info`.

**Response `200`**
```json
{
  "containers_total": 10,
  "containers_running": 3,
  "containers_paused": 1,
  "containers_stopped": 6
}
```

---

## `GET /host/stats` — Host Resource Usage

Returns host-level CPU, memory, and disk usage (reads `/proc/meminfo`, `/proc/stat`, and `shutil.disk_usage`).

**Response `200`**
```json
{
  "cpu_percent": 12.3,
  "mem_percent": 45.6,
  "mem_total_kb": 8192000,
  "mem_used_kb": 3735552,
  "disk_percent": 32.1,
  "disk_total_gb": 100.0,
  "disk_free_gb": 67.9
}
```

---

## Reconciliation Helpers

These endpoints are called by the provision-gateway to reconcile state.

### `GET /docker/container/{container}/exists`

```json
{ "exists": true }
```

### `GET /docker/container/{container}/running`

```json
{ "running": true }
```

### `POST /docker/network/{network}/connect/{container}`

```json
{ "connected": true }
```

### `POST /docker/nginx/reload`

Reloads subnet-acl-nginx (default) or a named container.

**Query parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `container` | string | `subnet-acl-nginx` | Nginx container to reload |

**Response `200`**
```json
{ "reloaded": true }
```

---

## `GET /nginx/connections` — Nginx Connection State

Returns the current nginx routing state: networks nginx is connected to,
all generated `.nginx.conf` files, and parsed upstreams from each conf.

**Response `200`**
```json
{
  "nginx_container": "subnet-acl-nginx",
  "connected_networks": ["myapp-user_alice-0", "myapp-user_bob-0"],
  "conf_files": ["myapp.user-alice.0.nginx.conf", "myapp.user-bob.0.nginx.conf"],
  "upstreams": [
    {
      "conf_file": "myapp.user-alice.0.nginx.conf",
      "server_name": "myapp-alice-0.localhost",
      "proxy_pass": "http://myapp-user_alice-0-web:80"
    }
  ]
}
```

---

## `POST /nginx/reconnect-all` — Reconnect Nginx to All Networks

Iterates all entries in `user_registry.yml`, reconnects `subnet-acl-nginx` to each
user network (idempotent), then reloads nginx.

**Response `200`**
```json
{
  "total_networks": 5,
  "reconnected": 5,
  "nginx_reloaded": true
}
```

---

## `POST /reconcile` — Run Live Reconciliation

Reads all `*.nginx.conf` files, verifies each upstream container is running,
reconnects nginx to every network in `user_registry.yml`, reloads nginx, and
reports per-service container health from stored `container_names`.  Nothing is
persisted to disk — this is a live query.

**Response `200`**
```json
{
  "message": "Reconciliation completed.",
  "report": {
    "last_run": "2026-07-06T09:34:49+00:00",
    "total_upstreams": 2,
    "reachable": 1,
    "unreachable": 1,
    "unreachable_details": [{"upstream": "...", "target_container": "...", "reason": "container not found"}],
    "networks_reconnected": 2,
    "total_networks_in_registry": 2,
    "nginx_reloaded": true,
    "upstreams": [{"conf_file": "...", "server_name": "...", "proxy_pass": "...", "target_container": "...", "reachable": true}],
    "containers_healthy": 1,
    "containers_total": 2
  }
}
```

---

## `GET /reconcile/status` — Live Nginx State Snapshot

Returns a live snapshot derived from `user_registry.yml` + Docker queries.
No cached state file is read — everything is queried live.

**Response `200`**
```json
{
  "total_users": 2,
  "total_networks": 2,
  "nginx_connected_networks": 2,
  "networks": ["siyuan-user_alice-0", "siyuan-mcp-user_alice-0"],
  "connected": ["siyuan-user_alice-0", "siyuan-mcp-user_alice-0"],
  "disconnected": [],
  "total_nginx_confs": 2,
  "services": [
    {
      "user_name": "alice",
      "service_name": "siyuan",
      "label": "0",
      "network_name": "siyuan-user_alice-0",
      "container_names": ["siyuan-user_alice-0-siyuan"],
      "containers": [{"name": "siyuan-user_alice-0-siyuan", "status": "running"}]
    }
  ]
}
```

---

## `GET /nginx-state` — Same as `/reconcile/status`

Convenience alias — returns the same live state snapshot.

---

## `GET /container-stats` — Container-Level Statistics

Returns container statistics for **only** the containers referenced in `user_registry.yml`.
Uses a single `docker ps -a` call rather than per-container inspect.

Categories:
- **healthy_running** — container is running (and health check OK if present)
- **unhealthy_running** — container is running but health check reports unhealthy
- **restarting** — container is in restarting state
- **down** — container exists but is stopped / exited / paused
- **missing** — container does not exist at all

**Response `200`**
```json
{
  "container_stats": {
    "healthy_running": 12,
    "unhealthy_running": 1,
    "restarting": 0,
    "down": 3,
    "missing": 0,
    "total_expected": 16
  }
}
```

The gateway dashboard uses this instead of running `docker ps` directly.

---

## `GET /service-stats` — Service-Level Statistics

Returns service-level health computed from `user_registry.yml` only. A service is
**healthy** when ALL its expected containers are running and healthy; otherwise it
is **unhealthy**.

**Response `200`**
```json
{
  "service_stats": {
    "healthy": 5,
    "unhealthy": 1,
    "expected": 6
  }
}
```

The gateway dashboard uses this instead of running `docker ps` directly.

---

## SSL Certificate Management

Endpoints for managing SSL/TLS certificates independently of user registration.
Certificates are stored in `SSL_DIR/{domain}/` (default: `$PROVISION_DIR/ssl/{domain}/`).

### `GET /ssl-certs` — List Certificates

Scans `SSL_DIR/` for subdirectories containing both `fullchain.pem` and `privkey.pem`.
Returns domain names with expiry information (computed via `openssl x509 -enddate`).

**Response `200`**
```json
{
  "domains": [
    {
      "domain": "example.com",
      "fullchain_path": "/srv/provision_subnet_acl/ssl/example.com/fullchain.pem",
      "privkey_path": "/srv/provision_subnet_acl/ssl/example.com/privkey.pem",
      "created_at": "",
      "expiry_date": "2026-10-05",
      "days_left": 89
    }
  ]
}
```

### `POST /ssl-certs` — Upload Certificates

Supports two modes:
- **Paste mode**: provide `fullchain` and `privkey` PEM content directly.
- **Path mode**: provide `ssl_path` (directory containing `fullchain.pem` and `privkey.pem`).
  Files are read from that path. Stores the source path in `.source_path` for later refresh.

Saves files to `SSL_DIR/{domain}/`. Overwrites existing files.

**Request body** (paste mode)
```json
{
  "domain": "example.com",
  "fullchain": "-----BEGIN CERTIFICATE-----\n...",
  "privkey": "-----BEGIN PRIVATE KEY-----\n..."
}
```

**Request body** (path mode)
```json
{
  "domain": "example.com",
  "ssl_path": "/etc/letsencrypt/live/example.com"
}
```

**Response `201`**
```json
{
  "domain": "example.com",
  "fullchain_path": "/srv/provision_subnet_acl/ssl/example.com/fullchain.pem",
  "privkey_path": "/srv/provision_subnet_acl/ssl/example.com/privkey.pem",
  "expiry_date": "2026-10-05",
  "days_left": 89,
  "message": "SSL certificates saved for example.com"
}
```

**Error codes**

| Code | Cause |
|---|---|
| `400` | Invalid domain name (empty, contains `/` or `..`) |
| `400` | `ssl_path` is not a directory |
| `400` | `fullchain.pem` or `privkey.pem` not found in `ssl_path` |

### `POST /ssl-certs/{domain}/refresh` — Refresh Certificates

Re-reads certificates from the original source path stored in `.source_path`.
Only works for certs that were originally uploaded via path mode.

**Response `200`**
```json
{
  "domain": "example.com",
  "fullchain_path": "/srv/provision_subnet_acl/ssl/example.com/fullchain.pem",
  "privkey_path": "/srv/provision_subnet_acl/ssl/example.com/privkey.pem",
  "expiry_date": "2026-10-05",
  "days_left": 89,
  "message": "SSL certificates refreshed for example.com"
}
```

**Error codes**

| Code | Cause |
|---|---|
| `404` | No certificates found for domain |
| `400` | No source path stored (uploaded via paste mode, not path mode) |
| `400` | Source path no longer exists |

### `DELETE /ssl-certs/{domain}` — Delete Certificates

Removes the entire `SSL_DIR/{domain}/` directory tree.

**Response `200`**
```json
{
  "domain": "example.com",
  "message": "SSL certificates deleted for example.com"
}
```

**Error codes**

| Code | Cause |
|---|---|
| `404` | No certificates found for domain |

---

## `GET /services/{service_name}/check-missing-files` — Deployment File Readiness

Checks which essential deployment files exist for a service under
`SOURCE_PROJECTS_DIR/{service_name}`. Used by the gateway to offer LLM-based generation
or manual upload before deployment.

**Query parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `recipe_path` | string | `""` | Optional recipe subdirectory for multi-recipe projects (e.g. `recipes/web`). When set, files are checked under `SOURCE_PROJECTS_DIR/{service_name}/{recipe_path}`. |

Essential files checked:
- `docker-compose.yml` (or `.yml.j2` / `.yaml.j2` template)
- `nginx.conf` (or `.conf.j2` template)
- `Dockerfile`
- `.env` (recommended, but not strictly required)

**Example — recipe subdirectory**
```bash
curl "http://localhost:8875/services/myapp/check-missing-files?recipe_path=recipes/web"
```

**Response `200`**
```json
{
  "service_name": "myapp",
  "project_root": "/srv/provision_subnet_acl/source_projects/myapp/recipes/web",
  "ready": true,
  "missing": [],
  "existing": ["docker-compose", "nginx.conf", "Dockerfile", ".env"]
}
```

**Error codes**

| Code | Cause |
|---|---|
| `404` | Service (or recipe subdirectory) not found — the message includes the `recipe_path` when one was given |

---

## `GET /subnet-pool` — Subnet Pool Usage

Returns subnet pool usage statistics for the dashboard. Computed from the `SUBNET_POOLS`
environment variable plus registry entries. When `SUBNET_POOLS` is empty or unset, subnet
management is disabled and the endpoint returns the disabled state.

**Response `200`** (enabled)
```json
{
  "enabled": true,
  "pools": [
    {
      "cidr": "100.96.0.0/16",
      "total_slots": 16384,
      "used_slots": 1,
      "free_slots": 16383,
      "used_pct": 0.0,
      "exhausted": false
    }
  ],
  "overall": { "total_slots": 32768, "used_slots": 1, "free_slots": 32767 },
  "allocations": [
    { "user": "alice", "service": "myapp", "label": "0", "subnet": "100.96.0.0/29" }
  ],
  "headroom": 2
}
```

**Response `200`** (disabled)
```json
{
  "enabled": false,
  "pools": [],
  "headroom": 2,
  "message": "Subnet management disabled"
}
```

Slots are counted in `/30` granularity. Each registered entry's `subnet` / `gateway` is
tracked in `user_registry.yml`; allocations for live Docker networks that fall inside a
configured pool are also counted so parallel stacks never collide.

---

## Quick Reference

```bash
# Async register (default) — returns task_id immediately
curl -X POST http://localhost:8875/users -H 'Content-Type: application/json' -d '{...}'
# → {"task_id": "a1b2c3d4e5f6", "status": "pending"}

# Poll task status
curl http://localhost:8875/tasks/a1b2c3d4e5f6

# List all tasks
curl http://localhost:8875/tasks

# Cancel a task
curl -X DELETE http://localhost:8875/tasks/a1b2c3d4e5f6

# SSE build log stream
curl http://localhost:8875/tasks/a1b2c3d4e5f6/log

# Sync register (blocking — backward compatible)
curl -X POST "http://localhost:8875/users?sync=true" -H 'Content-Type: application/json' -d '{...}'

# Sync rebuild
curl -X POST "http://localhost:8875/users/alice/services/myapp/0/rebuild?sync=true" \
  -H 'Content-Type: application/json' -d '{"no_cache": true}'

# Sync remove
curl -X DELETE "http://localhost:8875/users/alice/services/myapp/0?sync=true"

# Start / stop service
curl -X POST http://localhost:8875/users/alice/services/myapp/0/up
curl -X POST http://localhost:8875/users/alice/services/myapp/0/down

# Change password
curl -X PUT http://localhost:8875/users/alice/services/myapp/0/password \
  -H 'Content-Type: application/json' -d '{"passwd": "newsecret"}'

# Container logs
curl "http://localhost:8875/users/alice/services/myapp/0/containers/web/logs?tail=50"

# Reconciliation
curl -X POST http://localhost:8875/reconcile
curl http://localhost:8875/reconcile/status
curl http://localhost:8875/nginx-state

# Docker / host stats
curl http://localhost:8875/docker/ps
curl http://localhost:8875/docker/stats
curl http://localhost:8875/docker/info
curl http://localhost:8875/host/stats

# Container / service stats (registry-scoped)
curl http://localhost:8875/container-stats
curl http://localhost:8875/service-stats

# SSL certificate management
curl http://localhost:8875/ssl-certs
curl -X POST http://localhost:8875/ssl-certs -H 'Content-Type: application/json' -d '{"domain":"example.com","ssl_path":"/etc/letsencrypt/live/example.com"}'
curl -X POST http://localhost:8875/ssl-certs/example.com/refresh
curl -X DELETE http://localhost:8875/ssl-certs/example.com

# Reconciliation helpers
curl http://localhost:8875/docker/container/subnet-acl-nginx/exists
curl http://localhost:8875/docker/container/subnet-acl-nginx/running
curl -X POST http://localhost:8875/docker/network/mynet/connect/subnet-acl-nginx
curl -X POST http://localhost:8875/docker/nginx/reload

# Nginx state
curl http://localhost:8875/nginx/connections
curl -X POST http://localhost:8875/nginx/reconnect-all
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GENERATED_DIR` | `./generated` | Directory for nginx conf, htpasswd, and `user_registry.yml` |
| `REGISTRY_FILE` | `./user_registry.yml` | Path to the registry state file |
| `DOCKER_OPS_LOG` | _(unset)_ | If set, path to a file where all docker command stdout/stderr is appended for debugging (e.g. `${PROVISION_DIR}/generated/docker_ops.log`) |
| `PROVISION_API_PORT` | `8875` | Host port (set in `docker-compose.provision.yml`) |
| `SUBNET_POOLS` | _(empty)_ | Comma-separated `/16` pools for subnet management (e.g. `100.96.0.0/16,100.97.0.0/16`). Empty/unset = subnet management disabled |
| `SUBNET_HEADROOM` | `2` | Extra host IPs reserved per service (added to container count + 1 gateway when sizing a subnet) |
| `ENABLE_ACL` | `false` | v4 mode switch: `true` = env.d one-liner `set $auth_mode acl;` → per-service conf calls the gateway `/api/auth/verify` for ACL; `false` = env.d one-liner `set $auth_mode basic;` → Basic dialog via `/__basic__/`. The per-service conf is **byte-identical** across modes (never regenerated on mode switch) |
