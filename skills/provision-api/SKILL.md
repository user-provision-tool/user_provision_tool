---
name: provision-api
description: "Use when: interacting with the locally deployed provision-api (user_provision_tool) via curl — registering users, checking status, rebuilding, or removing per-user Docker Compose stacks. Also use when: a target repo has a Dockerfile but no docker-compose.yml or nginx.conf — read the repo docs to create them following the provision-api template patterns."
---

# Provision-API Skill

This skill covers working with the locally deployed **User Provision Tool** (`provision-api`) —
a FastAPI service at `http://localhost:8765` that stamps out isolated, routed per-user copies
of any Docker Compose stack with a single API call.

Source project: `_users_provision/` (under the workspace root).

---

## ⛔ CRITICAL: Never modify original source files

**Do NOT edit the user's original `docker-compose.yml` or `nginx.conf`.** These are the
user's source-of-truth files. The provision tool's auto-converter reads them and writes
`.j2` templates alongside them — it never modifies the originals.  If you need to make
changes, create a **new** file (e.g. `docker-compose.provision.yml`) and reference that
instead, or explain the change and let the user decide.

---

## Quick Reference (curl commands)

All examples assume the provision-api is running on `http://localhost:8765`.

All operations (register, rebuild, remove) return a `task_id` immediately.
Poll `GET /tasks/{task_id}` for progress; the result appears when status reaches `completed`.

### Health check

```bash
curl http://localhost:8765/health
# → {"status": "ok"}
```

### Register a user

```bash
curl -X POST http://localhost:8765/users \
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
# → 202 {"task_id": "a1b2c3d4e5f6", "status": "pending", "type": "register"}
```

### Register with proxy build args

```bash
curl -X POST http://localhost:8765/users \
  -H 'Content-Type: application/json' \
  -d '{
    "user_name": "alice",
    "service_name": "myapp",
    "project_root": "myapp",
    "compose_file_path": "docker-compose.yml",
    "build_args": {
      "HTTP_PROXY": "http://proxy:8080",
      "HTTPS_PROXY": "http://proxy:8080"
    }
  }'
```

### Register with HTTPS

```bash
# Full path — certs are copied to $SSL_DIR/example.com/
curl -X POST http://localhost:8765/users \
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
curl -X POST http://localhost:8765/users \
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

### Poll task status

```bash
curl http://localhost:8765/tasks/a1b2c3d4e5f6
# → {"task_id": "a1b2c3d4e5f6", "type": "register", "status": "completed", "result": {...}}

# List all tasks
curl http://localhost:8765/tasks
# → {"count": 3, "tasks": [...]}
```

### Cancel a task

```bash
curl -X DELETE http://localhost:8765/tasks/a1b2c3d4e5f6
# → {"task_id": "a1b2c3d4e5f6", "status": "cancelled"}
```

### Get all users status

```bash
curl http://localhost:8765/users
```

### Get single user status

```bash
curl http://localhost:8765/users/alice
```

### Rebuild user containers

```bash
curl -X POST http://localhost:8765/users/alice/services/myapp/0/rebuild \
  -H 'Content-Type: application/json' \
  -d '{"no_cache": true}'
# → 202 {"task_id": "c3d4e5f6a7b8", "status": "pending", "type": "rebuild"}
```

### Rebuild with proxy build args

```bash
curl -X POST http://localhost:8765/users/alice/services/myapp/0/rebuild \
  -H 'Content-Type: application/json' \
  -d '{"no_cache": true, "build_args": {"HTTP_PROXY": "http://proxy:8080"}}'
```

### Remove (deregister) a user's service

Removes **one** registration entry — the specific user + service + label combination.
A user registered with multiple services or labels is not affected; only the targeted
instance is torn down.

```bash
curl -X DELETE http://localhost:8765/users/alice/services/myapp/0
# → 202 {"task_id": "b2c3d4e5f6a7", "status": "pending", "type": "remove"}
```

### Start / Stop a service

```bash
# Start (docker compose up -d)
curl -X POST http://localhost:8765/users/alice/services/myapp/0/up
# → 200 {"message": "Service started.", "status": "up"}

# Stop (docker compose stop)
curl -X POST http://localhost:8765/users/alice/services/myapp/0/down
# → 200 {"message": "Service stopped.", "status": "down"}
```

### Change password

```bash
curl -X PUT http://localhost:8765/users/alice/services/myapp/0/password \
  -H 'Content-Type: application/json' \
  -d '{"passwd": "newsecret"}'
# → 200 {"message": "Password updated. Nginx reloaded.", ...}
```

### Container logs

```bash
curl "http://localhost:8765/users/alice/services/myapp/0/containers/web/logs?tail=50"
# → 200 {"container": "myapp-user_alice-0-web", "tail": 50, "logs": [...]}
```

### Docker / host monitoring

```bash
curl http://localhost:8765/docker/ps          # all containers
curl http://localhost:8765/docker/stats        # per-container resource stats
curl http://localhost:8765/docker/info         # docker host info (container counts)
curl http://localhost:8765/host/stats          # host CPU/memory/disk
curl http://localhost:8765/container-stats     # registry-scoped container health stats
curl http://localhost:8765/service-stats       # registry-scoped service health summary
```

### SSL certificate management

```bash
# List all available certificate domains
curl http://localhost:8765/ssl-certs

# Upload certificates (path mode — reads fullchain.pem + privkey.pem from a directory)
curl -X POST http://localhost:8765/ssl-certs \
  -H 'Content-Type: application/json' \
  -d '{"domain":"example.com","ssl_path":"/etc/letsencrypt/live/example.com"}'

# Upload certificates (paste mode — PEM content in request body)
curl -X POST http://localhost:8765/ssl-certs \
  -H 'Content-Type: application/json' \
  -d '{"domain":"example.com","fullchain":"-----BEGIN CERTIFICATE-----\n...","privkey":"-----BEGIN PRIVATE KEY-----\n..."}'

# Refresh from original source path
curl -X POST http://localhost:8765/ssl-certs/example.com/refresh

# Delete certificates for a domain
curl -X DELETE http://localhost:8765/ssl-certs/example.com
```

### Nginx state management

```bash
# View nginx connections and upstreams
curl http://localhost:8765/nginx/connections

# Reconnect provision-nginx to all user networks + reload
curl -X POST http://localhost:8765/nginx/reconnect-all
```

### SSE build log streaming

```bash
# Stream real-time build logs (Server-Sent Events)
curl http://localhost:8765/tasks/{task_id}/log
```

### Reconciliation helpers (used by provision-gateway)

```bash
curl http://localhost:8765/docker/container/provision-nginx/exists
curl http://localhost:8765/docker/container/provision-nginx/running
curl -X POST http://localhost:8765/docker/network/mynet/connect/provision-nginx
curl -X POST http://localhost:8765/docker/nginx/reload
```

---

## Request Parameters

| Field | Type | Required | Notes |
|---|---|---|---|
| `user_name` | string | ✓ | `[a-zA-Z0-9_-]+` |
| `service_name` | string | ✓ | `[a-zA-Z0-9_-]+` |
| `project_root` | string | — | Bare name resolves to `$SOURCE_PROJECTS_DIR/{name}`. Relative/absolute path used as-is. 404 if not found. |
| `compose_file_path` | string | † | Plain `docker-compose.yml` → auto-converted to `.j2` template |
| `compose_template_path` | string | † | Pre-made `.j2` compose template filename |
| `nginx_conf_file_path` | string | — | Plain nginx conf → auto-converted to `.j2` template |
| `nginx_conf_template_path` | string | — | Pre-made `.j2` nginx conf template filename |
| `env_file_path` | string | — | `.env` file for Docker Compose `${VAR}` substitution. Copied as `.env.{user}.{label}` next to the generated compose file. Any `env_file: .env` directives in service definitions are automatically replaced with this per-user file name. |
| `label` | string | — | Digits only; default `"0"` |
| `domain` | string | — | Domain for `server_name`; default `"localhost"` |
| `passwd` | string | — | Default `"123456"`. Pass `""` to disable HTTP basic auth entirely. |
| `volumes` | object | — | `{ "template_vol_key": "/host/path", ... }` |
| `build_args` | object | — | `{ "HTTP_PROXY": "http://proxy:8080", ... }` — passed as `--build-arg` to `docker compose build`. Stored in registry. |
| `https` | bool | — | Enable HTTPS (default `false`). Requires `fullchain` and `privkey`. |
| `fullchain` | string | — | Path or bare filename to the certificate file. Full path → copied to `$SSL_DIR/{domain}/fullchain.pem`. Bare filename → used directly from `$SSL_DIR/{domain}/`. |
| `privkey` | string | — | Path or bare filename to the private key file. Same resolution rules as `fullchain`. |
| `no_cache` | bool | — | **(rebuild only)** Pass `--no-cache` to `docker compose build` |

> † Exactly one of `compose_file_path` or `compose_template_path` is required.

## Response Codes

| Endpoint | Success | Error codes |
|---|---|---|
| `POST /users` | `202` → `task_id` | `404`, `422` |
| `DELETE /users/...` | `202` → `task_id` | `404` |
| `POST .../rebuild` | `202` → `task_id` | `404` |
| `POST .../up` | `200` | `404`, `500` |
| `POST .../down` | `200` | `404`, `500` |
| `PUT .../password` | `200` | `404` |
| `GET .../containers/{c}/logs` | `200` | `404` |
| `GET /users`, `GET /users/{name}` | `200` | `404` |
| `GET /tasks` | `200` | — |
| `GET /tasks/{id}` | `200` | `404` |
| `GET /tasks/{id}/log` | `200` (SSE) | — |
| `DELETE /tasks/{id}` | `200` | `404`, `409` |
| `GET /docker/ps`, `/stats`, `/info` | `200` | — |
| `GET /host/stats` | `200` | — |
| `GET /docker/container/{c}/exists` | `200` | — |
| `GET /docker/container/{c}/running` | `200` | — |
| `POST /docker/network/{n}/connect/{c}` | `200` | — |
| `POST /docker/nginx/reload` | `200` | — |
| `GET /nginx/connections` | `200` | — |
| `POST /nginx/reconnect-all` | `200` | — |

## Task Lifecycle

```
pending → running → completed
                  → failed   (error stored in task)
                  → cancelled (via DELETE /tasks/{id})
```

Tasks auto-clean after 1 hour. Poll `GET /tasks/{task_id}` for the `status` field.

---

## Deploying provision-api locally

```bash
export PROVISION_DIR=/srv/provision
export PROVISION_API_PORT=8765

# Create directory structure
mkdir -p $PROVISION_DIR/{generated,ssl,source_projects,user_data}

# Start
docker compose -f docker-compose.provision.yml up -d --build

# Verify
curl http://localhost:8765/health
```

The compose file at `_users_provision/docker-compose.provision.yml` defines two services:
- **provision-api** (FastAPI on `:8765`) — handles registration, rendering, and Docker orchestration
- **provision-nginx** (nginx:alpine on `:80`) — shared ingress router that proxies to per-user containers

---

## Creating docker-compose.yml when only a Dockerfile exists

When a target repo has a `Dockerfile` but **no** `docker-compose.yml`, read the repo's docs
(README, docs/, etc.) to infer the service architecture, then create a
`docker-compose.yml` following these rules.

### Rules for the compose file

1. **Use `build: .`** (or a subdirectory path) so the Dockerfile is the build context.
   Do NOT hardcode `image:` unless the docs specify a pre-built image.

2. **Do NOT set `container_name:` directly** — the provision tool's auto-converter will
   rewrite `container_name` to use the per-user `{{ container_prefix }}` prefix. Writing
   a descriptive name (e.g., `myapp-web`) helps the converter produce sensible prefixes.

3. **Do NOT hardcode host `ports:` unless essential** — the provision tool strips `ports:`
   on conversion so all traffic routes through `provision-nginx`. If the service needs a
   port exposed internally, use `expose:` instead.

4. **Use named volumes** for persistent data so the converter can extract volume keys
   and map them to per-user host paths via the `volumes` registration parameter.

5. **Use `${ENV_VAR}` syntax** for runtime secrets (API keys, DB passwords). These are
   resolved by `docker compose` at container startup via the `--env-file` flag, keeping
   secrets out of templates and source control.

6. **Define a network** so per-user containers are isolated. The converter replaces
   network names with `{{ network_name }}`.

7. **If the docs provide a compose example, use it verbatim** — only add missing
   directives (volumes, networks, environment) as needed.

### Template for compose file

```yaml
services:
  {service_shortname}:
    build: .
    container_name: {service_shortname}
    expose:
      - "{internal_port}"
    environment:
      - SOME_VAR=${SOME_VAR}
    volumes:
      - {named_vol}:/data
    networks:
      - {service_shortname}-net
    restart: unless-stopped

  # Add db, cache, etc. if the Dockerfile expects them
  db:
    image: postgres:16-alpine
    container_name: {service_shortname}-db
    environment:
      - POSTGRES_DB=${POSTGRES_DB}
      - POSTGRES_USER=${POSTGRES_USER}
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
    volumes:
      - db_data:/var/lib/postgresql/data
    networks:
      - {service_shortname}-net

volumes:
  {named_vol}:
  db_data:

networks:
  {service_shortname}-net:
```

### Important: write the file into the project root

The compose file must be placed **inside** the project's source directory (i.e., the
directory that will become `project_root`). This is because:
- `build: .` must resolve to the directory containing the Dockerfile.
- The provision tool's auto-converter writes the `.j2` template alongside the source file.
- The rendered per-user compose file is also written into this directory.

---

## Creating nginx.conf when none is provided

When a target repo has no `nginx.conf`, generate one **at the beginning** — before
registering any users.  Read the `docker-compose.yml` directly to discover the service
name(s) and internal port(s), then create the nginx conf.

1. **Read the compose file** — inspect `docker-compose.yml` to find the **service name**
   (the key under `services:`) and the internal port (from `expose:` or the Dockerfile).

2. **Generate the nginx.conf** — using the service name from step 1, create the nginx conf
   with the correct `proxy_pass` target.  Place it inside the project root (same directory
   as the Dockerfile and `docker-compose.yml`).

The `proxy_pass` target must use the **service name** (the Docker Compose service key),
which Docker's internal DNS resolves to the container IP.  The provision tool's converter
will prefix it with `{{ container_prefix }}`.

> ⚠️ **Critical**: Every `proxy_pass` host **must exactly match** a service key from
> `docker-compose.yml`.  Registration will be **rejected** if any host does not match —
> the error message lists the unknown hosts so you can fix them.  Do NOT use made-up
> names like `myapp-web` — read the actual service keys from the compose file.

### Nginx conf template

Copy this template and fill in the placeholders for the specific service:

```nginx
server {
    listen 80;
    server_name {service_name}_hostname;

    location / {
        proxy_pass http://{service_name}:{internal_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_read_timeout 300s;
        proxy_connect_timeout 60s;
        client_max_body_size 0;
    }
}
```

For HTTPS support, wrap the server blocks in `{% if https %}...{% endif %}` conditionals
and use `{{ ssl_certificate_path }}` / `{{ ssl_certificate_key_path }}` for the cert paths.
The converter automatically replaces `ssl_certificate` and `ssl_certificate_key` paths with
template variables and wraps `listen 443 ssl` server blocks in `{% if https %}` blocks.

**Placeholders to fill in:**

| Placeholder | What to put | Converter does |
|---|---|---|
| `{service_name}_hostname` | E.g. `myapp_hostname` | Replaced with `{{ hostname }}` → `{service}-{user}-{label}.{domain}` |
| `{service_name}` | **Compose service key** — read directly from the `services:` section of `docker-compose.yml`. Must be an exact match (case-insensitive). E.g. `web`, `mcp-server`, `db`. Do NOT add prefixes like `myapp-` — use the raw key. | Validated against compose service names; rejected if no match. Matching hosts become `{{ container_prefix }}<name>`. |
| `{internal_port}` | The internal port the service listens on, e.g. `8000` — from `expose:` or the Dockerfile | Left as-is |

### Converter behavior (what happens automatically)

- **`proxy_pass` service-name validation** — the converter reads the companion
  `docker-compose.yml` and checks that every `proxy_pass` host matches a compose
  service name.  **Hosts that do NOT match a compose service are rejected with an
  error** — you must update the nginx conf so each proxy_pass target is the exact
  service key from `services:` in `docker-compose.yml`.  Matching hosts are
  rewritten to `{{ container_prefix }}<name>` so they resolve to the actual
  deployed container name at render time.

- **`auth_basic` injection** — if the source conf has no `auth_basic` directives, the
  converter automatically injects `auth_basic "{{ service_name }} - {{ user_name }}";`
  and `auth_basic_user_file {{ htpasswd_path }};` before the first `proxy_pass`.
  When `passwd=""` (no-auth mode), these lines are stripped at render time.

- **Standard proxy headers** — `Host`, `X-Real-IP`, `X-Forwarded-For`, and
  `X-Forwarded-Proto` are already in the template; keep them.

- **WebSocket support** — if the service uses WebSockets, add these to the location block:
  ```nginx
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  ```

### If the target repo's docs provide an nginx example

Use it **verbatim** instead of the template above. The converter will still apply its
standard substitutions (`server_name`, `proxy_pass` prefix, `auth_basic` injection).

### File placement

- The nginx conf lives **inside** the project root (next to the compose file).
- The auto-converter writes the `.j2` template alongside it.
- The rendered per-user nginx conf is written to `GENERATED_DIR` (`$PROVISION_DIR/generated`).

---

## File placement checklist for a new service

When setting up a new service for the provision tool, ensure the project root contains:

```
source_projects/{service_name}/
├── Dockerfile                        ← required (build context)
├── docker-compose.yml                ← required (or docker-compose.{name}.yml)
├── nginx.conf                        ← optional but recommended (or {name}.nginx.conf)
├── .env                              ← optional (runtime secrets via ${ENV_VAR})
└── (application source files)
```

---

## Provision-api template variable reference

When writing `.j2` templates manually, these Jinja2 variables are available:

| Variable | Example | Scope |
|---|---|---|
| `{{ user_name }}` | `alice` | compose + nginx |
| `{{ service_name }}` | `myapp` | compose + nginx |
| `{{ label }}` | `0` | compose + nginx |
| `{{ container_prefix }}` | `myapp-user_alice-0-` | compose + nginx |
| `{{ network_name }}` | `myapp-user_alice-0` | compose only |
| `{{ volumes['key'] }}` | `/srv/provision/user-data/alice/app` | compose only |
| `{{ domain_name }}` | `example.com` | compose + nginx |
| `{{ hostname }}` | `myapp-alice-0.example.com` | nginx only |
| `{{ htpasswd_path }}` | `/srv/provision/generated/myapp.user-alice.0.htpasswd` | nginx only |
| `{{ https }}` | `True` / `False` | nginx only |
| `{{ ssl_certificate_path }}` | `/srv/provision/ssl/example.com/fullchain.pem` | nginx only |
| `{{ ssl_certificate_key_path }}` | `/srv/provision/ssl/example.com/privkey.pem` | nginx only |

- `{{ var }}` placeholders are resolved at **registration time** by Jinja2.
- `${ENV_VAR}` placeholders are resolved at **container startup** by `docker compose` via `--env-file`.
