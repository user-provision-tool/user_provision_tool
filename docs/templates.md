# Template Guide

Templates use the `.j2` file extension so that YAML linters do not flag Jinja2 syntax as errors.

---

## Two Placeholder Types

Templates can contain two distinct placeholder syntaxes with different resolution times:

```
┌──────────────────────────────────────────────────────────────────┐
│  Placeholder       │  Resolved by        │  When                 │
├──────────────────────────────────────────────────────────────────┤
│  {{ var }}         │  Jinja2 / tool       │  At registration time │
│  ${ENV_VAR}        │  docker compose      │  At container start   │
└──────────────────────────────────────────────────────────────────┘
```

`{{ var }}` placeholders are replaced by the tool when it renders the template into a concrete
compose file. `${ENV_VAR}` placeholders are left as-is in the rendered file and resolved by
`docker compose` at startup using the `.env` file supplied via `--env-file`.

This lets you bake per-user identity (name, label, volume paths) into the file at render time
while keeping runtime secrets (API keys, DB passwords) out of the registry and out of source
control.

---

## Compose Template Variables

| Variable | Example value | Description |
|---|---|---|
| `{{ user_name }}` | `alice` | User name passed to registration |
| `{{ service_name }}` | `myapp` | Service name |
| `{{ label }}` | `0` | Numeric label |
| `{{ container_prefix }}` | `myapp-user_alice-0-` | Prefix for `container_name` entries |
| `{{ volumes['key'] }}` | `/srv/provision_subnet_acl/user-data/alice/app` | Host path for a named volume |
| `{{ domain_name }}` | `example.com` | Domain (compose templates rarely use this) |
| `{{ subnet }}` | `100.96.0.0/29` | Reserved subnet for this service (empty string when subnet management is disabled) |
| `{{ gateway }}` | `100.96.0.1` | Gateway for the reserved subnet (empty string when subnet management is disabled) |

### IPAM subnet block (`{% if subnet %}`)

When `SUBNET_POOLS` is enabled, the rendered compose file gets an `ipam:` block under the
user network. Guard it with `{% if subnet %}` so the template stays valid when subnet
management is disabled (the variable renders as an empty string):

```yaml
networks:
  {{ network_name }}:
    name: {{ network_name }}
    {% if subnet %}
    ipam:
      config:
        - subnet: {{ subnet }}
          gateway: {{ gateway }}
    {% endif %}
```

`ensure_subnet_ipam_block()` auto-injects this block (backing up the original as `.bak`)
when subnet pools are enabled and the template predates the feature.

---

## Nginx Conf Template Variables

All compose variables are available, plus:

| Variable | Example value | Description |
|---|---|---|
| `{{ hostname }}` | `myapp-alice-0.example.com` | Derived as `{service}-{user}-{label}.{domain}` |
| `{{ htpasswd_path }}` | `/srv/provision_subnet_acl/generated/myapp.user-alice.0.htpasswd` | Absolute path to the generated `.htpasswd` file in `GENERATED_DIR` |
| `{{ https }}` | `True` / `False` | Boolean; `True` when HTTPS is enabled |
| `{{ ssl_certificate_path }}` | `/srv/provision_subnet_acl/ssl/example.com/fullchain.pem` | Path to the fullchain certificate file |
| `{{ ssl_certificate_key_path }}` | `/srv/provision_subnet_acl/ssl/example.com/privkey.pem` | Path to the private key file |

---

## Compose Template Example

```yaml
# myapp.yml.j2
services:
  web:
    image: nginx:alpine
    container_name: {{ container_prefix }}web
    volumes:
      - {{ volumes['app_data'] }}:/usr/share/nginx/html:ro
    environment:
      - SERVICE_NAME={{ service_name }}
      - USER_NAME={{ user_name }}
      - DB_PASSWORD=${DB_PASSWORD}      # resolved at runtime from .env

  db:
    image: postgres:16-alpine
    container_name: {{ container_prefix }}db
    environment:
      - POSTGRES_DB={{ service_name }}_{{ user_name }}
      - POSTGRES_USER={{ user_name }}
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}   # resolved at runtime from .env
    volumes:
      - {{ volumes['db_data'] }}:/var/lib/postgresql/data
```

---

## Nginx Conf Template Example

```nginx
# myapp.nginx.conf.j2
{% if https %}
# --- HTTPS enabled ---
server {
    listen 80;
    server_name {{ hostname }};
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name {{ hostname }};

    ssl_certificate     {{ ssl_certificate_path }};
    ssl_certificate_key {{ ssl_certificate_key_path }};

    auth_basic "{{ service_name }} — {{ user_name }}";
    auth_basic_user_file {{ htpasswd_path }};

    location / {
        proxy_pass       http://{{ container_prefix }}web:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
{% else %}
# --- HTTP only ---
server {
    listen 80;
    server_name {{ hostname }};

    auth_basic "{{ service_name }} — {{ user_name }}";
    auth_basic_user_file {{ htpasswd_path }};

    location / {
        proxy_pass       http://{{ container_prefix }}web:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
{% endif %}
```

---

## ACL Enforcement (`ENABLE_ACL`) — v5 simple model

**v5 model (cycle 20260824T173309Z, F8):** `ENABLE_ACL` does **not** rewrite the rendered nginx
conf and no longer drives an env.d mode switch. `render_nginx_conf()` always renders the SIMPLE
ACL-free form (byte-identical for `ENABLE_ACL` true/false — `TestV5SimpleNginxSyntax`, byte-identical
render tests). The v4 server scaffold, the `env.d` one-liner and the `/__basic__/` short-circuit are
REMOVED from the internal confs; `strip_v4_scaffold` removes stale v4 tokens from deployed confs.

The current SIMPLE per-service conf (server level):

```nginx
server_name {{ hostname }};

auth_basic "{{ service_name }} — {{ user_name }}";
auth_basic_user_file {{ htpasswd_path }};

location / {
    proxy_pass http://$upstream_XXXX;   # variable-based proxy_pass (see below)
    proxy_set_header Host $host;
    ...
}
```

Behavior:
- **ACL mode** (`ENABLE_ACL=true`): the edge `-nginx-acl` (gateway repo) is the entry; its
  `location /` runs `auth_request /_auth_jwt` → gateway `/api/auth/verify` (v4 hybrid `X-Client-Type`
  rule; `error_page 401/403` → browser 302 dashboard, API `401`/`403`; `WWW-Authenticate:
  Basic realm="subnet-acl"` `always` on 401, GAP-16). The internal per-service conf is unchanged.
- **ACL-off mode** (`ENABLE_ACL=false`, the default): the edge passes traffic through to the internal
  native Basic (`auth_basic`/`auth_basic_user_file` on the simple conf) — 0 gateway subrequests
  (F6/B5).
- The legacy `map $http_accept $is_browser` discriminator was removed in v4 (QA2) — no conf
  references `$is_browser`.
- `/_set_token` on the edge is a **plain variable proxy** to the gateway exchange
  (`/api/auth/exchange`) — no live bearer JWT in any URL. The exchange swaps the 30s HMAC code for
  the `provision_token` cookie (Max-Age=604800, `PROVISION_COOKIE_TTL`) and returns a
  port-preserving `302 $scheme://$http_host$arg_redirect;` so the `/go/` flow lands on the same
  host:port the browser came from.

---

## Nginx `proxy_pass` — Variable-Based DNS Resolution

After Jinja2 rendering, `render_nginx_conf()` post-processes every `proxy_pass` directive
to use **nginx variables** instead of static hostnames:

```
Before:  proxy_pass http://myapp-user_alice-0-web:80;
After:   set $upstream_0000 myapp-user_alice-0-web:80;
         proxy_pass http://$upstream_0000;
```

**Why**: Without variables, nginx resolves upstream hostnames **once** at startup/reload
and caches the IP forever. If the upstream container restarts (getting a new IP) or is
missing at reload time, nginx hangs. With variables, DNS resolution is **deferred to
request time** via the Docker embedded DNS resolver (`127.0.0.11`).

**Requirements**:
- The main `nginx.provision.conf` must include `resolver 127.0.0.11 valid=30s ipv6=off;`
- Each `proxy_pass` gets a unique variable name (`$upstream_0000`, `$upstream_0001`, …)
- Both `http://` and `https://` schemes are supported
- Original indentation is preserved

This is an automatic post-processing step — you write `proxy_pass http://{{ container_prefix }}web:80;`
in your template as usual; the variable rewrite happens transparently.

---

## `.env` File Support

If you supply an `.env` file path at registration time (`env_file_path` in the API,
`--env-file` in the CLI), the tool:

1. Copies the `.env` file next to the rendered compose file with a per-user unique name
   (``.env.{user_name}.{label}``) so that multiple users in the same project directory
   don't collide.
2. Passes `--env-file <copied-path>` to every `docker compose` invocation for that service.
3. **Automatically replaces** any ``env_file: .env`` directive in service definitions (both
   string and list forms) with the per-user env file name, so containers load environment
   variables from the correct file.

```
Registration
  ├─ template rendered → source_projects/myapp/docker-compose.user-alice.0.yml
  ├─ .env copied       → source_projects/myapp/.env.alice.0
  └─ env_file: .env    → env_file: .env.alice.0  (rewritten in rendered compose)

docker compose -f ...docker-compose.user-alice.0.yml --env-file .../.env.alice.0 up -d
                                                      └─ resolves ${ENV_VAR} at start
```

The `.env` path stored in the registry always points to the **copied** per-user file, so
``compose_up``, ``compose_down``, and ``compose_build`` all use the same resolved path.

---

## Volume Extraction

The tool parses the `volumes:` sections of a template before rendering it (using a
placeholder-safe Jinja2 environment) to discover which volume keys the template declares.
At registration time it cross-checks these keys against the `volumes` map you provided:

- **Missing keys** — declared in template but not provided → warning (API response / CLI prompt)
- **Extra keys** — provided but not in template → warning only
- Neither condition blocks registration; you may proceed with the warning.

---

## Automatic Template Generation

If you have a working `docker-compose.yml` or nginx conf but no `.j2` template yet, the
tool can generate one automatically.

**Via `register.py` flags** (convert + register in one step):
```bash
# Simplest: bare project_root name (resolves to $PROVISION_DIR/source_projects/myapp)
python cli/register.py -pr myapp \
  -fc docker-compose.yml -fn nginx.conf -u alice -sn myapp ...

# Full path when the project is outside SOURCE_PROJECTS_DIR
python cli/register.py -pr /srv/provision_subnet_acl/source_projects/myapp \
  -fc docker-compose.yml -fn nginx.conf -u alice -sn myapp ...
```

**Via standalone scripts** (generate template only, no registration):
```bash
python cli/gen_compose_template.py source_projects/myapp/docker-compose.yml -s myapp
# → writes source_projects/myapp/docker-compose.yml.j2

python cli/gen_nginx_template.py source_projects/myapp/nginx.conf -s myapp
# → writes source_projects/myapp/nginx.conf.j2
```

The converters apply these substitutions:

| Directive (compose) | Transformation |
|---|---|
| `container_name` | `→ {{ container_prefix }}{suffix}` |
| bind-mount source paths | `→ {{ volumes['key'] }}` (except Docker socket — see below) |
| network names | `→ {{ network_name }}` |
| named volume keys | `→ {{ volumes['key'] }}` |
| `name:` and `ports:` | stripped |
| `profiles:` | stripped from kept services; services with any non-empty profile string are excluded entirely |

> **Docker socket passthrough**: Bind mounts to `/var/run/docker.sock` and `/run/docker.sock`
> are **never** converted to template variables.  These are host system sockets that must
> remain as literal paths so containers can communicate with the host Docker daemon.
> They are left unchanged in both the `.j2` template and the rendered compose file.

| Directive (nginx) | Transformation |
|---|---|
| `server_name` | `→ {{ hostname }}` |
| `auth_basic` | `→ {{ service_name }} — {{ user_name }}` |
| `auth_basic_user_file` | `→ {{ htpasswd_path }}` |
| `proxy_pass` host matching a compose service name | `→ {{ container_prefix }}<name>` |
| `proxy_pass` host NOT matching any compose service | **Rejected** — registration fails with validation error listing unknown hosts |
| _(no `auth_basic` block present)_ | Injects `auth_basic "{{ service_name }} - {{ user_name }}";` and `auth_basic_user_file {{ htpasswd_path }};` before the first `proxy_pass` |
| every server block | Normalizes legacy `return 302 $arg_redirect;` → `return 302 $scheme://$http_host$arg_redirect;` so the `/go/` redirect preserves the host port |
| _(v5)_ | The `/_set_token`/`/_auth_jwt`/`/__basic__/`/`@auth_401`/`@auth_403`/env.d scaffold is NO LONGER injected into internal confs — it moved to the edge `-nginx-acl` (F3/F8); `strip_v4_scaffold` removes any stale v4 tokens from deployed confs |

> **v5: no ENABLE_ACL branch at render time** — the SIMPLE per-service conf is **always** rendered
> (byte-identical per-service conf). ACL enforcement is delegated to the edge `-nginx-acl` (F3/F8),
> never to the internal per-service conf.

> **Password stripping**: when `passwd` is empty (`""`), `render_nginx_conf()` strips all
> `auth_basic` and `auth_basic_user_file` lines from the rendered nginx conf, so no
> authentication directives appear in the final file for no-auth users.

---

## Naming Conventions for Template Files

There is no enforced naming convention, but the recommended pattern is:

```
source_projects/{service_name}/    ← bare project_root name = SOURCE_PROJECTS_DIR/{service_name}
  docker-compose.{service_name}.yml.j2   ← compose template
  {service_name}.nginx.conf.j2           ← nginx conf template
  {service_name}.env                     ← runtime secrets (.env)
  Dockerfile                             ← image build context
```

This layout keeps all source files together so that `build: .` in compose templates
resolves to the correct directory, and the rendered per-user compose file lands next to
the Dockerfile.

---

## VS Code AI Skill

The [provision-api skill](../skills/provision-api/SKILL.md) includes ready-to-copy templates
for `docker-compose.yml` and `nginx.conf` when setting up a new service — fill in two
placeholders and the auto-converter handles the rest. It also provides a full variable
reference and curl command cheat-sheet for the REST API.
