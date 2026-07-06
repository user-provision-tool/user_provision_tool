"""FastAPI service for the user provision tool.

Endpoints:
  POST   /users                                           register user + start containers (async → task_id)
  DELETE /users/{user_name}/services/{service_name}/{label}   stop + deregister (async → task_id)
  POST   /users/{user_name}/services/{service_name}/{label}/rebuild  (async → task_id)
  GET    /users                                           status of all users
  GET    /users/{user_name}                               status of one user
  GET    /tasks                                           list all tasks in the pool
  GET    /tasks/{task_id}                                 query async task status / result
  DELETE /tasks/{task_id}                                 cancel a pending or running task

Long-running operations (register, rebuild, remove) now return a ``task_id``
immediately.  Poll ``GET /tasks/{task_id}`` for progress (status, result, error).

For backward compatibility, pass ``?sync=true`` to block until completion.

Environment variables:
  GENERATED_DIR   directory for generated compose/nginx files  (default: ./generated)
  REGISTRY_FILE   path to user_registry.yml                    (default: ./user_registry.yml)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

# Make lib/ importable when the file sits at the project root
sys.path.insert(0, str(Path(__file__).parent))

from lib import docker_ops, provisioner, registry, template_engine, validation
from lib.compose_converter import compose_file_to_template, get_compose_service_names
from lib.nginx_converter import nginx_file_to_template
from lib.task_manager import task_manager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GENERATED_DIR = Path(
    os.environ.get("GENERATED_DIR", str(Path(__file__).parent / "generated"))
)
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

# User volume data root: auto-created subdirectories are used when no volumes
# are explicitly provided at registration time.
USER_DATA_DIR = Path(
    os.environ.get("USER_DATA_DIR", str(GENERATED_DIR.parent / "user_data"))
)
USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Source projects root: operators clone / copy service repos here.
SOURCE_PROJECTS_DIR = Path(
    os.environ.get("SOURCE_PROJECTS_DIR", str(GENERATED_DIR.parent / "source_projects"))
)
SOURCE_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# SSL certificates root: fullchain.pem and privkey.pem are copied here per domain.
SSL_DIR = Path(
    os.environ.get("SSL_DIR", str(GENERATED_DIR.parent / "ssl"))
)
SSL_DIR.mkdir(parents=True, exist_ok=True)

NGINX_CONTAINER = os.environ.get("NGINX_CONTAINER", "provision-nginx")

# The registry module reads REGISTRY_FILE from its own env var at import time.
# We additionally sync it here so both the API and the lib use the same path.
import lib.registry as _reg_mod
_reg_path = os.environ.get(
    "REGISTRY_FILE",
    str(Path(__file__).parent / "user_registry.yml"),
)
_reg_mod.REGISTRY_FILE = Path(_reg_path)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    user_name: str
    service_name: str
    # Optional project root — when provided, any relative path below is resolved against it
    # (equivalent to -pr in the CLI)
    project_root: str | None = None
    # Exactly one of these must be provided:
    #   compose_template_path — path to an existing Jinja2 .yml.j2 template
    #   compose_file_path     — path to a plain docker-compose.yml (auto-converted)
    compose_template_path: str | None = None
    compose_file_path: str | None = None
    # Optionally one of:
    nginx_conf_template_path: str | None = None
    nginx_conf_file_path: str | None = None
    env_file_path: str | None = None
    label: str = "0"
    domain: str = "localhost"
    passwd: str = "123456"
    volumes: dict[str, str] = {}
    build_args: dict[str, str] | None = None
    https: bool = False
    fullchain: str | None = None
    privkey: str | None = None

    @field_validator("user_name", "service_name")
    @classmethod
    def _validate_name(cls, v: str, info) -> str:
        try:
            validation.validate_name(v, info.field_name)
        except validation.ValidationError as e:
            raise ValueError(str(e))
        return v

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        try:
            validation.validate_label(v)
        except validation.ValidationError as e:
            raise ValueError(str(e))
        return v

    from pydantic import model_validator

    @model_validator(mode="after")
    def _check_compose_source(self) -> "RegisterRequest":
        has_tpl = bool(self.compose_template_path)
        has_file = bool(self.compose_file_path)
        if not has_tpl and not has_file:
            raise ValueError("one of compose_template_path or compose_file_path is required")
        if has_tpl and has_file:
            raise ValueError("compose_template_path and compose_file_path are mutually exclusive")
        if self.nginx_conf_template_path and self.nginx_conf_file_path:
            raise ValueError("nginx_conf_template_path and nginx_conf_file_path are mutually exclusive")
        if self.https:
            if not self.fullchain:
                raise ValueError("fullchain is required when https=True")
            if not self.privkey:
                raise ValueError("privkey is required when https=True")
        return self


class RebuildRequest(BaseModel):
    no_cache: bool = False
    build_args: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="User Provision Tool", version="1.0.0")


# ---------------------------------------------------------------------------
# GET /health  — liveness probe (no docker call)
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# GET /docker/ps  — list all containers
# ---------------------------------------------------------------------------

@app.get("/docker/ps")
def docker_ps_list() -> list[dict[str, Any]]:
    """Return all Docker containers (docker ps -a)."""
    return docker_ops.docker_ps()


# ---------------------------------------------------------------------------
# GET /docker/stats  — per-container resource stats
# ---------------------------------------------------------------------------

@app.get("/docker/stats")
def docker_stats() -> list[dict[str, Any]]:
    """Return docker stats snapshot."""
    return docker_ops.docker_stats_snapshot()


# ---------------------------------------------------------------------------
# GET /docker/info  — docker host info
# ---------------------------------------------------------------------------

@app.get("/docker/info")
def docker_info() -> dict[str, Any]:
    """Return docker system info (container counts, etc.)."""
    return docker_ops.docker_info()


# ---------------------------------------------------------------------------
# GET /host/stats  — host-level CPU/memory/disk
# ---------------------------------------------------------------------------

@app.get("/host/stats")
def host_stats() -> dict[str, Any]:
    """Return host-level CPU, memory, and disk usage."""
    import shutil, re
    stats: dict[str, Any] = {}

    # Memory
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    mem[parts[0].strip()] = int(parts[1].strip().split()[0])
        total = mem.get("MemTotal", 1)
        available = mem.get("MemAvailable", mem.get("MemFree", 0))
        stats["mem_percent"] = round((1 - available / total) * 100, 1)
        stats["mem_total_kb"] = total
        stats["mem_used_kb"] = total - available
    except Exception:
        stats["mem_percent"] = 0

    # CPU
    try:
        with open("/proc/stat") as f:
            cpu_line = f.readline()
        parts = [int(x) for x in cpu_line.split()[1:]]
        idle = parts[3]
        total_cpu = sum(parts)
        stats["cpu_percent"] = round((1 - idle / total_cpu) * 100, 1) if total_cpu > 0 else 0
    except Exception:
        stats["cpu_percent"] = 0

    # Disk
    try:
        usage = shutil.disk_usage("/")
        stats["disk_percent"] = round((1 - usage.free / usage.total) * 100, 1)
        stats["disk_total_gb"] = round(usage.total / (1024**3), 1)
        stats["disk_free_gb"] = round(usage.free / (1024**3), 1)
    except Exception:
        stats["disk_percent"] = 0

    return stats


# ---------------------------------------------------------------------------
# Reconciliation helpers (called by gateway)
# ---------------------------------------------------------------------------

@app.get("/docker/container/{container}/exists")
def container_exists_ep(container: str) -> dict[str, Any]:
    return {"exists": docker_ops.container_exists(container)}

@app.get("/docker/container/{container}/running")
def container_running_ep(container: str) -> dict[str, Any]:
    return {"running": docker_ops.container_running(container)}

@app.post("/docker/network/{network}/connect/{container}")
def network_connect_ep(network: str, container: str) -> dict[str, Any]:
    docker_ops.network_connect(container, network)
    return {"connected": True}

@app.post("/docker/nginx/reload")
def nginx_reload_ep(container: str = "provision-nginx") -> dict[str, Any]:
    docker_ops.nginx_reload(container)
    return {"reloaded": True}


# ---------------------------------------------------------------------------
# POST /users  — register (async by default; ?sync=true to block)
# ---------------------------------------------------------------------------

@app.post("/users", status_code=202)
def register_user(
    req: RegisterRequest,
    sync: bool = Query(False, description="If true, block until registration completes"),
) -> dict[str, Any]:
    # --- Resolve project_root: bare name → SOURCE_PROJECTS_DIR/{name} ---
    resolved_root: Path | None = None
    if req.project_root:
        raw = Path(req.project_root)
        if raw.is_absolute() or raw.is_dir():
            resolved_root = raw
        else:
            resolved_root = SOURCE_PROJECTS_DIR / req.project_root
        if not resolved_root.is_dir():
            raise HTTPException(404, f"project_root not found: {resolved_root}")

    # --- Resolve paths relative to project_root when given ---
    def _resolve(p: str | None) -> str | None:
        if p is None:
            return None
        if resolved_root and not Path(p).is_absolute():
            return str(resolved_root / p)
        return p

    compose_file_path      = _resolve(req.compose_file_path)
    compose_template_path  = _resolve(req.compose_template_path)
    nginx_conf_file_path   = _resolve(req.nginx_conf_file_path)
    nginx_conf_template_path = _resolve(req.nginx_conf_template_path)
    env_file_path          = _resolve(req.env_file_path)

    # --- Resolve compose template (convert plain file if needed) ---
    if compose_file_path:
        src = Path(compose_file_path)
        if not src.exists():
            raise HTTPException(404, f"compose_file_path not found: {compose_file_path}")
        template_out = str(src.parent / f"{src.stem}.yml.j2")
        try:
            compose_file_to_template(str(src), template_out, service_name_hint=req.service_name)
        except Exception as e:
            raise HTTPException(422, f"could not convert compose file: {e}")
        compose_template = template_out
    else:
        if not Path(compose_template_path).exists():
            raise HTTPException(404, f"compose_template_path not found: {compose_template_path}")
        compose_template = compose_template_path

    # --- Resolve nginx template (convert plain file if needed) ---
    # Extract compose service names so proxy_pass targets matching a compose
    # service name can be rewritten to use {{ container_prefix }}.
    _compose_svc_names: list[str] = []
    try:
        _compose_src = compose_file_path or compose_template_path
        if _compose_src:
            _compose_svc_names = get_compose_service_names(_compose_src)
    except Exception:
        pass

    nginx_template: str | None = None
    if nginx_conf_file_path:
        src = Path(nginx_conf_file_path)
        if not src.exists():
            raise HTTPException(404, f"nginx_conf_file_path not found: {nginx_conf_file_path}")
        template_out = str(src.parent / f"{src.name}.j2")
        try:
            nginx_file_to_template(
                str(src), template_out, req.service_name,
                compose_service_names=_compose_svc_names or None,
            )
        except Exception as e:
            raise HTTPException(422, f"could not convert nginx conf file: {e}")
        nginx_template = template_out
    elif nginx_conf_template_path:
        if not Path(nginx_conf_template_path).exists():
            raise HTTPException(404, f"nginx_conf_template_path not found: {nginx_conf_template_path}")
        nginx_template = nginx_conf_template_path

    # --- Build kwargs for provisioner call ---
    prov_kwargs = dict(
        user_name=req.user_name,
        service_name=req.service_name,
        label=req.label,
        compose_template=compose_template,
        output_dir=Path(compose_template).parent,
        nginx_output_dir=GENERATED_DIR,
        volumes=req.volumes or None,
        user_data_dir=USER_DATA_DIR,
        passwd=req.passwd,
        nginx_template=nginx_template,
        domain=req.domain,
        env_file=env_file_path,
        nginx_container=NGINX_CONTAINER,
        build_args=req.build_args,
        https=req.https,
        fullchain=req.fullchain,
        privkey=req.privkey,
        ssl_base_dir=str(SSL_DIR),
    )

    if sync:
        # Backward-compatible blocking call
        try:
            result = provisioner.register_user(**prov_kwargs)
        except ValueError as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(500, f"docker compose up failed: {e}")
        return {
            "status": "registered",
            "entry": result["entry"],
            "volume_warnings": result["volume_warnings"],
            "copied_env": result.get("copied_env"),
        }

    # Async: submit task, return task_id immediately
    task_id = task_manager.submit("register", provisioner.register_user, **prov_kwargs)
    return {
        "task_id": task_id,
        "status": "pending",
        "type": "register",
        "message": f"Registration queued.  Poll GET /tasks/{task_id} for status.",
    }


# ---------------------------------------------------------------------------
# DELETE /users/{user_name}/services/{service_name}/{label}  — remove (async)
# ---------------------------------------------------------------------------

@app.delete("/users/{user_name}/services/{service_name}/{label}")
def remove_user(
    user_name: str, service_name: str, label: str,
    sync: bool = Query(False, description="If true, block until removal completes"),
) -> dict[str, Any]:
    prov_kwargs = dict(
        user_name=user_name,
        service_name=service_name,
        label=label,
        nginx_container=NGINX_CONTAINER,
    )

    if sync:
        try:
            provisioner.remove_user(**prov_kwargs)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except RuntimeError as e:
            raise HTTPException(500, f"docker compose down failed: {e}")
        return {"status": "removed", "user_name": user_name, "service_name": service_name, "label": label}

    task_id = task_manager.submit("remove", provisioner.remove_user, **prov_kwargs)
    return {
        "task_id": task_id,
        "status": "pending",
        "type": "remove",
        "message": f"Removal queued.  Poll GET /tasks/{task_id} for status.",
    }


# ---------------------------------------------------------------------------
# POST /users/{user_name}/services/{service_name}/{label}/rebuild  (async)
# ---------------------------------------------------------------------------

@app.post("/users/{user_name}/services/{service_name}/{label}/rebuild")
def rebuild_user(
    user_name: str, service_name: str, label: str,
    req: RebuildRequest = RebuildRequest(),
    sync: bool = Query(False, description="If true, block until rebuild completes"),
) -> dict[str, Any]:
    prov_kwargs = dict(
        user_name=user_name,
        service_name=service_name,
        label=label,
        no_cache=req.no_cache,
        build_args=req.build_args,
    )

    if sync:
        try:
            provisioner.rebuild_user(**prov_kwargs)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except RuntimeError as e:
            raise HTTPException(500, f"rebuild failed: {e}")
        return {"status": "rebuilt", "user_name": user_name, "service_name": service_name, "label": label}

    task_id = task_manager.submit("rebuild", provisioner.rebuild_user, **prov_kwargs)
    return {
        "task_id": task_id,
        "status": "pending",
        "type": "rebuild",
        "message": f"Rebuild queued.  Poll GET /tasks/{task_id} for status.",
    }


# ---------------------------------------------------------------------------
# P5: PUT /users/{user_name}/{service_name}/{label}/password  — change password
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POST /users/{user_name}/services/{service_name}/{label}/up  — start containers
# ---------------------------------------------------------------------------

@app.post("/users/{user_name}/services/{service_name}/{label}/up")
def start_user_service(
    user_name: str, service_name: str, label: str,
) -> dict[str, Any]:
    """Start a user's service containers (docker compose up -d)."""
    try:
        provisioner.start_service(
            user_name=user_name,
            service_name=service_name,
            label=label,
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"message": "Service started.", "status": "up"}


# ---------------------------------------------------------------------------
# POST /users/{user_name}/services/{service_name}/{label}/down  — stop containers
# ---------------------------------------------------------------------------

@app.post("/users/{user_name}/services/{service_name}/{label}/down")
def stop_user_service(
    user_name: str, service_name: str, label: str,
) -> dict[str, Any]:
    """Stop a user's service containers (docker compose stop)."""
    try:
        provisioner.stop_service(
            user_name=user_name,
            service_name=service_name,
            label=label,
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"message": "Service stopped.", "status": "down"}


# ---------------------------------------------------------------------------
# P5: PUT /users/{user_name}/{service_name}/{label}/password  — change password
# ---------------------------------------------------------------------------

class PasswordChangeRequest(BaseModel):
    passwd: str

@app.put("/users/{user_name}/services/{service_name}/{label}/password")
def change_user_password(
    user_name: str, service_name: str, label: str,
    req: PasswordChangeRequest,
) -> dict[str, Any]:
    try:
        result = provisioner.change_password(
            user_name=user_name,
            service_name=service_name,
            label=label,
            passwd=req.passwd,
            nginx_container=NGINX_CONTAINER,
        )
    except KeyError as e:
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"message": "Password updated. Nginx reloaded.", **result}


# ---------------------------------------------------------------------------
# P3: GET /nginx/connections  — nginx connection state
# ---------------------------------------------------------------------------

@app.get("/nginx/connections")
def get_nginx_connections() -> dict[str, Any]:
    """Return nginx connection state:
    (a) list of networks provision-nginx is connected to
    (b) list of *.nginx.conf files in GENERATED_DIR
    (c) parsed upstreams
    """
    from pathlib import Path
    import re

    # (a) Networks provision-nginx is connected to
    nginx_info = docker_ops.container_inspect(NGINX_CONTAINER)
    connected_networks = {}
    if nginx_info:
        connected_networks = nginx_info.get("NetworkSettings", {}).get("Networks", {})

    # (b) List nginx conf files
    conf_files = sorted(Path(GENERATED_DIR).glob("*.nginx.conf"))
    conf_file_names = [f.name for f in conf_files]

    # (c) Parse upstreams from each conf file
    upstreams = []
    for cf in conf_files:
        try:
            content = cf.read_text()
            server_name = None
            proxy_pass = None
            m = re.search(r"server_name\s+([^;]+);", content)
            if m:
                server_name = m.group(1).strip().split()[0]
            m = re.search(r"proxy_pass\s+(https?://[^;]+);", content)
            if m:
                proxy_pass = m.group(1).strip()
            upstreams.append({
                "conf_file": cf.name,
                "server_name": server_name,
                "proxy_pass": proxy_pass,
            })
        except Exception:
            pass

    return {
        "nginx_container": NGINX_CONTAINER,
        "connected_networks": list(connected_networks.keys()),
        "conf_files": conf_file_names,
        "upstreams": upstreams,
    }


# ---------------------------------------------------------------------------
# P4: POST /nginx/reconnect-all  — reconnect nginx to all user networks
# ---------------------------------------------------------------------------

@app.post("/nginx/reconnect-all")
def reconnect_all() -> dict[str, Any]:
    """Iterate all entries in user_registry, reconnect provision-nginx to each
    network (idempotent), then reload nginx."""
    all_users = registry.get_all_users()
    networks = set()
    for entry in all_users:
        net = entry.get("network_name", "")
        if net:
            networks.add(net)

    reconnected = 0
    for net in sorted(networks):
        try:
            docker_ops.network_connect(NGINX_CONTAINER, net)
            reconnected += 1
        except Exception:
            pass

    docker_ops.nginx_reload(NGINX_CONTAINER)

    return {
        "total_networks": len(networks),
        "reconnected": reconnected,
        "nginx_reloaded": True,
    }


# ---------------------------------------------------------------------------
# P6: GET /users/{user}/{service}/{label}/containers/{container}/logs
# ---------------------------------------------------------------------------

@app.get("/users/{user_name}/services/{service_name}/{label}/containers/{container}/logs")
def get_container_logs(
    user_name: str, service_name: str, label: str, container: str,
    tail: int = Query(100, description="Number of log lines to return"),
) -> dict[str, Any]:
    """Get container logs for a specific container."""
    entry = registry.get_user_service(user_name, service_name, label)
    if not entry:
        raise HTTPException(404, f"No registration found for {user_name}/{service_name}/{label}.")
    
    prefix = template_engine.container_prefix(service_name, user_name, label)
    full_container_name = f"{prefix}{container}"
    
    # Verify container exists
    if not docker_ops.container_exists(full_container_name):
        raise HTTPException(404, f"Container not found: {full_container_name}")
    
    logs = docker_ops.container_logs(full_container_name, tail=tail)
    return {
        "container": full_container_name,
        "tail": tail,
        "logs": logs.splitlines(),
    }


# ---------------------------------------------------------------------------
# GET /tasks  — list all tasks in the pool
# ---------------------------------------------------------------------------

@app.get("/tasks")
def list_tasks() -> dict[str, Any]:
    tasks = task_manager.list_all()
    return {
        "count": len(tasks),
        "tasks": tasks,
    }


# ---------------------------------------------------------------------------
# GET /tasks/{task_id}  — query async task status
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}")
def get_task_status(task_id: str) -> dict[str, Any]:
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(404, f"Task not found: {task_id}")
    return task


# ---------------------------------------------------------------------------
# DELETE /tasks/{task_id}  — cancel a pending or running task
# ---------------------------------------------------------------------------

@app.delete("/tasks/{task_id}")
def cancel_task(task_id: str) -> dict[str, Any]:
    cancelled = task_manager.cancel(task_id)
    if not cancelled:
        task = task_manager.get(task_id)
        if task is None:
            raise HTTPException(404, f"Task not found: {task_id}")
        raise HTTPException(409, f"Task already in terminal state: {task['status']}")
    return {"task_id": task_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# P2: GET /tasks/{task_id}/log  — SSE build log streaming
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/log")
async def stream_task_log(
    task_id: str,
    tail: int = Query(200, description="Number of recent lines to send first"),
    follow: bool = Query(True, description="Keep streaming new lines"),
):
    """Stream build log via Server-Sent Events."""
    import asyncio
    from pathlib import Path

    log_file = Path(os.environ.get("DOCKER_OPS_LOG", str(GENERATED_DIR / "docker_ops.log")))

    async def log_generator():
        # Send initial tail
        if log_file.exists():
            try:
                lines = log_file.read_text().splitlines()
                recent = lines[-tail:] if len(lines) > tail else lines
                for line in recent:
                    yield f"data: {line}\n\n"
            except Exception:
                pass

        if not follow:
            yield "event: done\ndata: {}\n\n"
            return

        # Poll for new lines
        last_size = log_file.stat().st_size if log_file.exists() else 0
        while True:
            await asyncio.sleep(1)
            try:
                if log_file.exists():
                    current_size = log_file.stat().st_size
                    if current_size > last_size:
                        with open(log_file, "r") as f:
                            f.seek(last_size)
                            new_data = f.read()
                            for line in new_data.splitlines():
                                if line.strip():
                                    yield f"data: {line}\n\n"
                        last_size = current_size
            except Exception:
                break

    return StreamingResponse(
        log_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# GET /users  — all users status
# ---------------------------------------------------------------------------

@app.get("/users")
def get_all_users_status() -> dict[str, Any]:
    return _compute_status(None)


# ---------------------------------------------------------------------------
# GET /users/{user_name}  — single user status
# ---------------------------------------------------------------------------

@app.get("/users/{user_name}")
def get_user_status(user_name: str) -> dict[str, Any]:
    entries = registry.get_user(user_name)
    if not entries:
        raise HTTPException(404, f"No registrations found for user '{user_name}'.")
    return _compute_status(user_name)


# ---------------------------------------------------------------------------
# Status computation (shared logic)
# ---------------------------------------------------------------------------

def _compute_status(filter_user: str | None) -> dict[str, Any]:
    running = {c["name"]: c["status"] for c in docker_ops.docker_ps()}
    all_users = registry.get_all_users()

    if filter_user:
        user_names = list({u["user_name"] for u in all_users if u.get("user_name") == filter_user})
    else:
        user_names = list({u["user_name"] for u in all_users})

    return {"user_status": [_status_for_user(name, running) for name in sorted(user_names)]}


def _expected_services(compose_file: str) -> list[str]:
    if not Path(compose_file).exists():
        return []
    import yaml
    with open(compose_file) as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("services", {}).keys())


def _status_for_user(user_name: str, running: dict[str, str]) -> dict[str, Any]:
    entries = registry.get_user(user_name)
    healthy_services, unhealthy_services, missing_services = [], [], []

    for entry in entries:
        compose_file = entry.get("compose_file_path", "")
        prefix = template_engine.container_prefix(
            entry["service_name"], entry["user_name"], entry["label"]
        )
        expected_keys = _expected_services(compose_file)

        healthy: dict[str, str] = {}
        unhealthy: dict[str, str] = {}
        missing: dict[str, str] = {}

        for svc_key in expected_keys:
            cname = f"{prefix}{svc_key}"
            if cname in running:
                status = running[cname]
                if "unhealthy" in status.lower():
                    unhealthy[cname] = status
                elif "up" in status.lower() or "healthy" in status.lower():
                    healthy[cname] = status
                else:
                    unhealthy[cname] = status
            else:
                missing[cname] = "not running"

        svc: dict[str, Any] = {
            "service_name": entry["service_name"],
            "label": entry["label"],
            "compose_template_path": entry.get("compose_template_path", ""),
            "compose_file_path": compose_file,
            "healthy_containers": healthy,
            "unhealthy_containers": unhealthy,
            "missing_containers": missing,
        }

        if not Path(compose_file).exists():
            missing_services.append(svc)
        elif len(healthy) == len(expected_keys) and not unhealthy and not missing:
            healthy_services.append(svc)
        elif not healthy and not unhealthy:
            missing_services.append(svc)
        else:
            unhealthy_services.append(svc)

    total = len(entries)
    return {
        "user_name": user_name,
        "summary": {
            "expected_services_#": total,
            "healthy_services_#": len(healthy_services),
            "unhealthy_services_#": len(unhealthy_services) + len(missing_services),
        },
        "healthy_services": healthy_services,
        "unhealthy_services": unhealthy_services,
        "missing_services": missing_services,
    }
