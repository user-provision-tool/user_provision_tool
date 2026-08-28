# Deployment

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker Engine | 24+ on the host |
| Docker Compose plugin | `docker compose` (v2) |
| Internet access at build time | To pull `python:3.13-slim`, `docker:cli`, `uv` |

---

## Container Architecture

```
  ┌─────────────────────────────────────────── host ──────────────────────────────────────────────┐
  │                                                                                                │
  │   HTTP client          ┌────────────────────────────────┐      ┌────────────────────────────┐   │
  │   (curl / app)         │   subnet-acl-provision-api     │      │   PROVISION_DIR             │   │
  │        │               │   container :8875              │◄────►│   templates/   generated/   │   │
  │        │  REST API      │                                │      └────────────────────────────┘   │
  │        └──────────────►│   docker compose ...            │                    ▲                  │
  │                        └───────────────┬────────────────┘                    │ bind mounts      │
  │                                        │ /var/run/docker.sock                │                  │
  │                                        ▼                                     │                  │
  │                        ┌──────────────────────┐       ┌─────────────────┴──────────┐           │
  │                        │   Docker daemon       │──────►│   User containers          │           │
  │                        │   (host)              │       │   e.g. web, db, ...        │           │
  │                        └──────────────────────┘       └────────────────────────────┘           │
  └────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Key points:**
- The subnet-acl-provision-api container does **not** run a Docker daemon — it uses the host daemon via the socket.
- The `PROVISION_DIR` bind mount uses the same path on both sides (`${PROVISION_DIR}:${PROVISION_DIR}`) so absolute paths in generated compose files are valid on the host.
- User containers are started as siblings of the subnet-acl-provision-api container, not children.
- `subnet-acl-nginx` is the shared ingress router. It runs as a sibling container and is dynamically connected to each user's isolated Docker network after registration so it can proxy requests to that user's containers.

---

## Environment Variables

Set these before running `docker compose up`.

| Variable | Required | Example | Description |
|---|---|---|---|
| `PROVISION_DIR` | ✓ | `/srv/provision_subnet_acl` | Base directory (default `/srv/provision_subnet_acl`); must be the same path inside and outside the container |
| `PROVISION_API_PORT` | — | `8875` | Host port for the subnet-acl-provision-api REST API (default `8875`) |
| `NGINX_HTTP_PORT` | — | `8766` | Host port for subnet-acl-nginx HTTP (default `8766`) |
| `NGINX_HTTPS_PORT` | — | `8443` | Host port for subnet-acl-nginx HTTPS (default `8443`) |
| `NGINX_STREAM_PORT` | — | `8769` | Host port for subnet-acl-nginx stream/7687 (default `8769`) |
| `NGINX_CONTAINER` | — | `subnet-acl-nginx` | Name of the nginx container to connect/reload on registration (default `subnet-acl-nginx`) |
| `SUBNET_POOLS` | — | `100.96.0.0/16,100.97.0.0/16` | Comma-separated `/16` pools for subnet management. Empty/unset = subnet management disabled |
| `SUBNET_HEADROOM` | — | `2` | Extra host IPs reserved per service (added to container count + 1 gateway when sizing a subnet; default `2`) |
| `ENABLE_ACL` | — | `true` | v5 (F7): read only by the gateway + the edge `-nginx-acl`; the `-api` no longer reads it and there is no env.d mode switch. Toggle = recreate the edge + restart the gateway (default `false`). The internal per-service conf is SIMPLE ACL-free and byte-identical across modes. See [ACL Enforcement](#acl-enforcement-enable_acl). |
| `DOCKER_OPS_LOG` | — | `${PROVISION_DIR}/generated/docker_ops.log` | If set, all docker command stdout/stderr is appended here for debugging |
| `TASK_LOG_DIR` | — | `${PROVISION_DIR}/generated/task_logs` | Directory for per-task isolated `.log` files (one file per async task) |
| `TASK_TTL_SECONDS` | — | `604800` (7 days) | How long finished tasks and their log files are retained before automatic cleanup |
| `TASK_MAX_COUNT` | — | `1000` | Maximum number of tasks retained in memory; oldest completed tasks are evicted when exceeded |
| `SSL_DIR` | — | `${PROVISION_DIR}/ssl` | Base directory for SSL certificates (default `${PROVISION_DIR}/ssl`). Created automatically. |

---

## Fullset deployment (edge + gateway + dashboard)

This document's topology and env vars describe the **minimal** deployment (provision-api + internal
`-nginx`). In the **fullset** deployment the gateway + dashboard + edge `-nginx-acl` (from the gateway
repo) are added, and the **edge becomes the sole client-facing entry**:

- The edge is published on `${ACL_HTTP_PORT:-8767}` / `${ACL_HTTPS_PORT:-8768}` (gateway compose), and
  `NGINX_HTTP_PORT` / `NGINX_HTTPS_PORT` (used by `/go/`, the service URL, and system info) are the
  **edge's** client-facing ports — not the internal nginx's.
- The internal nginx stays services-only (simple per-service confs; no host ports published in fullset).
- See `_provision_gateway/docs/architecture.md` for the fullset topology and the edge `-nginx-acl` role.

---

## `docker-compose.provision.yml` Walkthrough

```yaml
services:
  subnet-acl-provision-api:
    build:
      context: ./user_provision_tool              # builds from user_provision_tool/Dockerfile
    container_name: subnet-acl-provision-api
    ports:
      - "${PROVISION_API_PORT:-8875}:8000"        # host:container
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock   # Docker socket
      - ${PROVISION_DIR:-/srv/provision_subnet_acl}:${PROVISION_DIR:-/srv/provision_subnet_acl}  # same-path bind mount
      - ${SSL_PROVIDER_PATH:-/etc/letsencrypt}:${SSL_PROVIDER_PATH:-/etc/letsencrypt}:ro  # read-only cert provider path
    networks:
      - subnet_acl_shared
    environment:
      - SSL_PROVIDER_PATH=${SSL_PROVIDER_PATH:-/etc/letsencrypt}
      - GENERATED_DIR=${PROVISION_DIR:-/srv/provision_subnet_acl}/generated        # nginx conf, htpasswd, registry
      - USER_DATA_DIR=${PROVISION_DIR:-/srv/provision_subnet_acl}/user_data         # auto-created per-user volume dirs
      - SOURCE_PROJECTS_DIR=${PROVISION_DIR:-/srv/provision_subnet_acl}/source_projects  # operator repo drop zone; bare
                                                                                          # project_root names resolve here
      - REGISTRY_FILE=${PROVISION_DIR:-/srv/provision_subnet_acl}/generated/user_registry.yml
      - NGINX_CONTAINER=subnet-acl-nginx          # which container to connect/reload
      - DOCKER_OPS_LOG=${PROVISION_DIR:-/srv/provision_subnet_acl}/generated/docker_ops.log  # optional debug log
      - TASK_LOG_DIR=${PROVISION_DIR:-/srv/provision_subnet_acl}/generated/task_logs          # per-task isolated logs
      - TASK_TTL_SECONDS=${TASK_TTL_SECONDS:-604800}    # keep finished tasks 7 days
      - TASK_MAX_COUNT=${TASK_MAX_COUNT:-1000}          # max tasks in memory
      - SSL_DIR=${PROVISION_DIR:-/srv/provision_subnet_acl}/ssl              # base directory for TLS certificates
      - SUBNET_POOLS=${SUBNET_POOLS:-}                  # comma-separated /16 pools; empty = disabled
      - SUBNET_HEADROOM=${SUBNET_HEADROOM:-2}           # headroom IPs per service
      - ENABLE_ACL=${ENABLE_ACL:-false}                 # v5: -api no longer reads it (env.d mode switch removed, F7/F10); gateway+edge read it
    dns:
      - 8.8.8.8
      - 8.8.4.4
    restart: unless-stopped

  subnet-acl-nginx:
    image: nginx:alpine
    container_name: subnet-acl-nginx
    ports:
      - "${NGINX_HTTP_PORT:-8766}:80"             # HTTP
      - "${NGINX_HTTPS_PORT:-8443}:443"           # HTTPS
      - "${NGINX_STREAM_PORT:-8769}:7687"         # stream / 7687
    volumes:
      - ${PROVISION_DIR:-/srv/provision_subnet_acl}:${PROVISION_DIR:-/srv/provision_subnet_acl}:ro    # read-only; htpasswd + ssl paths must resolve
      - ./nginx.provision.conf:/etc/nginx/nginx.provision.conf:ro
    networks:
      - subnet_acl_shared
    environment:
      - GENERATED_DIR=${PROVISION_DIR:-/srv/provision_subnet_acl}/generated
    # envsubst replaces only $GENERATED_DIR; all nginx $variables are left intact
    command: >
      /bin/sh -c "envsubst '$$GENERATED_DIR' < /etc/nginx/nginx.provision.conf
                  > /etc/nginx/nginx.conf && nginx -g 'daemon off;'"
    restart: unless-stopped

networks:
  subnet_acl_shared:
    external: true
    name: subnet_acl_shared
```

The `nginx.provision.conf` includes all per-user virtual-host confs at startup:

```nginx
# nginx.provision.conf (simplified, v5 services-only — F9)
http {
    include ${GENERATED_DIR}/*.nginx.conf;   # ← envsubst fills GENERATED_DIR
}
```

> The legacy `map $http_accept $is_browser` discriminator was **removed in v4 (QA2)** — the per-service
> confs no longer reference `$is_browser`. Client-type detection (browser vs API) is decided by the
> gateway `/api/auth/verify` hybrid rule (`X-Client-Type`) instead. In v5 (cycle 20260824T173309Z, F9)
> the internal `nginx.provision.conf` is **services-only**: the v4 `map $host $dashboard_host` block
> ("Dashboard host:port for ACL redirect targets") was **REMOVED** — portal/ACL redirects and the
> 401/403 challenges now live on the edge `-nginx-acl` (F4/F8), and the internal per-service confs are
> simple ACL-free and never reference `$dashboard_host`.

Each `*.nginx.conf` file is written by subnet-acl-provision-api when a user registers. After writing
the file, subnet-acl-provision-api calls `docker exec subnet-acl-nginx nginx -s reload` so the new
virtual host takes effect immediately without a container restart.

---

## Nginx Routing

`subnet-acl-nginx` is an `nginx:alpine` container defined in `docker-compose.provision.yml`.
It is the single ingress point for all HTTP traffic to all user containers.

### How routing works

```
HTTP request
  Host: myapp-alice-0.example.com
        │
        ▼
  subnet-acl-nginx  (host port 8766)
        │
        │  resolver 127.0.0.11 (Docker embedded DNS)
        │  nginx matches server_name in GENERATED_DIR/myapp.user-alice.0.nginx.conf
        │
        ▼
  proxy_pass  http://$upstream_0000
              (variable-based → DNS resolved per-request via 127.0.0.11)
              (reachable because nginx is connected to the myapp-user_alice-0 network)
```

Routing is virtual-host based (matched by the `Host:` header / `server_name` directive).
Each registered user gets their own `*.nginx.conf` in `GENERATED_DIR`.

Per-user nginx confs use variable-based `proxy_pass` (`set $upstream_XXXX host:port; proxy_pass http://$upstream_XXXX;`) so that DNS resolution is deferred to request time via the Docker embedded DNS resolver (`127.0.0.11`). This allows nginx to start and reload cleanly even when upstream containers are stopped or missing — no more hanging on `nginx -s reload`.

### Config loading

`nginx.provision.conf` is mounted read-only into the container. At container startup,
`envsubst` substitutes `$GENERATED_DIR` to produce `/etc/nginx/nginx.conf`:

```nginx
http {
    include ${GENERATED_DIR}/*.nginx.conf;   # expands to e.g. /srv/provision_subnet_acl/generated/*.nginx.conf
}
```

Only `$GENERATED_DIR` is substituted; all nginx `$variables` (e.g. `$host`, `$remote_addr`)
are left intact by the `envsubst '$$GENERATED_DIR'` invocation.

### Dynamic updates

When subnet-acl-provision-api registers or removes a user's service instance:

1. It writes (or deletes) the user's `*.nginx.conf` in `GENERATED_DIR`.
2. It runs `docker exec subnet-acl-nginx nginx -s reload` — nginx picks up the new conf
   without a container restart.
3. It calls `docker network connect {network_name} subnet-acl-nginx` (register) or
   `docker network disconnect` (remove) so nginx can reach the user's containers.

### Why user containers don't bind ports

The compose converter strips the `ports:` key from all services in user compose files.
All traffic flows through `subnet-acl-nginx`. This avoids host port conflicts between users
running the same service type, and keeps user services unreachable except through nginx.

---

## Subnet Management

When `SUBNET_POOLS` is set (a comma-separated list of `/16` pools), every registered service
gets its own dedicated subnet reserved from the pool instead of Docker auto-assigning one:

- **Dynamic sizing** — the smallest `/30`..`/24` that fits `containers + SUBNET_HEADROOM + 1 (gateway)`
  is selected. A single-container service needs `1 + 2 + 1 = 4` usable IPs, which requires a `/29`
  (6 usable) — the `/29` minimum ensures `subnet-acl-nginx` always has an address on the network.
- **Bitmap allocator with alignment** — subnets are carved from the pool with alignment to their
  own size so no partial overlaps occur. Already-allocated registry subnets and live Docker
  networks inside the pools are marked occupied.
- **Exhaustion** — if every pool is full, registration raises `RuntimeError` (surfaced as `500`
  in sync mode / the task's `error` field in async mode) instead of silently falling back to
  Docker auto-assign.
- **Registry tracking** — each entry records its `subnet` / `gateway`. `GET /subnet-pool` reports
  `{enabled, pools[], overall, allocations, headroom}` for the dashboard.
- **Auto IPAM injection** — when pools are enabled, `ensure_subnet_ipam_block()` re-converts old
  compose templates that lack the `{% if subnet %}` ipam block (backing the original up as `.bak`),
  so the reserved subnet always renders into the generated compose file. The injected block is:

  ```yaml
  {% if subnet %}
      ipam:
        config:
          - subnet: {{ subnet }}
            gateway: {{ gateway }}
  {% endif %}
  ```

---

## ACL Enforcement (`ENABLE_ACL`)

**v5 model (cycle 20260824T173309Z, F8):** `ENABLE_ACL` no longer changes the per-service nginx
template and no longer drives an env.d mode switch. `render_nginx_conf()` always renders the SIMPLE
ACL-free form (`server_name` + `auth_basic` + variable `proxy_pass`) — byte-identical for
`ENABLE_ACL` true/false (`TestV5SimpleNginxSyntax`: no auth_request/WWW-Authenticate/`@auth_401`/
`@auth_403`/env.d/`$client_type`). The v4 server scaffold, `env.d` one-liner and `/__basic__/`
short-circuit are REMOVED; `strip_v4_scaffold` sweeps stale v4 tokens from deployed confs.

- `ENABLE_ACL=true` → ACL mode: the edge `-nginx-acl` (gateway repo) is the entry; its `location /`
  runs `auth_request /_auth_jwt` → gateway `/api/auth/verify` (v4 hybrid `X-Client-Type` rule;
  `error_page 401/403` → browser 302 dashboard, API `401`/`403`; `WWW-Authenticate:
  Basic realm="subnet-acl"` `always` on 401, GAP-16).
- `ENABLE_ACL=false` (the default) → ACL-off mode: the edge passes traffic straight through to the
  internal native Basic (`auth_basic` on the simple per-service conf) — non-certed service over
  http → 401 native Basic dialog (`realm="example-service - alice"`, B5/F6).
- `/_set_token` on the edge is a plain variable proxy to the gateway exchange (`/api/auth/exchange`);
  it swaps the 30s HMAC exchange code for the `provision_token` cookie and redirects with a
  port-preserving `return 302 $scheme://$http_host$arg_redirect;` so the `/go/` flow lands on the
  same host:port the browser came from. **No JWT appears in any URL.**
- The legacy `map $http_accept $is_browser` discriminator was removed in v4 (QA2); nginx confs no
  longer reference `$is_browser`. Client-type is decided by the gateway verify hybrid rule.

Deployed confs are kept in sync via `reconciliation.regenerate_nginx_confs()` (re-renders registry
confs with the v5 SIMPLE renderer and `strip_v4_scaffold` strips stale v4 tokens; `POST
/nginx/regenerate`). Migrate existing confs with `migrate_v5.py`
(`cd _users_provision && uv run python -m user_provision_tool.migrate_v5 --dry-run`, F15).

---

## Start / Stop

```bash
# 1. Set required variables
export PROVISION_DIR=/srv/provision_subnet_acl
export PROVISION_API_PORT=8875

# 2. Create the provision directory structure
mkdir -p $PROVISION_DIR/generated $PROVISION_DIR/source_projects $PROVISION_DIR/user_data

# 3. Start (builds image on first run)
docker compose -f docker-compose.provision.yml up -d --build

# 4. Check it is running
curl http://localhost:8875/health
# → {"status": "ok"}

# 5. Stop
docker compose -f docker-compose.provision.yml down
```

---

## Directory Layout at Runtime

```
PROVISION_DIR/
├── source_projects/              ← your service source trees (bind-mounted same-path)
│   └── myapp/                        ← bare project_root "myapp" resolves here
│       ├── Dockerfile
│       ├── docker-compose.myapp.yml.j2    ← compose template (you provide, or auto-generated)
│       ├── myapp.nginx.conf.j2            ← nginx template   (you provide, or auto-generated)
│       ├── myapp.env                      ← runtime secrets   (you provide)
│       ├── .env.alice.0                   ← per-user env copy (written by tool)
│       └── docker-compose.user-alice.0.yml ← rendered per-user compose (written by tool)
│
├── user_data/                    ← per-user volume directories (auto-created by tool)
│   └── alice/
│       └── myapp/
│           └── 0/
│               ├── app_data/
│               └── db_data/
│
└── generated/                    ← written by subnet-acl-provision-api
    ├── user_registry.yml
    ├── myapp.user-alice.0.nginx.conf
    └── myapp.user-alice.0.htpasswd
```

> **Path note**: `source_projects/` inside the container is the same absolute path on the
> host because of the same-path bind mount (`${PROVISION_DIR}:${PROVISION_DIR}`). When you
> pass `project_root: "myapp"` (bare name) to the API or `-pr myapp` to the CLI, it resolves
> to `SOURCE_PROJECTS_DIR/myapp` = `$PROVISION_DIR/source_projects/myapp` on both sides.

---

## Upgrading

To update the subnet-acl-provision-api image after a code change:

```bash
docker compose -f docker-compose.provision.yml up -d --build --force-recreate
```

User containers are unaffected — they are managed independently by the Docker daemon.

---

## Dockerfile Notes

The Dockerfile uses a multi-stage build to pull the Docker CLI binary from
`docker:cli` (Docker Hub) and `uv` from `ghcr.io/astral-sh/uv`. Neither copies
anything from the host filesystem. The docker binary in `docker:cli` is statically
linked (Alpine/musl) and runs on any Linux.

Dependencies are installed via `uv sync` into `.venv/` during the build step.
At runtime the container starts `uvicorn` directly from `.venv/bin/uvicorn` to avoid
the package-sync delay that `uv run` introduces.

The `docker-buildx` plugin is copied alongside `docker-compose` because Docker
29+ requires it when BuildKit is the active builder. Without it, `docker build`
(and `docker compose build`) would fail inside the container.

```
FROM python:3.13-slim
  ├─ COPY --from=docker:cli       → /usr/local/bin/docker
  │                                  /usr/local/libexec/docker/cli-plugins/docker-compose
  │                                  /usr/local/libexec/docker/cli-plugins/docker-buildx
  ├─ COPY --from=ghcr.io/.../uv  → /usr/local/bin/uv
  ├─ COPY pyproject.toml uv.lock → uv sync (install deps into .venv/)
  ├─ COPY lib/ cli/ api.py
  └─ CMD [".venv/bin/uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## VS Code AI Skill

A [provision-api skill](../skills/provision-api/SKILL.md) is available for AI-assisted
setup. It provides:

- Ready-to-use curl command snippets for the REST API
- Templates for `docker-compose.yml` and `nginx.conf` when a target repo has only a Dockerfile
- Template variable reference for writing `.j2` templates
