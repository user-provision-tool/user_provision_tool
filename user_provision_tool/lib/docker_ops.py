"""Subprocess wrappers for docker compose commands.

Inside the deployed container, docker socket access is granted directly
(no sudo needed). On the host, use sudo externally or add user to docker group.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path


_LOG_FILE = os.environ.get("DOCKER_OPS_LOG", "")


def _write_log(text: str) -> None:
    if _LOG_FILE:
        try:
            Path(_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
            with open(_LOG_FILE, "a") as f:
                f.write(text)
        except Exception:
            pass


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(args)}", flush=True)
    _write_log(f"+ {' '.join(args)}\n")
    # Enable BuildKit so Dockerfiles using --mount=type=cache and other
    # BuildKit features work correctly.
    env = {**os.environ, "DOCKER_BUILDKIT": "1"}

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _read_stdout(pipe) -> None:
        for line in iter(pipe.readline, ""):
            print(line, end="", flush=True)
            _write_log(line)
            stdout_lines.append(line)

    def _read_stderr(pipe) -> None:
        for line in iter(pipe.readline, ""):
            print(line, end="", file=sys.stderr, flush=True)
            _write_log(line)
            stderr_lines.append(line)

    with subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as proc:
        t_out = threading.Thread(target=_read_stdout, args=(proc.stdout,), daemon=True)
        t_err = threading.Thread(target=_read_stderr, args=(proc.stderr,), daemon=True)
        t_out.start()
        t_err.start()
        proc.wait()
        # Close write ends so reader threads see EOF and exit
        proc.stdout.close()  # type: ignore[union-attr]
        proc.stderr.close()  # type: ignore[union-attr]
        t_out.join()
        t_err.join()

    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)

    if check and proc.returncode != 0:
        detail = (stderr or stdout or "").strip()
        raise RuntimeError(
            f"Command failed (exit {proc.returncode}): {' '.join(args)}"
            + (f"\n{detail}" if detail else "")
        )
    return subprocess.CompletedProcess(args, proc.returncode, stdout=stdout, stderr=stderr)


def _compose_base(compose_file: str, env_file: str | None, project_name: str | None) -> list[str]:
    cmd = ["docker", "compose", "-f", compose_file]
    if project_name:
        cmd += ["--project-name", project_name]
    if env_file:
        cmd += ["--env-file", env_file]
    return cmd


def compose_up(compose_file: str, env_file: str | None = None, project_name: str | None = None) -> None:
    _run(_compose_base(compose_file, env_file, project_name) + ["up", "-d"])


def compose_down(compose_file: str, env_file: str | None = None, project_name: str | None = None) -> None:
    _run(_compose_base(compose_file, env_file, project_name) + ["down"])


def compose_stop(compose_file: str, env_file: str | None = None, project_name: str | None = None) -> None:
    """Stop containers without removing them (docker compose stop)."""
    _run(_compose_base(compose_file, env_file, project_name) + ["stop"])


def compose_down_by_project(project_name: str) -> None:
    """Tear down a Compose project by project name alone (no compose file needed).

    Useful as a fallback when the per-user compose file has been lost but the
    containers and networks still exist under *project_name*.
    """
    _run(["docker", "compose", "-p", project_name, "down", "--remove-orphans"])


def compose_build(compose_file: str, no_cache: bool = False, env_file: str | None = None, project_name: str | None = None, build_args: dict[str, str] | None = None) -> None:
    cmd = _compose_base(compose_file, env_file, project_name) + ["build"]
    if no_cache:
        cmd.append("--no-cache")
    if build_args:
        for key, value in build_args.items():
            cmd += ["--build-arg", f"{key}={value}"]
    _run(cmd)


def network_connect(container: str, network: str) -> None:
    """Connect *container* to *network*. Silently no-ops if already connected."""
    _run(["docker", "network", "connect", network, container], check=False)


def network_disconnect(container: str, network: str) -> None:
    """Disconnect *container* from *network*. Silently no-ops if not connected."""
    _run(["docker", "network", "disconnect", network, container], check=False)


def nginx_reload(container: str) -> None:
    """Send a reload signal to nginx inside *container*."""
    _run(["docker", "exec", container, "nginx", "-s", "reload"], check=False)


def docker_ps() -> list[dict[str, str]]:
    """Return list of running containers as dicts with keys: name, status, image."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
        text=True,
        capture_output=True,
    )
    containers = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            containers.append({
                "name": parts[0].strip(),
                "status": parts[1].strip(),
                "image": parts[2].strip() if len(parts) > 2 else "",
            })
    return containers


def docker_stats_snapshot() -> list[dict[str, str]]:
    """Return a one-shot snapshot of docker stats (no-stream)."""
    result = subprocess.run(
        [
            "docker", "stats", "--no-stream",
            "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}",
        ],
        text=True,
        capture_output=True,
    )
    stats = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            stats.append({
                "name": parts[0].strip(),
                "cpu": parts[1].strip(),
                "mem": parts[2].strip(),
            })
    return stats


def network_list() -> list[str]:
    """Return list of Docker network names."""
    result = subprocess.run(
        ["docker", "network", "ls", "--format", "{{.Name}}"],
        text=True, capture_output=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def network_inspect(network: str) -> dict | None:
    """Inspect a Docker network. Returns parsed JSON or None."""
    import json
    result = subprocess.run(
        ["docker", "network", "inspect", network],
        text=True, capture_output=True,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        return data[0] if data else None
    except (json.JSONDecodeError, IndexError):
        return None


def container_inspect(container: str) -> dict | None:
    """Inspect a Docker container. Returns parsed JSON or None."""
    import json
    result = subprocess.run(
        ["docker", "inspect", container],
        text=True, capture_output=True,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        return data[0] if data else None
    except (json.JSONDecodeError, IndexError):
        return None


def container_exists(container: str) -> bool:
    """Check if a container exists (running or stopped)."""
    result = subprocess.run(
        ["docker", "inspect", container],
        text=True, capture_output=True,
    )
    return result.returncode == 0


def container_running(container: str) -> bool:
    """Check if a container is running."""
    info = container_inspect(container)
    if info is None:
        return False
    return info.get("State", {}).get("Running", False)


def network_connected_to_container(network: str, container: str) -> bool:
    """Check if *container* is connected to *network*."""
    info = network_inspect(network)
    if info is None:
        return False
    containers = info.get("Containers", {})
    for cid, cdata in containers.items():
        if cdata.get("Name") == container:
            return True
    return False


def container_logs(container: str, tail: int = 100) -> str:
    """Get the last *tail* lines of a container's logs."""
    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), container],
        text=True, capture_output=True,
    )
    return result.stdout


def orphan_network_cleanup(network: str, nginx_container: str = "provision-nginx") -> bool:
    """Clean up an orphaned network. Disconnect nginx and remove if only nginx is left.
    Returns True if the network was removed."""
    info = network_inspect(network)
    if info is None:
        return False

    containers = info.get("Containers", {})
    # Check what's connected
    connected_names = {cdata.get("Name", "") for cdata in containers.values()}
    
    # If only provision-nginx is connected, clean up
    if connected_names == {nginx_container} or len(connected_names) == 1:
        network_disconnect(nginx_container, network)
        result = subprocess.run(
            ["docker", "network", "rm", network],
            text=True, capture_output=True,
        )
        return result.returncode == 0
    return False
