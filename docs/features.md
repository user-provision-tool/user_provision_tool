# Users Provision Tool — Nginx Templating Features Status

> **Version**: 1.1
> **Date**: 2026-08-22 (updated — v4 Service-ACL enforcement: byte-identical per-service confs + env.d mode switch, unified nginx assembly, portal mode, `/__basic__/` Basic short-circuit, conf regeneration)
> **Purpose**: Quick reference and implementation status tracker for the nginx templating/assembly features of the users-provision tool (v4 model).

---

## Status Legend

| Icon | Status |
|---|---|
| ✅ | Implemented & Verified |
| 🟡 | Implemented — Needs Verification |
| 🔴 | Not Implemented |
| ⚠️ | Partially Implemented / Known Issues |
| 🔮 | Future / Stretch Goal |

---

## T. Nginx Templating & Assembly (v4)

| # | Feature | Status | Notes |
|---|---|---|---|
| T1 | Byte-identical per-service conf across modes | ✅ | `template_engine.render_nginx_conf` ALWAYS injects the v4 scaffolding (no `ENABLE_ACL` branch); per-service conf is byte-identical for `ENABLE_ACL` true/false (test `test_render_nginx_conf_byte_identical_across_enable_acl`) |
| T2 | env.d mode-switch one-liner | ✅ | `write_env_d` writes the `set $auth_mode acl;|basic;` one-liner; `env.d` included per-service at **server** level (an http-level `set` is invalid nginx); empty env.d ⇒ `$auth_mode ""` ⇒ Basic |
| T3 | v4 server scaffold | ✅ | `_V4_SERVER_SCAFFOLD`: `set $auth_mode/$portal_scheme/$dashboard_host/$upstream`, `include /etc/nginx/env.d/*.env`, `location = /_set_token`, `location = /_auth_jwt`, `location /__basic__/`, `@auth_401/@auth_403` |
| T4 | Mode-switch rewrite + auth_request | ✅ | `_LOCATION_ROOT_PREFIX`: `if ($auth_mode != "acl") rewrite` + `auth_request /_auth_jwt` scoped to `location /` (Basic dialog only reachable via the `/__basic__/` short-circuit) |
| T5 | `/__basic__/` Basic short-circuit | ✅ | internal `location /__basic__/` holds the **only** `auth_basic`/`auth_basic_user_file`; empty env.d ⇒ Basic mode with 0 gateway subrequests — minimal deployments start clean (F5/B5/B12) |
| T6 | Unified nginx assembly | ✅ | `nginx.provision.conf` includes `portal.d/*.conf` + `services.d/*.conf` + `default_server 444` catch-all; docker `nginx -t` clean for fullset (ACL), fullset-https portal, minimal (empty env.d/portal.d/services.d) and minimal+services |
| T7 | Portal mode (http/https) | ✅ | `write_portal_d` renders http / https (443 ssl + 80 301) vhosts; `PORTAL_MODE`/`PORTAL_TLS_DIR`/`PORTAL_CERT_NAME` env plumbing in `docker-compose.provision.yml`; portal `location = /api/auth/verify|/api/auth/exchange { return 404; }`; portal proxy_pass variable-based (`$portal_api`/`$portal_dash`) for deferred-DNS startup |
| T8 | No `is_browser` Accept map | ✅ | nginx Accept `$is_browser` map removed from `nginx.provision.conf`; client-type detection moved to the gateway `/api/auth/verify` hybrid rule (X-Client-Type) |
| T9 | Conf regeneration | ✅ | `reconciliation.regenerate_nginx_confs()` re-renders registry confs via the v4 renderer and strips `is_browser` from orphan confs; wired into `recover_on_startup` and the `POST /nginx/regenerate` endpoint; deployed confs show 0 `is_browser` refs (QA2) |
| T10 | Service-side `/_set_token` plain variable proxy | ✅ | `location = /_set_token` is a plain variable proxy to the gateway exchange — no live bearer JWT in any URL; `_auth_jwt` proxies the gateway verify (F7/GAP-9) |
| T11 | API-first 401/403 | ✅ | `@auth_401/@auth_403` return 401/403 for non-browser `client_type`; `add_header WWW-Authenticate` always on 401; 403 browser→alert only when `auth_action=acl_denied`; login redirect carries no `?redirect=` param (GAP-14) |

---

## Summary Statistics

| Category | Total | Implemented | Verified | Gaps |
|---|---|---|---|---|
| Nginx Templating & Assembly (v4) | 11 | 11 | 11 | 0 |
| **TOTAL** | **11** | **11** | **11** | **0** |

**Implementation Rate:** 11/11 = **100.0%**
**Verified Rate:** 11/11 = **100.0%**
