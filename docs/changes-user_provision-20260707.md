# `_users_provision` Changes: `86bd51f` → HEAD

**Date**: 2026-07-06  
**Purpose**: Guide for `_provision_gateway` developers to understand all API contract changes, new endpoints, modified behavior, and new configuration in the provision-api.

---

## Table of Contents

1. [New API Endpoints](#1-new-api-endpoints)
2. [Modified API Endpoints](#2-modified-api-endpoints)
3. [New Environment Variables](#3-new-environment-variables)
4. [New Modules](#4-new-modules)
5. [Module Changes](#5-module-changes)
6. [Config/Infra Changes](#6-configinfra-changes)
7. [New/Changed Tests](#7-newchanged-tests)
8. [Doc Changes](#8-doc-changes)
9. [Bug Fixes](#9-bug-fixes)
10. [API Contract Changes (Gateway Impact)](#10-api-contract-changes-gateway-impact)

---

## 1. New API Endpoints

All new endpoints are defined in `user_provision_tool/api.py`.

### 1.1 Service Lifecycle

| Method | Path | Handler | Description |
|--------|------|---------|-------------|
| `POST` | `/users/{user_name}/services/{service_name}/{label}/up` | `start_user_service()` | Start a service's containers (`docker compose up -d`) |
| `POST` | `/users/{user_name}/services/{service_name}/{label}/down` | `stop_user_service()` | Stop a service's containers (`docker compose stop`) |
| `PUT` | `/users/{user_name}/services/{service_name}/{label}/password` | `change_password()` | Change a user's password (re-hash bcrypt, update `.htpasswd`, reload nginx) |
| `GET` | `/users/{user_name}/services/{service_name}/{label}/containers/{container}/logs` | `get_container_logs()` | Get container logs for a specific compose service |

#### `POST .../up` — Start Service

- **Request body**: None
- **Response `200`**:
  ```json
  { "message": "Service started.", "status": "up" }
  ```
- **Errors**: `404` (KeyError — no registration; FileNotFoundError — compose file missing), `500` (RuntimeError — `docker compose up` failed)

#### `POST .../down` — Stop Service

- **Request body**: None
- **Response `200`**:
  ```json
  { "message": "Service stopped.", "status": "down" }
  ```
- **Errors**: `404` (KeyError — no registration; FileNotFoundError — compose file missing), `500` (RuntimeError — `docker compose stop` failed)

#### `PUT .../password` — Change Password

- **Request body**:
  ```json
  { "passwd": "newsecret" }
  ```
- **Response `200`**:
  ```json
  {
    "message": "Password updated. Nginx reloaded.",
    "user_name": "alice",
    "service_name": "myapp",
    "label": "0"
  }
  ```
- **Errors**: `404` (no registration found, or htpasswd file missing)

#### `GET .../containers/{container}/logs` — Container Logs

- **Query params**:

  | Parameter | Type | Default | Description |
  |-----------|------|---------|-------------|
  | `tail` | int | `100` | Number of log lines to return |

- **Response `200`**:
  ```json
  {
    "container": "myapp-user_alice-0-web",
    "tail": 100,
    "logs": ["line1", "line2", "..."]
  }
  ```
- **Errors**: `404` (no registration, or container does not exist)
- **Note**: `{container}` is the **short compose service name** (e.g., `web`, `db`), NOT the full Docker container name.

---

### 1.2 Docker / Host Monitoring

| Method | Path | Handler | Description |
|--------|------|---------|-------------|
| `GET` | `/docker/ps` | `docker_ps_list()` | List all Docker containers (`docker ps -a`) |
| `GET` | `/docker/stats` | `docker_stats()` | Per-container resource stats (`docker stats --no-stream`) |
| `GET` | `/docker/info` | `docker_info()` | Docker host info (container counts from `docker info`) |
| `GET` | `/host/stats` | `host_stats()` | Host-level CPU/memory/disk usage (reads `/proc/meminfo`, `/proc/stat`, `shutil.disk_usage`) |

#### `GET /docker/ps`

- **Response `200`**: `list[dict]`
  ```json
  [
    { "name": "provision-nginx", "status": "Up 3 hours", "image": "nginx:alpine" },
    { "name": "myapp-user_alice-0-web", "status": "Up 2 hours", "image": "myapp:latest" }
  ]
  ```

#### `GET /docker/stats`

- **Response `200`**: `list[dict]`
  ```json
  [
    { "name": "provision-nginx", "cpu": "0.05%", "mem": "10.5MiB / 1.94GiB" }
  ]
  ```

#### `GET /docker/info`

- **Response `200`**:
  ```json
  {
    "containers_total": 10,
    "containers_running": 3,
    "containers_paused": 1,
    "containers_stopped": 6
  }
  ```

#### `GET /host/stats`

- **Response `200`**:
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

### 1.3 Reconciliation Helpers (called by gateway)

| Method | Path | Handler | Description |
|--------|------|---------|-------------|
| `GET` | `/docker/container/{container}/exists` | `container_exists_ep()` | Check if a Docker container exists |
| `GET` | `/docker/container/{container}/running` | `container_running_ep()` | Check if a Docker container is running |
| `POST` | `/docker/network/{network}/connect/{container}` | `network_connect_ep()` | Connect a container to a Docker network |
| `POST` | `/docker/nginx/reload` | `nginx_reload_ep()` | Reload nginx in a container |

#### `GET /docker/container/{container}/exists`
- **Response `200`**: `{ "exists": true }` or `{ "exists": false }`

#### `GET /docker/container/{container}/running`
- **Response `200`**: `{ "running": true }` or `{ "running": false }`

#### `POST /docker/network/{network}/connect/{container}`
- **Request body**: None
- **Response `200`**: `{ "connected": true }`

#### `POST /docker/nginx/reload`
- **Query params**:

  | Parameter | Type | Default | Description |
  |-----------|------|---------|-------------|
  | `container` | string | `provision-nginx` | Nginx container name to reload |

- **Response `200`**: `{ "reloaded": true }`

---

### 1.4 Nginx State Management

| Method | Path | Handler | Description |
|--------|------|---------|-------------|
| `GET` | `/nginx/connections` | `get_nginx_connections()` | Nginx connection state (networks, confs, upstreams) |
| `POST` | `/nginx/reconnect-all` | `reconnect_all()` | Reconnect nginx to all user networks and reload |
| `POST` | `/reconcile` | `trigger_reconciliation()` | Run live nginx upstream reconciliation |
| `GET` | `/reconcile/status` | `reconciliation_status()` | Live nginx state snapshot |
| `GET` | `/nginx-state` | `get_nginx_state()` | Same as `/reconcile/status` |

#### `POST /reconcile` — Run Live Reconciliation

- **Response `200`**:
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
- **Errors**: `500` (if reconciliation throws)

#### `GET /reconcile/status` — Live Nginx State Snapshot

- **Response `200`**:
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

#### `GET /nginx-state`
Same response as `/reconcile/status` — convenience alias.

---

### 1.5 SSE Build Log Streaming (Enhanced)

| Method | Path | Handler | Description |
|--------|------|---------|-------------|
| `GET` | `/tasks/{task_id}/log` | `stream_task_log()` | SSE stream of per-task build log |

- **Query params**:

  | Parameter | Type | Default | Description |
  |-----------|------|---------|-------------|
  | `tail` | int | `200` | Number of recent lines to send first |
  | `follow` | bool | `true` | Keep streaming new lines (`false` for one-shot) |

- **Response**: `Content-Type: text/event-stream`
  ```
  data: + docker compose -f ... up -d
  data: + docker network connect myapp-user_alice-0 provision-nginx
  data: + docker exec provision-nginx nginx -s reload
  event: done
  data: {}
  ```

- **Behavior**: Reads from **per-task isolated log file** (`$TASK_LOG_DIR/task-{task_id}.log`) first. Falls back to global `DOCKER_OPS_LOG` if per-task log doesn't exist.

---

## 2. Modified API Endpoints

### 2.1 `POST /users` (Register) — New proxy_pass Validation

**New behavior**: Before converting the nginx conf, `_validate_nginx_proxy_targets()` checks that every `proxy_pass` host in the nginx conf **exactly matches** a compose service name from `docker-compose.yml`. If any host does NOT match, registration is rejected:

- **Error `422`**: `"nginx conf proxy_pass target(s) do not match any compose service name. Compose services: [...]. Unknown proxy_pass host(s): [...]. Update nginx.conf so every proxy_pass host matches a service key from docker-compose.yml."`

**Before**: The converter used a `service_name_hint` prefix heuristic to strip prefixes like `myapp-` from proxy_pass hosts (e.g., `myapp-web` → `web`). No validation was performed.

**After**: Only exact compose service name matching. No prefix stripping. Unknown hosts are **rejected** at registration time.

> ⚠️ **BREAKING**: Nginx confs using prefixed hostnames (e.g., `myapp-web` instead of `web`) will now be rejected at registration.

### 2.2 `GET /users` and `GET /users/{user_name}` (Status) — New `container_names` Field

Each service entry now includes a **new field**:
```json
{
  "container_names": ["myapp-user_alice-0-web", "myapp-user_alice-0-db"],
  ...
}
```

Container names are sourced from the registry entry's `container_names` field (stored at registration time), with a backward-compatible fallback to deriving from the compose file for legacy entries.

### 2.3 `POST /users` (Register) — Registry Now Stores `container_names`

At registration time, the provisioner now:
1. Parses the rendered compose file to get service names
2. Computes full container names using `template_engine.container_prefix()`
3. Stores `container_names` in the registry entry
4. Always re-saves the registry to persist both `env_file_path` and `container_names`

### 2.4 `DELETE /users/{user_name}/services/{service_name}/{label}` — Orphan Network Cleanup

During removal, after `compose_down`, the provisioner now:
1. Calls `docker_ops.orphan_network_cleanup()` to remove the Docker network if nginx was the only remaining container
2. Calls `docker_ops.nginx_reload()` to remove stale upstream references

---

## 3. New Environment Variables

| Variable | Default | Consumed In | Purpose |
|----------|---------|-------------|---------|
| `TASK_LOG_DIR` | `$GENERATED_DIR/task_logs` | `task_manager.py`, `api.py` | Directory for per-task isolated `.log` files |
| `TASK_TTL_SECONDS` | `604800` (7 days) | `task_manager.py` | How long finished tasks and their log files are retained |
| `TASK_MAX_COUNT` | `1000` | `task_manager.py` | Maximum number of tasks retained in memory |

All three are exposed in `.env.example` (commented out), `docker-compose.provision.yml`, and `docs/deployment.md`.

---

## 4. New Modules

### 4.1 `lib/reconciliation.py` — **NEW FILE** (276 lines)

Provides nginx network recovery and live reconciliation. All state from `user_registry.yml` — no separate state file.

| Function | Signature | Description |
|----------|-----------|-------------|
| `recover_on_startup()` | `(nginx_container: str = "provision-nginx") -> dict` | Reconnect nginx to every user network from registry. Called automatically on API boot via FastAPI `lifespan`. Returns `{"networks_reconnected": int, "networks_total": int, "nginx_reloaded": bool}`. Idempotent. |
| `run_reconciliation()` | `(nginx_container: str = "provision-nginx") -> dict` | Parse all `*.nginx.conf` files, verify each upstream container is running, reconnect nginx to all networks, reload nginx. Returns full report dict. Nothing persisted. |
| `get_nginx_state()` | `() -> dict` | Live snapshot of nginx state from `user_registry.yml` + Docker queries. Returns `{"total_users", "total_networks", "nginx_connected_networks", "networks", "connected", "disconnected", "total_nginx_confs", "services"}`. |

**Private helpers**:

| Function | Description |
|----------|-------------|
| `_derived_container_names(entry)` | Backward-compat fallback: derive container names from compose file when `container_names` not stored in registry |
| `_generated_dir()` | Returns `GENERATED_DIR` from env |

---

## 5. Module Changes

### 5.1 `lib/docker_ops.py` — New Functions + Thread-Local Logging

**New public functions**:

| Function | Signature | Description |
|----------|-----------|-------------|
| `compose_stop()` | `(compose_file, env_file=None, project_name=None) -> None` | Stop containers without removing them |
| `docker_info()` | `() -> dict` | Docker system info with container counts. Returns `{}` on parse failure. |
| `docker_stats_snapshot()` | `() -> list[dict]` | Per-container resource stats. Returns list of `{name, cpu, mem}`. |
| `container_exists()` | `(container: str) -> bool` | Check if container exists |
| `container_running()` | `(container: str) -> bool` | Check if container is running |
| `container_inspect()` | `(container: str) -> dict \| None` | Full `docker inspect` parsed JSON |
| `network_list()` | `() -> list[str]` | List all Docker network names |
| `network_inspect()` | `(network: str) -> dict \| None` | Inspect a Docker network |
| `network_connected_to_container()` | `(network: str, container: str) -> bool` | Check if container is connected to network |
| `container_logs()` | `(container: str, tail: int = 100) -> str` | Container logs via `docker logs --tail N` |
| `orphan_network_cleanup()` | `(network_name: str, nginx_container: str = "provision-nginx") -> bool` | Remove network if only nginx remains. Returns `True` if removed. |

**New thread-local log functions**:

| Function | Signature | Description |
|----------|-----------|-------------|
| `set_task_log_file()` | `(path: str) -> None` | Set per-task log file for current thread |
| `clear_task_log_file()` | `() -> None` | Clear per-task log file for current thread |

**Modified behavior**:

- **`_write_log()`**: Now writes to **two** destinations: global `DOCKER_OPS_LOG` file (if set) AND thread-local per-task log file (if `set_task_log_file()` was called).

### 5.2 `lib/provisioner.py` — New Functions

**New functions**:

| Function | Signature | Description |
|----------|-----------|-------------|
| `start_service()` | `(*, user_name, service_name, label) -> dict` | Start service (`docker compose up -d`). Raises `KeyError`, `FileNotFoundError`, `RuntimeError`. |
| `stop_service()` | `(*, user_name, service_name, label) -> dict` | Stop service (`docker compose stop`). Same exceptions. |

**Modified**:

- `register_user()`: Computes and stores `container_names` in registry; always re-saves registry
- `remove_user()`: Calls `orphan_network_cleanup()` + `nginx_reload()` after compose_down

### 5.3 `lib/task_manager.py` — Per-Task Log Files + Configurable TTL

**`Task` class** — New slot: `log_file: str`

**`TaskManager.__init__()` signature change**:

| Before | After |
|--------|-------|
| `(max_workers=4, max_age_seconds=3600)` | `(max_workers=4, ttl_seconds=604800, max_tasks=1000, log_dir="")` |

- `ttl_seconds` replaces `max_age_seconds` — default changed from **1 hour** → **7 days**
- `max_tasks`: max tasks retained; oldest evicted when exceeded
- `log_dir`: directory for per-task log files

**New methods**:

| Method | Signature | Description |
|--------|-----------|-------------|
| `get_log_file()` | `(task_id: str) -> str \| None` | Return per-task log file path, or `None` |
| `_cleanup_excess()` | — | Evict oldest tasks when count exceeds `_max_tasks` |
| `_delete_task_log()` | `(task_id: str)` static | Delete per-task log file from disk |

**Modified methods**:

- `submit()`: Creates log file path, calls `_cleanup_excess()`
- `_run_task()`: Calls `set_task_log_file()` before execution, `clear_task_log_file()` in `finally`
- `_cleanup_stale()`: Uses `_ttl` instead of `_max_age`; deletes log files via `_delete_task_log()`

**Singleton init** — reads env vars:
```python
task_manager = TaskManager(
    ttl_seconds=int(os.environ.get("TASK_TTL_SECONDS", "604800")),
    max_tasks=int(os.environ.get("TASK_MAX_COUNT", "1000")),
    log_dir=os.environ.get("TASK_LOG_DIR",
        str(Path(os.environ.get("GENERATED_DIR", "./generated")) / "task_logs")),
)
```

### 5.4 `lib/nginx_converter.py` — Deterministic proxy_pass Rewriting

**`convert_nginx()` parameter semantics changed**:

| Before | After |
|--------|-------|
| `service_name_hint` used for prefix-stripping heuristic | `service_name_hint` **deprecated** for proxy_pass; only used for header comment |
| `compose_service_names` used for exact match | `compose_service_names` is **the ONLY input** for proxy_pass rewriting |

> ⚠️ **BREAKING**: The prefix-stripping heuristic (`myapp-web` → `web`) is removed. If `compose_service_names` is empty/None, proxy_pass targets are left unchanged. Unknown hosts are rejected at registration time.

### 5.5 `cli/register.py` — proxy_pass Validation

Added `_validate_nginx_proxy_targets()` — same logic as API version. Rejects registration with exit code 1 if any proxy_pass host doesn't match a compose service name.

### 5.6 `cli/status.py` — Uses Stored `container_names`

Refactored to read `container_names` from registry entry first, with backward-compatible compose file fallback. Uses `template_engine.container_prefix()` for fallback derivation.

---

## 6. Config/Infra Changes

### 6.1 `.env.example`
Three new environment variables added (commented out):
```bash
# TASK_LOG_DIR=${PROVISION_DIR}/generated/task_logs
# TASK_TTL_SECONDS=604800
# TASK_MAX_COUNT=1000
```

### 6.2 `docker-compose.provision.yml`
Three new environment variables in the `provision-api` service:
```yaml
- TASK_LOG_DIR=${PROVISION_DIR}/generated/task_logs
- TASK_TTL_SECONDS=${TASK_TTL_SECONDS:-604800}
- TASK_MAX_COUNT=${TASK_MAX_COUNT:-1000}
```

### 6.3 `.gitignore`
Added `_ignore/` directory.

---

## 7. New/Changed Tests

### Test Count Summary

| Layer | Before | After | Δ |
|-------|--------|-------|---|
| Unit (`test_unit.py`) | 132 | **186** | +54 |
| E2E (`test_e2e.py`) | 40 | **40** | 0 (1 modified) |
| Proxy (`test_proxy_support.py`) | 38 | **38** | 0 |
| Task Manager (`test_task_manager.py`) | 10 | **14** | +6 (per-task log, TTL, max-count, thread isolation) |
| Integration (`test_integration.sh`) | 29 | **107** | +78 |
| **Total** | **249** | **385** | **+136** |

### New Test Coverage Areas

- **`TestDockerOps`**: 26 new tests covering compose_stop, docker_info, container_exists/running, container_inspect, network_list/inspect, network_connected_to_container, container_logs, orphan_network_cleanup, thread-local task log (set/clear/write/leak)
- **`TestProvisionerEnvFile`**: 7 new tests covering start_service, stop_service, change_password, remove with orphan cleanup
- **`TestAPINewEndpoints`**: 23 new tests covering all new API endpoints (docker/ps, docker/stats, docker/info, host/stats, up, down, password, nginx/connections, nginx/reconnect-all, container logs, task log SSE, health, tasks, reconcile, reconcile/status, nginx-state)
- **`TestNginxConverter`**: 2 modified tests for deterministic proxy_pass rewriting
- **Integration**: Tests 28-40 covering all new endpoints, reconciliation, up/down lifecycle, password change, container logs, SSE streaming, per-task logs

---

## 8. Doc Changes

| File | Changes |
|------|---------|
| `README.md` | Added sections 8-15 documenting all new endpoints; expanded SSE streaming section with per-task log details and env var configuration |
| `docs/api-reference.md` | Route table expanded from 10 to 22 endpoints; full documentation for each new endpoint including request/response schemas, query params, error codes; SSE endpoint fully documented with per-task log behavior |
| `docs/architecture.md` | Updated directory layout; updated module descriptions for docker_ops, provisioner, task_manager; added SSE log path to data flow diagram |
| `docs/deployment.md` | Added `TASK_LOG_DIR`, `TASK_TTL_SECONDS`, `TASK_MAX_COUNT` to env vars table and compose example |
| `docs/templates.md` | Updated proxy_pass conversion table; added critical warning about exact compose service name matching; documented validation rejection behavior |
| `docs/testing.md` | Updated all test counts (29→107, 132→186, 10→14, 220→278); added new test coverage descriptions |
| `skills/provision-api/SKILL.md` | Added examples for all new endpoints; updated response code table; updated proxy_pass documentation |

---

## 9. Bug Fixes

1. **Registry persistence**: `env_file_path` was conditionally saved; now always re-saved with `container_names`
2. **Orphan Docker networks**: User removal now cleans up the Docker network if nginx was the only remaining container
3. **Nginx reload after removal**: `remove_user()` now reloads nginx to remove stale upstream references
4. **Deterministic proxy_pass rewriting**: Prefix-stripping heuristic removed; exact compose service name matching only
5. **Task cleanup TTL**: Changed from 1 hour to 7 days (configurable); log files now deleted alongside tasks
6. **Container names in status**: Now stored in registry at registration time rather than derived from compose file every query

---

## 10. API Contract Changes (Gateway Impact)

### 10.1 New Endpoints to Proxy

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/users/{u}/services/{s}/{l}/up` | POST | Start stopped service |
| `/users/{u}/services/{s}/{l}/down` | POST | Stop service |
| `/users/{u}/services/{s}/{l}/password` | PUT | Change password |
| `/users/{u}/services/{s}/{l}/containers/{c}/logs` | GET | Container logs |
| `/docker/ps` | GET | List containers |
| `/docker/stats` | GET | Resource stats |
| `/docker/info` | GET | Docker host info |
| `/host/stats` | GET | Host CPU/mem/disk |
| `/docker/container/{c}/exists` | GET | Container exists check |
| `/docker/container/{c}/running` | GET | Container running check |
| `/docker/network/{n}/connect/{c}` | POST | Connect to network |
| `/docker/nginx/reload` | POST | Reload nginx |
| `/nginx/connections` | GET | Nginx state |
| `/nginx/reconnect-all` | POST | Reconnect all networks |
| `/reconcile` | POST | Run reconciliation |
| `/reconcile/status` | GET | Nginx state snapshot |
| `/nginx-state` | GET | Same as `/reconcile/status` |
| `/tasks/{task_id}/log` | GET | SSE log streaming |

### 10.2 Changed Response Formats

**`GET /users` and `GET /users/{user_name}`** — Each service status now includes:
```json
"container_names": ["full-container-name-1", "full-container-name-2"]
```

**`POST /users` (register)** — New possible error:
- **HTTP 422** when nginx conf `proxy_pass` hosts don't match compose service names. Error message contains `Compose services:` and `Unknown proxy_pass host(s):` lists.

### 10.3 New Query Parameters

| Endpoint | Parameter | Type | Default |
|----------|-----------|------|---------|
| `GET .../containers/{c}/logs` | `tail` | int | `100` |
| `GET /tasks/{task_id}/log` | `tail` | int | `200` |
| `GET /tasks/{task_id}/log` | `follow` | bool | `true` |
| `POST /docker/nginx/reload` | `container` | string | `provision-nginx` |

### 10.4 New Error Codes

| Endpoint | Code | Condition |
|----------|------|-----------|
| `POST .../up` | `404` | No registration or compose missing |
| `POST .../up` | `500` | compose up failed |
| `POST .../down` | `404` | No registration or compose missing |
| `POST .../down` | `500` | compose stop failed |
| `PUT .../password` | `404` | No registration or htpasswd missing |
| `GET .../logs` | `404` | No registration or container missing |
| `POST /users` | `422` | proxy_pass hosts don't match compose services |
| `POST /reconcile` | `500` | Reconciliation exception |

### 10.5 Breaking Changes

1. **proxy_pass naming**: Nginx confs must use **exact compose service names** in `proxy_pass`. Previously prefixed names like `myapp-web` were accepted; now only `web` works. Registration is rejected (422) otherwise.
2. **Task TTL**: Changed from 1 hour to 7 days (configurable). Completed task status and log files persist much longer.
3. **`container_names` field**: New field in user status responses. Gateway must handle its presence.

### 10.6 New Environment Variables

If the gateway manages the provision-api deployment, configure:
- `TASK_LOG_DIR` — default: `$GENERATED_DIR/task_logs`
- `TASK_TTL_SECONDS` — default: `604800`
- `TASK_MAX_COUNT` — default: `1000`

### 10.7 Startup Behavior

The provision-api now runs `reconciliation.recover_on_startup()` on boot (FastAPI `lifespan`). This automatically reconnects `provision-nginx` to all user networks from the registry. No gateway action needed.

---

**Diff summary**: 23 files changed. 18 new API endpoints. 3 new env vars. 1 new module (`reconciliation.py`). 136 new tests. Significant behavioral changes in proxy_pass validation, task management, and container name tracking.
