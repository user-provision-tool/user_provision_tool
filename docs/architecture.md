# Architecture: User Containers Provision Tool

## High-Level Diagram

```mermaid
%%{init: {"flowchart": {"defaultRenderer": "elk"}, "elk": {"nodePlacementStrategy": "NETWORK_SIMPLEX", "edgeRouting": "SPLINES"}} }%%
flowchart LR
    operator(["Operator"])
    end_user(["End User\nbrowser / curl"])

    subgraph host["Docker Host"]
        subgraph upt["User Provision Tool"]
            direction TB
            provision_api["subnet-acl-provision-api\nFastAPI · :8875"]
            provision_nginx["subnet-acl-nginx\nnginx · :8766 & :8443"]
        end

        docker_daemon["Docker Daemon"]
        provision_dir[("PROVISION_DIR\nregistry · templates · confs")]
        user_nets["User Containers\nper-user Docker networks"]
    end

    operator -->|"REST API / CLI"| provision_api
    end_user -->|"HTTP · Host header"| provision_nginx

    provision_api -->|"compose up/down\nnetwork connect"| docker_daemon
    provision_api -->|"write registry & configs"| provision_dir

    provision_nginx -->|"read *.nginx.conf"| provision_dir
    provision_nginx -->|"proxy_pass"| user_nets

    docker_daemon -->|"start · stop · build"| user_nets
    user_nets -->|"bind mounts"| provision_dir

    style upt fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
```


## Directory Layout

```
user_provision_tool/
├── api.py                         # FastAPI REST service (primary runtime entry point)
├── docker-compose.provision.yml   # Runs the subnet-acl-provision-api container itself
├── Dockerfile                     # Builds the subnet-acl-provision-api image
├── pyproject.toml / uv.lock       # Python dependencies (managed via uv)
│
├── cli/                           # CLI entry points (direct/scripted use)
│   ├── __init__.py
│   ├── register.py                # Register user + start containers
│   ├── remove.py                  # Stop + deregister a user's service
│   ├── rebuild.py                 # Rebuild user containers
│   ├── status.py                  # Query container health
│   ├── gen_compose_template.py    # Convert plain compose file → .j2 template
│   └── gen_nginx_template.py      # Convert plain nginx conf  → .j2 template
│
├── lib/                           # Shared library modules
│   ├── __init__.py
│   ├── validation.py              # Name/label regex validation
│   ├── registry.py                # CRUD on user_registry.yml
│   ├── template_engine.py         # Jinja2 compose + nginx rendering
│   ├── auth.py                    # Password hashing (passlib/bcrypt)
│   ├── docker_ops.py              # Subprocess wrappers for docker compose
│   ├── provisioner.py             # Shared registration / removal / rebuild workflow
│   ├── compose_converter.py       # Plain docker-compose.yml → Jinja2 template; injects {% if subnet %} ipam block
│   ├── nginx_converter.py         # Plain nginx conf → Jinja2 template; injects _set_token + ACL locations
│   ├── subnet_manager.py          # Per-service subnet sizing + bitmap allocation from SUBNET_POOLS
│   ├── task_manager.py            # Async task pool (ThreadPoolExecutor) for background Docker ops
│   └── yaml_utils.py              # Shared IndentedDumper for consistent YAML output
│
├── source_projects/               # SOURCE_PROJECTS_DIR = $PROVISION_DIR/source_projects
│   │                              # Bare project_root name "myapp" → source_projects/myapp/
│   └── {project}/
│       ├── Dockerfile
│       ├── docker-compose.{project}.yml.j2        # compose template
│       └── docker-compose.user-{user}.{label}.yml # rendered per-user compose
│
├── generated/                     # GENERATED_DIR = $PROVISION_DIR/generated (auto-created)
│   ├── user_registry.yml          # Managed state file
│   ├── task_logs/                 # Per-task isolated log files (TASK_LOG_DIR)
│   │   └── task-{task_id}.log     # task output, streamed via SSE
│   ├── {svc}.user-{user}-{label}.nginx.conf
│   └── {svc}.user-{user}-{label}.htpasswd
│
└── tests/
    ├── conftest.py                # Shared pytest fixtures
│   ├── test_unit.py               # Unit tests
│   ├── test_e2e.py                # End-to-end pytest tests
│   ├── test_proxy_support.py      # Proxy / --build-arg tests
│   ├── test_task_manager.py       # Async task pool tests
│   ├── test_subnet_manager.py     # Subnet sizing / allocation / pool stats tests
│   ├── test_integration.sh        # Full Docker integration test
│   ├── mock_proxy.py              # Forward HTTP/HTTPS proxy for integration tests
    └── fixtures/
        ├── docker-compose.template.yml.j2
        ├── myapp.template.nginx.conf.j2
        ├── docker-compose.plain.yml
        └── myapp.plain.nginx.conf
```

---

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `validation.py` | Enforce `[a-zA-Z0-9_]` for names, `[0-9]` for label; raise `ValidationError` |
| `registry.py` | Load/save `user_registry.yml`; add/remove/query entries by user+service+label |
| `template_engine.py` | Extract template volumes; render compose and nginx files via Jinja2; copy `.env` as per-user file + rewrite `env_file:` refs; rewrite static `proxy_pass` → variable-based (`set $upstream_XXXX` + `proxy_pass http://$upstream_XXXX`) for per-request DNS resolution so nginx reloads cleanly even with missing upstream containers; pass `subnet` / `gateway` into compose templates (guarded by `{% if subnet %}`); render the SIMPLE ACL-free per-service conf (`server_name` + `auth_basic` + variable `proxy_pass`, byte-identical across `ENABLE_ACL` — no v4 scaffold, no `ENABLE_ACL` branch, F8) and `strip_v4_scaffold` stale v4 tokens; the `_set_token`/`_auth_jwt` relay lives on the edge `-nginx-acl` (F3) |
| `auth.py` | `getpass` prompt; bcrypt hash via `passlib.hash.bcrypt`; write `.htpasswd` file |
| `docker_ops.py` | `compose_up`, `compose_down`, `compose_stop`, `compose_build`, `docker_ps`, `docker_ps_all`, `docker_stats_snapshot`, `docker_info`, `network_connect`, `network_disconnect`, `network_list`, `network_inspect`, `nginx_reload`, `container_inspect`, `container_exists`, `container_running`, `network_connected_to_container`, `container_logs`, `orphan_network_cleanup` wrappers; real-time stdout/stderr via `subprocess.Popen` + threading; supports `--build-arg` for proxy; writes to `DOCKER_OPS_LOG` file when env var is set; thread-local per-task log file support (`set_task_log_file` / `clear_task_log_file`) with parent-thread path capture to fix child-thread logging; uses `docker container inspect` (explicit type) to avoid image-name ambiguity |
| `provisioner.py` | Shared workflow for register/remove/rebuild/start_service/stop_service/change_password; supports `build_args` (proxy) passed through to docker_ops; orphan network cleanup on remove; calls `ensure_subnet_ipam_block()` (when pools are enabled) and `subnet_manager.allocate_subnet()` at registration; records `subnet` / `gateway` in the registry entry; both `api.py` and `cli/` delegate here |
| `compose_converter.py` | Parse a plain `docker-compose.yml` and emit a Jinja2 `.yml.j2` template; services with named profiles are excluded; `profiles:` key is stripped from kept services; Docker socket paths (`/var/run/docker.sock`, `/run/docker.sock`) are preserved as literal host paths — never converted to per-user volume variables; injects the `{% if subnet %}` ipam block after the network `name:` line; `ensure_subnet_ipam_block()` re-injects it into old templates (backing up as `.bak`) when subnet pools are enabled |
| `nginx_converter.py` | Apply regex substitutions to a plain nginx conf and emit a `.j2` template; injects `auth_basic` + `auth_basic_user_file` directives before the first `proxy_pass` if none are already present; detects when a `proxy_pass` host matches a compose service name and rewrites it to `{{ container_prefix }}<name>`; normalizes legacy `return 302 $arg_redirect;` → `$scheme://$http_host$arg_redirect;` (v5: no longer injects `/_set_token`/`/_auth_jwt`/ACL directives into internal confs — those moved to the edge, F3/F8) |
| `subnet_manager.py` | Per-service subnet allocation engine: sizes the smallest `/30`..`/24` fitting `containers + SUBNET_HEADROOM + 1(gateway)`; bitmap allocator with alignment over `/16` pools (`SUBNET_POOLS`); `RuntimeError` on exhaustion; discovers registry + live Docker subnets; `get_pool_stats()` powers `GET /subnet-pool` |
| `task_manager.py` | In-memory async task pool (`ThreadPoolExecutor`); submit → status → cancel lifecycle; powers `GET /tasks`, `GET /tasks/{id}`, `DELETE /tasks/{id}` endpoints; each task writes to an isolated per-task log file at `$TASK_LOG_DIR/task-{task_id}.log`; configurable TTL (`TASK_TTL_SECONDS`, default 7 days) and max count (`TASK_MAX_COUNT`, default 1000) with automatic eviction of oldest tasks |
| `yaml_utils.py` | Shared `IndentedDumper` class (extends `yaml.Dumper`) that always indents sequence items under their parent key; used by `compose_converter` and `template_engine` for consistent YAML serialisation |

---

## Module Dependencies

```
  api.py                →  task_manager → provisioner → validation, registry, template_engine, auth, docker_ops
  api.py                →  subnet_manager (GET /subnet-pool, status subnet discovery)
  api.py (SSE /log)     →  task_manager (reads task-{id}.log from TASK_LOG_DIR)
  provisioner.py        →  subnet_manager (allocate_subnet), compose_converter (ensure_subnet_ipam_block)
  cli/register.py       →  provisioner  →  (same)
  cli/remove.py         →  provisioner  →  registry, docker_ops
  cli/rebuild.py        →  provisioner  →  registry, docker_ops
  cli/status.py         →               →  registry, docker_ops, template_engine
  cli/gen_compose_template.py  →  compose_converter
  cli/gen_nginx_template.py    →  nginx_converter
```

---

## Data Flows

### Registration (API or CLI)

```
Input: user_name, service_name, label, volumes, passwd, template paths, env_file?
  │
  ├─ validation.py ── validate names and label format
  │
  ├─ template_engine.py ── extract declared volume keys from template
  │       └─ volumes mismatch? → CLI warns + prompts; API rejects with 400
  │
  ├─ provisioner.register_user()  ← single entry point for both CLI and API
  │       │
  │       ├─ auth.py ── hash password → bcrypt hash
  │       │
  │       ├─ subnet_manager.py ── (when SUBNET_POOLS set) allocate_subnet()
  │       │       └─ smallest /30../24 fitting containers + SUBNET_HEADROOM + 1(gateway)
  │       │       └─ bitmap allocator with alignment; RuntimeError on pool exhaustion
  │       │       └─ compose_converter.ensure_subnet_ipam_block() re-injects
  │       │          {% if subnet %} ipam block into old templates (backup as .bak)
  │       │
  │       ├─ registry.py ── append entry to user_registry.yml (incl. subnet + gateway)
  │       │
  │       ├─ template_engine.py ── render docker-compose.user-{user}.{label}.yml
  │       │       └─ written into project root (source dir, next to Dockerfile)
  │       │       └─ subnet/gateway passed to template ({% if subnet %} ipam block)
  │       │       └─ env_file provided? → copy as .env.{user}.{label} + rewrite env_file: .env refs
  │       │
  │       ├─ template_engine.py ── render {svc}.user-{user}.{label}.nginx.conf  (optional)
  │       │       └─ written into GENERATED_DIR
  │       │       └─ v5: renders the SIMPLE ACL-free conf (byte-identical across ENABLE_ACL);
  │       │          no v4 scaffold/env.d — ACL gate lives on the edge -nginx-acl (F8)
  │       │       └─ auth.py ── write .htpasswd into GENERATED_DIR
  │       │
  │       ├─ docker_ops.py ── docker compose -f <compose> --project-name <network_name> [--env-file <env>] up -d
  │       │
  │       └─ docker_ops.py ── network_connect + nginx_reload
  │
  └─ optional pre-step: compose_converter / nginx_converter
          └─ triggered by -fc / -fn flags; converts plain files → .j2 before registration
```

### Removal

```
Input: user_name, service_name, label
  │
  ├─ registry.py ── look up compose_file_path + env_file_path + network_name
  │
  ├─ docker_ops.py ── docker compose --project-name <network_name> down
  │
  ├─ docker_ops.py ── orphan_network_cleanup (if only nginx left on network)
  │
  ├─ docker_ops.py ── nginx_reload
  │
  └─ registry.py ── remove entry from user_registry.yml
```

### Rebuild

```
Input: user_name, service_name, label
  │
  ├─ registry.py ── look up compose_file_path + env_file_path
  │
  ├─ docker_ops.py ── docker compose --project-name <network_name> build
  │
  └─ docker_ops.py ── docker compose --project-name <network_name> up -d
```

---

## Naming Conventions

| Artifact | Pattern |
|---|---|
| Compose file | `docker-compose.user-{user_name}.{label}.yml` |
| Nginx conf | `{service_name}.user-{user_name}.{label}.nginx.conf` |
| htpasswd file | `{service_name}.user-{user_name}.{label}.htpasswd` |
| Copied env file | `.env.{user_name}.{label}` (placed next to compose file) |
| Container prefix | `{service_name}-user_{user_name}-{label}-` |
| Nginx hostname | `{service_name}-{user_name}-{label}.{domain_name}` |

---

## Key Design Decisions

1. **`cli/` package** — all four CLI scripts live under `cli/` and share `lib/` with no logic duplication. The `api.py` is the preferred runtime entry point.
2. **`.j2` template extension** — compose and nginx templates use the `.j2` suffix so YAML linters do not flag Jinja2 placeholders as syntax errors.
3. **Two placeholder types in templates** — `{{ var }}` is resolved by Jinja2 at render time; `${ENV_VAR}` is passed through as literal text and resolved by `docker compose` at runtime via `--env-file`.
4. **Docker socket pattern** — the subnet-acl-provision-api container mounts `/var/run/docker.sock` and runs `docker compose` without `sudo`. No Docker daemon is installed inside the container; only the CLI binary is present.
5. **Same-path bind mount** — `${PROVISION_DIR}:${PROVISION_DIR}` ensures the absolute paths written into generated compose files are valid on the host where the Docker daemon runs. It also means a bare `project_root` name like `"myapp"` resolves to `SOURCE_PROJECTS_DIR/myapp` (`$PROVISION_DIR/source_projects/myapp` by default) — the same absolute path both inside the container and on the host.
6. **`passlib.hash.bcrypt`** — passwords are hashed with `bcrypt.using(rounds=12).hash()`; hashes are stored in `user_registry.yml` and written into `.htpasswd` files for nginx basic auth.
7. **`user_registry.yml` as source of truth** — `cli/status.py` and `GET /users` cross-reference live `docker ps` output against registry entries to compute per-service health.
8. **Docker Compose project isolation** — every `compose_up`, `compose_down`, and `compose_build` call passes `--project-name {network_name}`. Because all rendered compose files share the same source directory, omitting this would cause Compose to infer the same project name for all users and tear down one user's containers when starting another's.
9. **BuildKit enabled in subprocesses** — all `docker` subprocess calls inherit `DOCKER_BUILDKIT=1` from `os.environ`. This is required for Docker 29+ (where BuildKit is the default builder) and enables `--mount=type=cache` and other BuildKit Dockerfile features.
10. **`subnet-acl-nginx` as shared ingress** — user containers never bind host ports (`ports:` is stripped from compose templates). All HTTP traffic enters through the `subnet-acl-nginx` sibling container, which routes by virtual host (`Host:` header → `server_name`). After every registration or removal, subnet-acl-provision-api connects/disconnects nginx to the user's isolated Docker network and calls `nginx -s reload` to update routing without a container restart.
11. **Variable-based `proxy_pass` with Docker DNS resolver** — per-user nginx confs use `set $upstream_XXXX host:port; proxy_pass http://$upstream_XXXX;` instead of static `proxy_pass http://host:port;`. This defers DNS resolution to request time via the Docker embedded DNS resolver (`127.0.0.11`), so nginx starts and reloads cleanly even when upstream containers are stopped or missing. The `resolver 127.0.0.11 valid=30s ipv6=off;` directive is configured in `nginx.provision.conf`.
12. **Per-service subnet management (`SUBNET_POOLS`)** — when `/16` pools are configured, every service gets a dedicated subnet sized to `containers + SUBNET_HEADROOM + 1(gateway)` (`/30`..`/24`, `/29` minimum so nginx can join). A bitmap allocator with alignment prevents overlaps; exhaustion raises `RuntimeError` instead of silently falling back to Docker auto-assign. `ensure_subnet_ipam_block()` re-converts old templates (backup `.bak`) so the reserved subnet always renders. Registry entries track `subnet` / `gateway`.
13. **`ENABLE_ACL` no longer touches the internal template (v5)** — the v4 server scaffold (env.d mode one-liner, auth_request `/_auth_jwt`, `@auth_401/@auth_403`, `location /__basic__/`) is REMOVED. Internal per-service confs are SIMPLE and ACL-free (`server_name` + `auth_basic` + variable `proxy_pass`), byte-identical across `ENABLE_ACL` (F8). `ENABLE_ACL` is read only by the gateway + the edge `-nginx-acl` (F7); ACL enforcement (`/_auth_jwt` → gateway `/api/auth/verify`, F3), portal routing (F4) and the 401/403 challenges (F3) live on the edge. `/_set_token` on the edge is a plain variable proxy to the gateway exchange, returning `$scheme://$http_host$arg_redirect` so the `/go/` cookie flow lands on the same host port — no JWT in any URL.

---

## Status Model

```
Registry entries for user
        │
        ▼
For each entry → expected containers = services declared in compose template
        │
        ├─ docker ps match, status "Up"           → healthy_containers
        ├─ docker ps match, status contains error  → unhealthy_containers
        └─ not found in docker ps output           → missing_containers

Service health = "healthy"  iff  healthy == expected  AND  unhealthy + missing == 0
```

### Status Response Schema

```json
{
  "user_status": [
    {
      "user_name": "alice",
      "summary": {
        "expected_services_#": 2,
        "healthy_services_#": 1,
        "unhealthy_services_#": 0
      },
      "healthy_services": [
        {
          "service_name": "myapp",
          "label": "0",
          "compose_file_path": "/srv/provision_subnet_acl/generated/docker-compose.myapp-user_alice-0.yml",
          "healthy_containers": { "myapp-user_alice-0-web": "Up 2 hours" },
          "unhealthy_containers": {},
          "missing_containers": {}
        }
      ],
      "unhealthy_services": [],
      "missing_services": []
    }
  ]
}
```
