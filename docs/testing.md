# Testing

The test suite has six layers:

```
Integration (bash)   tests/test_integration.sh     120 tests
  └─ full Docker round-trip: build image → start API → register → rebuild → remove
    also covers container-stats, service-stats, nginx resilience (missing upstreams),
    up/down, password change, container logs, SSE per-task log streaming,
    docker/host stats, nginx connections, reconnect-all, reconciliation,
    HTTPS registration, proxy_pass compose name detection, env_file handling,
    async task pool, project_root resolution, build_args with MockProxy,
    per-task isolated log files (creation, content, SSE streaming)

E2E (pytest)         tests/test_e2e.py              43 tests
  └─ exercise CLI scripts end-to-end against real files, no Docker
    includes proxy --build-arg support tests
    includes env_file_path with per-user copy + env_file: .env rewrite

Proxy Support        tests/test_proxy_support.py    38 tests
  └─ docker_ops, provisioner, API models, CLI parsing, MockProxy lifecycle

Async Task Pool      tests/test_task_manager.py     16 tests
  └─ submit, complete, fail, cancel, list_all, uniqueness, per-task log file,
    TTL cleanup, max-count eviction

Subnet Manager       tests/test_subnet_manager.py   23 tests
  └─ subnet sizing (/30../24), bitmap allocation + alignment, pool exhaustion,
    registry/host subnet discovery, pool stats (GET /subnet-pool)

Unit (pytest)        tests/test_unit.py             233 tests
  └─ individual lib/ functions in isolation, all I/O mocked
    includes provisioner proxy support tests
    includes env_file render_compose rewrite + per-user copy tests
    includes HTTPS: nginx rendering, converter SSL path replacement,
      SSL block wrapping, provisioner cert copying + bare filenames
    includes docker_ops: compose_stop, docker_info, container_exists/running,
      container_inspect, network_list/inspect, network_connected_to_container,
      container_logs, orphan_network_cleanup, thread-local task log,
      docker_ps_all, per-task log threading fix
    includes provisioner: start_service, stop_service, change_password,
      orphan network cleanup on remove
    includes subnet/IPAM: ensure_subnet_ipam_block inject + .bak backup
    includes ACL template: ENABLE_ACL=true auth_request/_auth_jwt enforcement,
      ENABLE_ACL=false keeps auth_basic, _set_token port-preserving redirect,
      no-ACL-locations templates left untouched
    includes check-missing-files endpoint: response model, route, 404 for
      missing service, all-present, j2 templates, recipe_path subdir (present,
      missing dir 404, root files ignored)
    includes GET /subnet-pool: stats when SUBNET_POOLS set, disabled when empty
    includes api (FastAPI TestClient): up/down/password endpoints, docker/ps,
      docker/stats, docker/info, host/stats, reconciliation helpers,
      nginx/connections, nginx/reconnect-all, container logs, task log SSE,
      health, tasks list, container-stats, service-stats,
      ssl-certs (list/upload/refresh/delete) — all with mocked docker_ops
```

---

## Unit Tests

**File:** `tests/test_unit.py`  
**Covers:** `validation`, `registry`, `template_engine`, `auth`, `docker_ops`, `compose_converter`, `nginx_converter`, `subnet_manager`, `provisioner` (proxy support) — plus the API endpoints for `check-missing-files` (incl. `recipe_path`) and `/subnet-pool`

Run:
```bash
uv run pytest tests/test_unit.py -v
```

Notable patterns:
- `registry` tests use the `registry_file` fixture (monkeypatched temp file).
- `docker_ops` tests mock `subprocess.Popen` to avoid real Docker calls (was `subprocess.run` prior to real-time-output refactoring).
- `template_engine` tests render against fixture templates in `tests/fixtures/`.
- `TestComposeConverter` covers container_name rewriting, volume extraction, network substitution, profile filtering.
- `TestNginxConverter` covers server_name, auth_basic, proxy_pass, htpasswd_path substitutions, and SSL certificate path conversion with `{% if https %}` block wrapping.
- `TestProvisionerProxySupport` covers `build_args` storage in registry and rebuild fallback.
- `TestProvisionerEnvFile` covers HTTPS, start_service, stop_service, change_password, orphan network cleanup, and `container_names` storage in registry.
- `TestAPINewEndpoints` covers API endpoints using FastAPI `TestClient`: docker/ps, docker/stats, docker/info, host/stats, reconciliation helpers, up/down/password, nginx/connections, nginx/reconnect-all, container logs, task log SSE, health, tasks, reconcile, reconcile/status, nginx-state, container-stats, service-stats, ssl-certs (list/upload/refresh/delete).
- `TestNginxConverter` covers deterministic proxy_pass rewriting (exact compose service name matching, no prefix stripping), SSL certificate path replacement, auth_basic injection, and HTTPS block auto-generation.
- `TestRenderNginxConfACL` covers `ENABLE_ACL=true` — `auth_request /_auth_jwt` + `error_page 401/403` dashboard redirects injected, `auth_basic` stripped, the `_set_token` redirect preserved with `$scheme://$http_host$arg_redirect`; `ENABLE_ACL=false` keeps `auth_basic`; a template with no `location = /_auth_jwt` is left untouched.
- `TestEnsureSubnetIpamBlock` covers `ensure_subnet_ipam_block()` — injects the `{% if subnet %}` ipam block into an old template and backs the original up as `.bak`.
- `TestCheckMissingFiles` covers `GET /services/{service_name}/check-missing-files` — response model, route registration, 404 for missing service, all-present, `.j2` templates, and the `recipe_path` query parameter (recipe subdir present, missing recipe dir → 404, root files ignored when `recipe_path` is given).
- `TestSubnetPoolAPI` covers `GET /subnet-pool` — returns pool stats when `SUBNET_POOLS` is set and the disabled state when it is empty.

---

## Subnet Manager Tests

**File:** `tests/test_subnet_manager.py` — 23 tests

```bash
uv run pytest tests/test_subnet_manager.py -v
```

Covers `lib/subnet_manager.py`:
- **Sizing** — `subnet_size_for_containers()` returns the smallest `/30`..`/24` fitting `containers + HEADROOM + 1(gateway)`; single/two/three containers → `/29` (the minimum so nginx can join), four/six → `/28`, fourteen → `/27`, thirty → `/26`, sixty-two → `/25`, larger → `/24`.
- **Allocation** — `allocate_subnet()` picks a free block from the pool, returns `None` when disabled, raises `RuntimeError` on exhaustion, skips already-allocated subnets, and aligns blocks to their own size.
- **Pool stats** — `get_pool_stats()` returns `enabled`/`pools`/`overall`/`allocations`/`headroom`, and the disabled state when `SUBNET_POOLS` is empty.
- **Discovery** — `get_allocated_subnets()` / `get_host_allocated_subnets()` extract registry subnets and live Docker subnets inside the pools (empty when disabled / on Docker error).

---

## E2E Tests

**File:** `tests/test_e2e.py`  
**Covers:** all `cli/` scripts invoked end-to-end against real temp directories.

Run:
```bash
uv run pytest tests/test_e2e.py -v
```

Notable patterns:
- `docker_ops` is patched so no containers are started (`subprocess.Popen` mocked).
- `TestE2EProxySupport` — register/rebuild with `--build-arg` flag, registry storage, fallback.
- Tests verify file generation, registry writes, and stdout/stderr.
- `TestE2ERegistration` — basic registration, dedup, network isolation.
- `TestE2ERemoval` / `TestE2ERebuild` / `TestE2EStatus` — lifecycle operations.
- `TestE2EConverterIntegration` — `-fc` and `-fn` flags: plain file conversion + registration.
- `TestE2ENoPassword` — registration with `passwd=''`.
- `TestE2EFullLifecycle` — register → status → rebuild → remove round-trip.

---

## Proxy Support Tests

**File:** `tests/test_proxy_support.py` — 38 tests

```bash
uv run pytest tests/test_proxy_support.py -v
```

Covers: `docker_ops.compose_build` --build-arg flags, `provisioner` build_args flow, API Pydantic models, CLI `--build-arg` parsing, `MockProxy` lifecycle (start/stop/relay/context-manager).

---

## Async Task Pool Tests

**File:** `tests/test_task_manager.py` — 16 tests

```bash
uv run pytest tests/test_task_manager.py -v
```

Covers: submit → complete, submit → fail, cancel pending, cancel completed, cancel nonexistent, task ID uniqueness, task dict structure, list_all, empty list.

---

## Integration Tests

**File:** `tests/test_integration.sh`  
**Requires:** Docker, `curl`; `jq` optional (falls back to Python).

Runs the full end-to-end cycle against a real Docker daemon (120 tests):

```
Build subnet-acl-provision-api image
  └─ Start subnet-acl-provision-api container
       └─ Wait for /health
            ├─ GET  /users                              → empty list
            ├─ POST /users?sync=true (with passwd)      → register testuser/myapp/0
            ├─ POST /users?sync=true (duplicate)        → 409
            ├─ docker ps                                 → web + db containers running
            ├─ GET  /users/testuser                     → 1 healthy service
            ├─ POST .../rebuild?sync=true               → rebuilt
            ├─ docker ps                                 → containers still running
            ├─ DELETE .../myapp/0?sync=true             → removed
            ├─ docker ps                                 → containers gone
            ├─ GET  /users                              → empty list
            ├─ POST /users (re-register)                → network connectivity check
            ├─ docker network inspect                   → subnet-acl-nginx connected
            ├─ DELETE /users (teardown)                 → network removed
            ├─ POST /users?sync=true with -fc/-fn       → auto-converted + registered
            ├─ POST /users (no compose source)          → 422
            ├─ POST /users (both compose sources)       → 422
            ├─ POST /users?sync=true (default passwd)   → htpasswd + auth_basic
            ├─ POST /users?sync=true (bare project_root)→ resolves correctly
            ├─ POST /users?sync=true with build_args    → MockProxy + docker_ops.log
            ├─ POST .../rebuild?sync=true override      → override in docker_ops.log
            ├─ POST /users (async)                      → task_id, poll until complete
            ├─ GET  /tasks                              → task pool list with table
            ├─ POST /users (cancel)                     → DELETE /tasks/{id} cancelled
            ├─ POST /users?sync=true (compose svc name) → proxy_pass detection
            ├─ POST /users?sync=true with env_file_path → per-user .env copy + env_file: rewrite
            ├─ GET  /tasks/{nonexistent}                → 404
            ├─ POST /users?sync=true (hyphenated user)  → hyphen allowed, container prefix correct
            ├─ POST /users?sync=true (plain conf + https)→ auto-HTTPS generation from HTTP conf
            ├─ GET  /container-stats                    → registry-scoped container stats
            ├─ GET  /service-stats                      → registry-scoped service stats
            ├─ POST /ssl-certs + GET + DELETE           → SSL cert lifecycle
            ├─ per-task log output verification         → docker output captured in logs
            └─ nginx resilience with missing upstreams  → reload succeeds with stopped/removed containers
```

Run:
```bash
# From project root, with Docker access
sudo bash tests/test_integration.sh

# Or if your user is in the docker group
bash tests/test_integration.sh
```

### Run all tests

```bash
# All pytest-based tests (353 tests, no Docker needed)
uv run pytest tests/test_unit.py tests/test_e2e.py tests/test_proxy_support.py tests/test_task_manager.py tests/test_subnet_manager.py -v

# Full integration (120 tests, requires Docker)
sudo bash tests/test_integration.sh
```

The test creates an isolated `PROVISION_DIR` in a temp directory and tears everything
down (containers + temp dir) on exit, even on failure.

---

## Running All Tests (unit + e2e)

```bash
# Install dev dependencies
uv sync

# Run pytest
python -m pytest tests/test_unit.py tests/test_e2e.py tests/test_proxy_support.py tests/test_task_manager.py tests/test_subnet_manager.py -v
```

Expected: **353 passed** (233 unit + 43 e2e + 38 proxy + 16 task_manager + 23 subnet).

---

## Fixtures

| File | Used by |
|---|---|
| `tests/fixtures/docker-compose.template.yml.j2` | unit, e2e, integration |
| `tests/fixtures/myapp.template.nginx.conf.j2` | unit, e2e, integration |
| `tests/fixtures/docker-compose.plain.yml` | unit, e2e, integration |
| `tests/fixtures/myapp.plain.nginx.conf` | unit, e2e, integration |

`conftest.py` provides:
- `tmp_generated` — isolated temp directory acting as `generated/`
- `registry_file` — monkeypatched `REGISTRY_FILE` in a temp location
- `mock_input_yes` — patches `input()` to return `"y"` (for volume mismatch prompts)
