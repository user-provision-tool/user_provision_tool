# User Provision Tool

Give each user their own isolated copy of a service — with one API call.
No template prep required: just point to your existing `docker-compose.yml` and nginx conf.

## What it does

Imagine a shared server that can instantly spin up a private workspace for any new user —
with its own containers, database, and dedicated web address — and tear it all down just as
quickly. No manual setup, no port conflicts, no data leaking between users.

Technically: you drop a plain `docker-compose.yml` (and optionally a plain nginx conf) into
a source project directory. When a user registers, the tool:

1. **Auto-converts** your plain files into per-user Jinja2 templates (once, on first use)
2. **Renders** isolated `docker-compose.user-{user}.{label}.yml` and `*.nginx.conf` for that user
3. **Starts** the containers with `docker compose up --project-name {isolated-name}`
4. **Routes** HTTP/HTTPS traffic by connecting `subnet-acl-nginx` to the user's Docker network and reloading nginx live
5. **Supports TLS** — pass `--https` with certificate paths and the tool copies certs, renders HTTPS server blocks, and enables SSL termination
6. **Tracks** state in `user_registry.yml` — remove a user's service and its containers are torn down cleanly

Per-user container names: `{service}-user_{user}-{label}-{svc}`
Per-user hostnames: `{service}-{user}-{label}.{domain}`

```mermaid
flowchart LR
    operator(["Operator"])
    end_user(["End User"])

    input["Source project"]
    conv["① auto convert\ncompose · nginx → .j2"]
    render["② render per-user\ncompose · nginx · htpasswd"]
    compose["③ compose up\nper user isolated service"]
    route["④ nginx connect\n+ reload"]

    operator -->|"POST /users"| input
    input --> conv --> render --> compose --> route
    end_user -->|"HTTP"| route

    style conv    fill:#dbeafe,stroke:#3b82f6
    style render  fill:#dbeafe,stroke:#3b82f6
    style compose fill:#dbeafe,stroke:#3b82f6
    style route   fill:#dbeafe,stroke:#3b82f6
```

### What it is

A **provisioner**: given a Docker Compose stack, it stamps out one isolated, routed copy per user — on a single Docker host — via a single API call.

- **Your users are tenants**, not operators. They never touch Docker or the server.
- **Your service is a compose file** you already have. No rewrite into k8s manifests or job specs.
- **Your host is one machine.** You want simplicity, not a cluster.

### What it is not

- Not a multi-node scheduler — all containers run on the same host
- Not a general-purpose PaaS — it does one thing: provision and tear down per-user stacks

### How it compares

| Tool | Target user | Single-call tenant provisioning | Built-in routing | Multi-node | Complexity |
|---|---|---|---|---|---|
| **user_provision_tool** | Your end-customers / tenants | ✅ | ✅ nginx — per-user-service conf, hot-reloaded | ❌ single host | low |
| **Coolify** | Developers / operators | ❌ operator-scoped | ✅ Traefik or Caddy via Docker labels | ❌ single host | low |
| **Docker Swarm** | Operators | ❌ you script it | ❌ none built-in | ✅ | medium |
| **Nomad** | Operators | ❌ you script it | ❌ needs Consul Connect | ✅ | medium |
| **Kubernetes** | Operators | ❌ you script it | ✅ ingress controllers | ✅ | high |

---

## Quick Start (API)

**1. Set up the provision directory and drop in your service**
```bash
export PROVISION_DIR=/srv/provision_subnet_acl
mkdir -p $PROVISION_DIR/{generated,ssl,source_projects/myapp}
# copy your service into source_projects/myapp/  (Dockerfile, docker-compose.yml, nginx.conf, .env, ...)
```

**2. Start the provision service**
```bash
docker compose -f docker-compose.provision.yml up -d --build
```

**3. Register a user — just the service name and filenames**

*Async (default) — returns task_id immediately, work runs in background:*
```bash
curl -X POST http://localhost:8875/users \
  -H 'Content-Type: application/json' \
  -d '{
    "user_name": "alice",
    "service_name": "myapp",
    "project_root": "myapp",
    "compose_file_path": "docker-compose.yml",
    "nginx_conf_file_path": "nginx.conf",
    "env_file_path": ".env",
    "domain": "example.com",
    "passwd": "secret"
  }'
# → {"task_id": "a1b2c3d4e5f6", "status": "pending", "type": "register"}
```

*Sync (blocking) — add ?sync=true:*
```bash
curl -X POST "http://localhost:8875/users?sync=true" \
  -H 'Content-Type: application/json' \
  -d '{...}'
# → {"status": "registered", "entry": {...}, "copied_env": ".../.env.alice.0"}
```

**4. Poll task status or check all tasks**
```bash
curl http://localhost:8875/tasks/a1b2c3d4e5f6
# → {"task_id": "a1b2c3d4e5f6", "status": "completed", "result": {...}}

curl http://localhost:8875/tasks
# → {"count": 3, "tasks": [...]}

curl -X DELETE http://localhost:8875/tasks/a1b2c3d4e5f6
# → cancel a pending/running task
```

**5. Check user status**
```bash
curl http://localhost:8875/users/alice
```

**6. Rebuild (with proxy build args)**
```bash
curl -X POST "http://localhost:8875/users/alice/services/myapp/0/rebuild?sync=true" \
  -H 'Content-Type: application/json' \
  -d '{"no_cache": true, "build_args": {"HTTP_PROXY": "http://proxy:8080"}}'
```

**7. Remove**
```bash
curl -X DELETE "http://localhost:8875/users/alice/services/myapp/0?sync=true"
```

**8. Start / stop a service**
```bash
curl -X POST http://localhost:8875/users/alice/services/myapp/0/up
curl -X POST http://localhost:8875/users/alice/services/myapp/0/down
```

**9. Change password**
```bash
curl -X PUT http://localhost:8875/users/alice/services/myapp/0/password \
  -H 'Content-Type: application/json' -d '{"passwd": "newsecret"}'
```

**10. Container logs**
```bash
curl "http://localhost:8875/users/alice/services/myapp/0/containers/web/logs?tail=50"
```

**11. Docker / host monitoring**
```bash
curl http://localhost:8875/docker/ps          # list all containers
curl http://localhost:8875/docker/stats        # per-container resource stats
curl http://localhost:8875/docker/info         # docker host info
curl http://localhost:8875/host/stats          # host CPU/memory/disk
curl http://localhost:8875/container-stats     # registry-scoped container stats
curl http://localhost:8875/service-stats       # registry-scoped service health summary
```

**12. Nginx state**
```bash
curl http://localhost:8875/nginx/connections     # nginx networks + upstreams
curl -X POST http://localhost:8875/nginx/reconnect-all  # reconnect to all networks
```

**13. SSE build log streaming (per-task isolated logs)**

```bash
# Each async task writes to its own isolated log file at:
#   $TASK_LOG_DIR/task-{task_id}.log
# Stream the log via SSE while the task runs:
curl http://localhost:8875/tasks/{task_id}/log?tail=20&follow=true

# Configuration (optional environment variables):
#   TASK_LOG_DIR       — where per-task .log files are stored
#                         (default: $GENERATED_DIR/task_logs)
#   TASK_TTL_SECONDS   — how long finished tasks + logs are kept
#                         (default: 604800 = 7 days)
#   TASK_MAX_COUNT     — max tasks in memory; oldest evicted when exceeded
#                         (default: 1000)
```

**14. Reconciliation & nginx state**
```bash
curl -X POST http://localhost:8875/reconcile        # run live reconciliation
curl http://localhost:8875/reconcile/status          # live nginx state snapshot
curl http://localhost:8875/nginx-state               # same as above
```

**15. SSL certificate management**
```bash
# List all available SSL certificate domains
curl http://localhost:8875/ssl-certs

# Upload certificates (path mode — reads from a directory)
curl -X POST http://localhost:8875/ssl-certs \
  -H 'Content-Type: application/json' \
  -d '{"domain":"example.com","ssl_path":"/etc/letsencrypt/live/example.com"}'

# Refresh from original source path
curl -X POST http://localhost:8875/ssl-certs/example.com/refresh

# Delete certificates
curl -X DELETE http://localhost:8875/ssl-certs/example.com
```

**16. Register with HTTPS**
```bash
# Full path — certs are copied to $PROVISION_DIR/ssl/example.com/
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

# Bare filename — certs already in $PROVISION_DIR/ssl/example.com/
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

**17. Subnet management — enable per-service IPAM pools**

Set `SUBNET_POOLS` (comma-separated `/16` pools) on the provision stack to reserve a dedicated
subnet for every registered service. The smallest `/30`..`/24` that fits
`containers + HEADROOM + 1 (gateway)` is allocated from the pool with a bitmap allocator
(`/29` minimum so `subnet-acl-nginx` can join the network). An empty/unset `SUBNET_POOLS`
disables subnet management entirely.

```bash
export SUBNET_POOLS=100.96.0.0/16,100.97.0.0/16   # empty = disabled
export SUBNET_HEADROOM=2                            # default 2
export ENABLE_ACL=true                              # v5: read by gateway+edge -nginx-acl (edge ACL gate); false = internal native Basic
```

```bash
# Inspect pool usage (enabled, pools[], overall, allocations[], headroom)
curl http://localhost:8875/subnet-pool
```

**18. Check missing deployment files (with recipe subdirectory)**

`recipe_path` checks a subdirectory for multi-recipe projects (e.g. `recipes/web`). The 404
message includes the recipe when it is not found.

```bash
curl "http://localhost:8875/services/myapp/check-missing-files?recipe_path=recipes/web"
# → {"service_name": "myapp", "ready": true, "missing": [], "existing": ["docker-compose", "nginx.conf", "Dockerfile", ".env"]}
```

---

## Quick Start (CLI)

```bash
# Register — just the service name as project root + filenames
python cli/register.py \
  -u alice -sn myapp \
  -pr myapp \
  -fc docker-compose.yml \
  -fn nginx.conf \
  -e .env \
  -d example.com

# Or use a full path when the project isn't under SOURCE_PROJECTS_DIR
python cli/register.py \
  -u alice -sn myapp \
  -pr /srv/provision_subnet_acl/source_projects/myapp \
  -fc docker-compose.yml \
  -fn nginx.conf \
  -d example.com

# Status
python cli/status.py -u alice

# Rebuild
python cli/rebuild.py -u alice -sn myapp -l 0

# Remove
python cli/remove.py -u alice -sn myapp -l 0

# With HTTPS
python cli/register.py \
  -u alice -sn myapp \
  -pr myapp \
  -fc docker-compose.yml \
  -fn nginx.conf \
  -d example.com \
  --https \
  --fullchain /etc/letsencrypt/live/example.com/fullchain.pem \
  --privkey /etc/letsencrypt/live/example.com/privkey.pem
```

---

## Architecture

```mermaid
%%{init: {"flowchart": {"defaultRenderer": "elk"}, "elk": {"nodePlacementStrategy": "NETWORK_SIMPLEX", "edgeRouting": "SPLINES"}} }%%
flowchart LR
    operator(["Operator"])
    end_user(["End User\nbrowser / curl"])

    subgraph host["Docker Host"]
        subgraph upt["User Provision Tool"]
            direction TB
            provision_api["subnet-acl-provision-api\nFastAPI · :8875"]
            provision_nginx["subnet-acl-nginx\nnginx · :8766"]
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

---

## Documentation

| Document | Topic |
|---|---|
| [architecture.md](docs/architecture.md) | Module layout, data flows, naming conventions |
| [api-reference.md](docs/api-reference.md) | All REST endpoints and request/response schemas |
| [cli-reference.md](docs/cli-reference.md) | CLI script arguments and examples |
| [templates.md](docs/templates.md) | Writing compose and nginx templates |
| [deployment.md](docs/deployment.md) | Running in production, environment variables |
| [testing.md](docs/testing.md) | Running unit, e2e, and integration tests |
| [template_rendering_workflow.md](docs/template_rendering_workflow.md) | Step-by-step rendering pipeline |
| [SKILL.md](skills/provision-api/SKILL.md) | VS Code AI skill — curl reference, compose & nginx templates for new services |

---

## Development

```bash
# Install dependencies (requires uv)
uv sync

# Run unit + e2e + proxy + task manager + subnet manager tests (353 tests, no Docker needed)
uv run pytest tests/test_unit.py tests/test_e2e.py tests/test_proxy_support.py tests/test_task_manager.py tests/test_subnet_manager.py -v

# Run full integration tests (120 tests, requires Docker)
sudo bash tests/test_integration.sh
```
