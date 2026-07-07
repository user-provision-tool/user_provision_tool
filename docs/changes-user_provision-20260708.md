# `_users_provision` Changes: `2af870b` → HEAD (`d363c83`)

**Date**: 2026-07-08  
**Purpose**: Document all changes between commit `2af870b6f54031d8398222401a71fe21e524777f` and HEAD (`d363c83`) on the `gateway_support` branch.

---

## Table of Contents

1. [New API Endpoints](#1-new-api-endpoints)
2. [Docker Ops Changes](#2-docker-ops-changes)
3. [Template Engine Changes](#3-template-engine-changes)
4. [Nginx Config Changes](#4-nginx-config-changes)
5. [Test Changes](#5-test-changes)
6. [Doc Changes Already Applied](#6-doc-changes-already-applied)

---

## 1. New API Endpoints

All new endpoints are defined in `user_provision_tool/api.py`.

### 1.1 Container & Service Statistics

Two new read-only endpoints that compute statistics from `user_registry.yml` (registy-scoped, not full Docker host):

| Method | Path | Handler | Description |
|--------|------|---------|-------------|
| `GET` | `/container-stats` | `get_container_stats()` | Container-level statistics for registry-referenced containers only |
| `GET` | `/service-stats` | `get_service_stats()` | Service-level health summary for registry-referenced services only |

#### `GET /container-stats`

Returns counts across five categories:
- **healthy_running** — container is running and health check is OK (or absent)
- **unhealthy_running** — container is running but health check reports unhealthy
- **restarting** — container is in restarting state
- **down** — container exists but is stopped / exited / paused
- **missing** — container does not exist at all

Uses a single `docker ps -a` call (via new `docker_ps_all()`) rather than per-container `inspect`.

**Response `200`**:
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

#### `GET /service-stats`

Returns service-level counts:
- **healthy** — all expected containers for the service are running and healthy
- **unhealthy** — at least one container is unhealthy, restarting, down, or missing
- **expected** — total number of service instances in the registry

**Response `200`**:
```json
{
  "service_stats": {
    "healthy": 5,
    "unhealthy": 1,
    "expected": 6
  }
}
```

Both endpoints are designed for the gateway dashboard to replace direct `docker ps` usage.

---

### 1.2 SSL Certificate Management

Four new endpoints for managing SSL/TLS certificates independently of user registration:

| Method | Path | Handler | Description |
|--------|------|---------|-------------|
| `GET` | `/ssl-certs` | `list_ssl_certs()` | List all available SSL certificate domains |
| `POST` | `/ssl-certs` | `upload_ssl_cert()` | Upload SSL certificates for a domain |
| `POST` | `/ssl-certs/{domain}/refresh` | `refresh_ssl_cert()` | Refresh certificates from original source path |
| `DELETE` | `/ssl-certs/{domain}` | `delete_ssl_cert()` | Delete certificates for a domain |

#### `GET /ssl-certs`

Scans `SSL_DIR/` for subdirectories containing both `fullchain.pem` and `privkey.pem`.
Returns domain names with expiry information (computed via `openssl x509 -enddate`).

**Response `200`**:
```json
{
  "domains": [
    {
      "domain": "example.com",
      "fullchain_path": "/srv/provision/ssl/example.com/fullchain.pem",
      "privkey_path": "/srv/provision/ssl/example.com/privkey.pem",
      "created_at": "",
      "expiry_date": "2026-10-05",
      "days_left": 89
    }
  ]
}
```

#### `POST /ssl-certs`

Supports two upload modes:

- **Paste mode**: Provide `fullchain` and `privkey` PEM content directly in the request body.
- **Path mode**: Provide `ssl_path` — a directory containing `fullchain.pem` and `privkey.pem`. Files are read from that path. Also stores the source path in `.source_path` for later refresh.

Saves files to `SSL_DIR/{domain}/`. Overwrites existing files.

**Request body** (`SSLCertUploadRequest`):
```json
{
  "domain": "example.com",
  "fullchain": "-----BEGIN CERTIFICATE-----\n...",
  "privkey": "-----BEGIN PRIVATE KEY-----\n..."
}
```
Or with path mode:
```json
{
  "domain": "example.com",
  "ssl_path": "/etc/letsencrypt/live/example.com"
}
```

**Response `201`**:
```json
{
  "domain": "example.com",
  "fullchain_path": "/srv/provision/ssl/example.com/fullchain.pem",
  "privkey_path": "/srv/provision/ssl/example.com/privkey.pem",
  "expiry_date": "2026-10-05",
  "days_left": 89,
  "message": "SSL certificates saved for example.com"
}
```

**Error codes**:

| Code | Cause |
|---|---|
| `400` | Invalid domain name (empty, contains `/` or `..`) |
| `400` | SSL path is not a directory |
| `400` | `fullchain.pem` or `privkey.pem` not found in the provided path |

#### `POST /ssl-certs/{domain}/refresh`

Re-reads certificates from the original source path stored in `.source_path`.
Only works for certs that were originally uploaded via path mode.

**Response `200`**:
```json
{
  "domain": "example.com",
  "fullchain_path": "/srv/provision/ssl/example.com/fullchain.pem",
  "privkey_path": "/srv/provision/ssl/example.com/privkey.pem",
  "expiry_date": "2026-10-05",
  "days_left": 89,
  "message": "SSL certificates refreshed for example.com"
}
```

**Error codes**:

| Code | Cause |
|---|---|
| `404` | No certificates found for domain |
| `400` | No source path stored (uploaded via paste mode) |
| `400` | Source path no longer exists |

#### `DELETE /ssl-certs/{domain}`

Removes the entire `SSL_DIR/{domain}/` directory tree.

**Response `200`**:
```json
{
  "domain": "example.com",
  "message": "SSL certificates deleted for example.com"
}
```

**Error codes**:

| Code | Cause |
|---|---|
| `404` | No certificates found for domain |

#### New Helper: `_get_cert_expiry()`

Uses `openssl x509 -enddate -noout -in <cert>` to parse certificate expiry.
Returns `(iso8601_date_string, days_until_expiry)` or `("unknown", -1)` on error.

#### New Model: `SSLCertUploadRequest`

Pydantic model with fields: `domain` (str), `fullchain` (str, default `""`), `privkey` (str, default `""`), `ssl_path` (str, default `""`).

---

## 2. Docker Ops Changes

**File**: `user_provision_tool/lib/docker_ops.py`

### 2.1 Per-Task Log Threading Fix

The `_run()` function now captures `_task_log.path` in the **parent thread** before spawning stdout/stderr reader threads. Previously, `threading.local()` data was accessed inside child threads where it was always `None`, causing docker command output to be silently lost from per-task log files.

A new inner function `_write_line(text)` uses pre-captured paths to write to both the global log and per-task log, working correctly from child threads.

### 2.2 New `docker_ps_all()` Function

```python
def docker_ps_all() -> list[dict[str, str]]:
    """Return list of ALL containers (including stopped) as dicts with keys: name, status, image."""
```

Runs `docker ps -a` instead of `docker ps`. Used by the new `container-stats` and `service-stats` endpoints to detect stopped/missing containers.

The existing `docker_ps()` now delegates to a shared `_docker_ps_raw(False)` helper.

### 2.3 `container_inspect` and `container_exists` Use Explicit Type

Changed from `docker inspect <name>` to `docker container inspect <name>`. This avoids ambiguity: `docker inspect <name>` may return image metadata when a container with that name does not exist but an image with the same name does.

---

## 3. Template Engine Changes

**File**: `user_provision_tool/lib/template_engine.py`

### 3.1 Variable-Based `proxy_pass` for Per-Request DNS Resolution

`render_nginx_conf()` now rewrites static `proxy_pass` directives to use nginx variables:

```
Before:  proxy_pass http://myapp-user_alice-0-web:80;
After:   set $upstream_0000 myapp-user_alice-0-web:80;
         proxy_pass http://$upstream_0000;
```

This defers DNS resolution to **request time** (using the Docker embedded DNS resolver at `127.0.0.11`). Without this change, nginx resolves upstream hostnames ONCE at startup/reload and caches the IP forever. If the container restarts or is missing at reload time, nginx would hang.

Key behaviors:
- Each `proxy_pass` gets a unique variable name (`$upstream_0000`, `$upstream_0001`, ...)
- The regex matches `proxy_pass http://host:port;` and `proxy_pass https://host:port;`
- The rewrite preserves the original indentation

This requires the `resolver` directive in the main nginx config (see Section 4).

---

## 4. Nginx Config Changes

**File**: `nginx.provision.conf`

Added Docker embedded DNS resolver:

```nginx
resolver 127.0.0.11 valid=30s ipv6=off;
```

- `127.0.0.11` — Docker's embedded DNS server
- `valid=30s` — DNS cache TTL of 30 seconds
- `ipv6=off` — IPv4 only (Docker embedded DNS does not support IPv6)

This resolver is used together with the variable-based `proxy_pass` (Section 3.1) to resolve upstream container hostnames dynamically at request time.

---

## 5. Test Changes

### 5.1 Unit Tests (`test_unit.py`)

Increased from **186 → 192 tests** (+6).

New tests cover:
- SSL certificate API endpoints: `GET /ssl-certs`, `POST /ssl-certs`, `POST /ssl-certs/{domain}/refresh`, `DELETE /ssl-certs/{domain}`
- Container & service stats endpoints: `GET /container-stats`, `GET /service-stats`
- `docker_ps_all()` and related helper functions
- Thread-local per-task log fix verification

Test class `TestAPINewEndpoints` now includes 8 API tests (up from previous count).

### 5.2 Integration Tests (`test_integration.sh`)

Increased from **107 → 120 tests** (+13).

New tests:
- **Test 41**: Per-task log captures docker command OUTPUT — verifies that per-task log files contain real docker output (not just command lines), confirming the threading.local fix works end-to-end. Also verifies SSE log stream returns detailed output.
- **Test 42**: Nginx resilience — reload succeeds even with missing upstream containers. Full cycle:
  1. Deploy a service → verify variable-based `proxy_pass` in generated nginx conf
  2. Reload nginx with active upstreams → succeeds
  3. Stop containers → reload nginx → succeeds
  4. Remove containers entirely → reload nginx → succeeds (critical test)
- Container-stats and service-stats endpoint integration tests
- SSL certificate endpoint integration tests

### 5.3 E2E Tests (`test_e2e.py`)

Minor updates — test assertions updated for variable-based `proxy_pass` in nginx conf output.

---

## 6. Doc Changes Already Applied

The following docs were already updated as part of these commits:

| File | Changes |
|------|---------|
| `README.md` | Test counts updated (292 pytest, 120 integration) |
| `docs/testing.md` | Test counts, layer descriptions updated with container-stats, service-stats, nginx resilience mentions |
