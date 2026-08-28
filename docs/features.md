# Users Provision Tool — Nginx Templating Features Status

> **Version**: 2.0
> **Date**: 2026-08-24 (updated — cycle 20260824T173309Z v5 ACL-enforcement design implemented + verified, F1–F15 all IMPLEMENTED: `render_nginx_conf` emits the SIMPLE ACL-free per-service form (`server_name` + `auth_basic` + variable `proxy_pass`), byte-identical across `ENABLE_ACL` — the v4 server scaffold, env.d one-liner mode switch, `/__basic__/` short-circuit and portal.d vhosts are REMOVED (F8); `nginx.provision.conf` is services-only (F9); `-api` renders simple confs only and no longer reads `ENABLE_ACL`/`PORTAL_MODE`, `/docker/nginx/env` → 410 (F10); `migrate_v5.py` swept 33 deployed confs, 0 scaffold remain, with the documented repo-root invocation `cd _users_provision && uv run python -m user_provision_tool.migrate_v5 --dry-run` (F15). The ACL gate, portal routing and 401/403 challenges now live on the EDGE `-nginx-acl` in the gateway repo (F3/F4/F14). Live-verified: 388 pytest / 0, browser 2/0; GAP-16 (edge `WWW-Authenticate` `always`) and GAP-17 (migrate_v5 repo-root invocation) fixed. Prior: cycle 20260823T204609Z iter-1 (G1): `location = /_set_token` now uses `proxy_pass http://$gw/api/auth/exchange$is_args$args;` so the `?code=&redirect=` query string is relayed to the gateway exchange — no more 401 "Missing exchange code" on the `/go/` handoff (verified live: 303 `_set_token?code=` → nginx relay → 302 + Set-Cookie Max-Age=604800). Prior: v4 Service-ACL enforcement: byte-identical per-service confs + env.d mode switch, unified nginx assembly, portal mode, `/__basic__/` Basic short-circuit, conf regeneration)
> **Purpose**: Quick reference and implementation status tracker for the nginx templating/assembly features of the users-provision tool (v5 model).
> **Scope**: this doc tracks the **nginx templating/assembly pipeline**. The full provision-api surface
> (register/remove/rebuild/up/down/password/status/logs/tasks/SSE/docker/reconcile/health), SSL certs,
> container/service stats, the subnet engine, reconciliation and the CLI scripts are documented in
> `api-reference.md` / `cli-reference.md` / `testing.md`.
>
> **Edge features (v5)**: the edge `-nginx-acl` — portal routing, the `/_auth_jwt`/`/_set_token` relays
> and the API-first 401/403 challenges — is implemented in the **gateway repo** (`_provision_gateway/nginx.acl/`)
> and tracked in `_provision_gateway/docs/features.md` (portal routing → ACL15, API-first 401/403 → ACL16,
> relays → ACL2/ACL7). It is not a feature of this repo.
> **Updated**: 2026-08-27 — cycle 20260827T161836Z iter-3 FINAL verification (supervisor PASSED,
> openGapCount=0): the iter-3 gap list was EMPTY (analyzer PASSED gaps:[], gap-reviewer r1 PASSED
> failures:[], coder filesChanged=[] — no code/config/test changes). Golden F1–F22 all remain
> IMPLEMENTED and prior gaps G1–G4 remain RESOLVED in the working tree (incl. G4 — this repo's
> `tests/test_integration.sh:266` trap EXIT TERM INT HUP). Final test evidence: users_provision
> pytest 388/0 (full suite incl. T1/T9 byte-identical SIMPLE conf + v5 templating regressions),
> gateway pytest 250/0 (pythonPassed 638 total), shell suites 124/0 (incl. 13.8 edge relay),
> browser 5/0, migrate_v5 --dry-run VERIFICATION PASSED (0 scaffold, no auth_request in generated
> confs). No feature row in this doc changed status.
> Prior: doc-accuracy pass (T10/T11 edge features de-tracked to the gateway repo; scope and counts corrected).

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

## T. Nginx Templating & Assembly (v5)

| # | Feature | Status | Notes |
|---|---|---|---|
| T1 | Byte-identical SIMPLE per-service conf across modes (v5) | ✅ | v5 (F8): `template_engine.render_nginx_conf` emits the SIMPLE ACL-free form (`server_name` + `auth_basic` + variable-based `proxy_pass`) — NO `ENABLE_ACL` branch, NO v4 scaffold; byte-identical across `ENABLE_ACL` (`TestV5SimpleNginxSyntax`: no auth_request/WWW-Authenticate/`@auth_401`/`@auth_403`/env.d/`$client_type`; byte-identical render tests) |
| T2 | env.d mode-switch one-liner | 🔴 | REMOVED in v5 (F8): env.d mounts and the mode-switch one-liner are gone — the internal per-service conf is simple and ACL-free; ACL enforcement is delegated to the edge `-nginx-acl`. No `write_env_d` in the v5 pipeline (verified by `TestV5SimpleNginxSyntax`: no env.d) |
| T3 | v4 server scaffold | 🔴 | REMOVED in v5 (F8/F9): `_V4_SERVER_SCAFFOLD` (env.d include, `$auth_mode`, `/_set_token`, `/_auth_jwt`, `/__basic__/`, `@auth_401/@auth_403`) is no longer injected into internal confs; `strip_v4_scaffold` removes stale v4 tokens from deployed confs (33 swept live, 0 scaffold remain). The `_set_token`/`_auth_jwt` relay lives on the EDGE (F3) |
| T4 | Mode-switch rewrite + auth_request | 🔴 | REMOVED in v5 (F3/F8): the `if ($auth_mode != "acl") rewrite` + `auth_request /_auth_jwt` moved to the EDGE `-nginx-acl` (`location /` in the edge template); internal per-service confs are simple (no `$auth_mode`) |
| T5 | `/__basic__/` Basic short-circuit | 🔴 | REMOVED in v5 (F6): the short-circuit is gone — internal confs carry their own simple `auth_basic`/`auth_basic_user_file`. ACL-off traffic passes through the edge to the internal native Basic (B5) |
| T6 | Services-only nginx assembly (v5) | ✅ | v5 (F9): `nginx.provision.conf` includes ONLY `services.d/*.conf` + `listen 80 default_server; return 444;` catch-all; no portal.d/env.d includes (`TestV5PortalDDeprecated.test_internal_nginx_provision_conf_services_only`). docker `nginx -t` clean |
| T7 | Portal mode (http/https) | 🔴 | REMOVED in v5 (F4/F9): portal routing moved to the EDGE portal server (portal host → gateway/dashboard; verify/exchange → 404; `/api/` → gateway SSE 3600s; `/login` → gateway). Internal nginx no longer renders portal.d vhosts; `PORTAL_MODE` is no longer read by the `-api` (F7/F10) |
| T8 | No `is_browser` Accept map | ✅ | nginx Accept `$is_browser` map removed from `nginx.provision.conf`; client-type detection moved to the gateway `/api/auth/verify` hybrid rule (X-Client-Type) — unchanged in v5 |
| T9 | Conf regeneration (v5) | ✅ | v5 (F8/F10): `reconciliation.regenerate_nginx_confs()` re-renders registry confs via the v5 SIMPLE renderer and `strip_v4_scaffold` removes stale v4 tokens (env.d/portal.d/auth_request/`is_browser`) from orphan confs; wired into `recover_on_startup` and the `POST /nginx/regenerate` endpoint (live: 33 confs swept, 0 scaffold; 24 regenerated / 0 errors) |

---

## Summary Statistics

| Category | Total | Implemented | Verified | Gaps |
|---|---|---|---|---|
| Nginx Templating & Assembly (v5) | 9 | 4 | 4 | 5 |
| **TOTAL** | **9** | **4** | **4** | **5** |

**Implementation Rate:** 4/9 = **44.4%** (T2–T5, T7 are 🔴 — v4 mechanisms removed in v5, replaced by the edge `-nginx-acl`)
**Verified Rate:** 4/9 = **44.4%**
