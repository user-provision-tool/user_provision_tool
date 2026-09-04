"""cli/register.py — register a user and start their service containers.

Usage:
    python cli/register.py -u USER_NAME -sn SERVICE_NAME
        -pr PROJECT_ROOT
        { -tc COMPOSE_TEMPLATE | -fc COMPOSE_FILE [-fc COMPOSE_FILE ...] }
        [-v KEY=VALUE ...] [-e ENV_FILE ...] [--profile NAME ...]
        [-tn NGINX_TEMPLATE | -fn NGINX_FILE]
        [-l LABEL] [-d DOMAIN]

-pr  Project root directory.
       Accepts a full path, relative path, or just a directory name.
       A bare name (e.g. "myapp") is resolved to SOURCE_PROJECTS_DIR/myapp.
       • All generated files (rendered compose, nginx conf, htpasswd) are
         written here so they live alongside the Dockerfile / source tree
       • 'build: .' contexts in compose files resolve correctly
-tc  Filename of an existing Jinja2 compose template (.yml.j2) inside project root.
-fc  Filename of a plain docker-compose.yml inside project root.
     Converted to a .j2 template before registration.  Mutually exclusive
     with -tc.
-tn  Filename of an existing Jinja2 nginx conf template (.nginx.conf.j2) inside project root.
-fn  Filename of a plain nginx conf file inside project root.
     Converted to a .j2 template before registration.  Mutually exclusive
     with -tn.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure lib/ is importable when running as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

import os

from lib import auth, provisioner, template_engine, validation
from lib.compose_converter import compose_file_to_template, get_compose_service_names
from lib.nginx_converter import nginx_file_to_template

GENERATED_DIR = Path(
    os.environ.get("GENERATED_DIR", str(Path(__file__).parent.parent / "generated"))
)

USER_DATA_DIR = Path(
    os.environ.get("USER_DATA_DIR", str(Path(__file__).parent.parent / "user_data"))
)

SOURCE_PROJECTS_DIR = Path(
    os.environ.get("SOURCE_PROJECTS_DIR", str(Path(__file__).parent.parent / "source_projects"))
)


def _validate_nginx_proxy_targets(
    nginx_conf: Path, compose_service_names: list[str]
) -> None:
    """Validate that every proxy_pass host in *nginx_conf* is a compose service.

    Prints an error and exits with code 1 if any host does not match.
    """
    import re
    content = nginx_conf.read_text()
    hosts: list[str] = []
    for m in re.finditer(r'proxy_pass\s+https?://([a-zA-Z0-9_-]+)', content):
        hosts.append(m.group(1))

    compose_set = {n.lower() for n in compose_service_names}
    unknown = [h for h in hosts if h.lower() not in compose_set]

    if unknown:
        print(
            f"ERROR: nginx conf proxy_pass target(s) do not match any compose service name.\n"
            f"  Compose services: {sorted(compose_service_names)}\n"
            f"  Unknown proxy_pass host(s): {sorted(unknown)}\n"
            f"  Update {nginx_conf.name} so every proxy_pass host matches a service key from docker-compose.yml.",
            file=sys.stderr,
        )
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Register a user and start their service containers.")
    p.add_argument("-u", "--user-name", required=True, help="User name")
    p.add_argument("-sn", "--service-name", required=True, help="Service name")
    p.add_argument("-pr", "--project-root", required=True,
        help=(
            "Project root directory. Can be a full path, a relative path, or just a "
            "directory name — in which case it is resolved to "
            "SOURCE_PROJECTS_DIR/{name}. Generated files are written here; all source "
            "filenames (-tc/-fc/-tn/-fn) are resolved relative to this directory."
        ),
    )

    src_group = p.add_mutually_exclusive_group(required=True)
    src_group.add_argument(
        "-tc", "--compose-template",
        help="Filename of an existing Jinja2 compose template (.yml.j2) inside project root.",
    )
    src_group.add_argument(
        "-fc", "--compose-file",
        action="append",
        default=[],
        metavar="FILE",
        help=(
            "Filename of a plain docker-compose.yml inside project root. Converted "
            "to .yml.j2 before registration. Repeatable: two or more files are "
            "merged with compose-spec semantics (docker compose -f a -f b config "
            "--no-interpolate --no-path-resolution) into <first>.merged.yml.j2."
        ),
    )

    p.add_argument("-v", "--volume", action="append", default=[],
        metavar="KEY=VALUE",
        help=(
            "Volume mapping (can be repeated): template_volume=host_path. "
            "When omitted, paths are auto-generated under USER_DATA_DIR."
        ),
    )
    p.add_argument("-e", "--env-file", action="append", default=[],
        metavar="FILE",
        help=(
            "Path to an env file for docker compose variable substitution "
            "(repeatable; order preserved, later files win)."
        ),
    )
    p.add_argument("--profile", action="append", default=[],
        metavar="NAME",
        help=(
            "Compose profile to activate (repeatable). Default (no flag) runs "
            "only default services plus the implicit \"\" profile."
        ),
    )

    nginx_group = p.add_mutually_exclusive_group()
    nginx_group.add_argument(
        "-tn", "--nginx-template",
        help="Filename of an existing Jinja2 nginx conf template (.nginx.conf.j2) inside project root.",
    )
    nginx_group.add_argument(
        "-fn", "--nginx-file",
        help="Filename of a plain nginx conf file inside project root. Converted to .j2 before registration.",
    )

    p.add_argument("-l", "--label", default="0", help="Label (digits only, default: 0)")
    p.add_argument("-d", "--domain", default="localhost", help="Domain name for nginx hostname")
    p.add_argument("--https", action="store_true", default=False, help="Enable HTTPS support (requires --fullchain and --privkey)")
    p.add_argument("--fullchain", default=None, help="Path to fullchain.pem certificate file (required when --https is set)")
    p.add_argument("--privkey", default=None, help="Path to privkey.pem private key file (required when --https is set)")
    p.add_argument("--build-arg", action="append", default=[], dest="build_args_raw",
        metavar="KEY=VALUE", help="Build argument passed to docker compose build (can be repeated, e.g. --build-arg HTTP_PROXY=http://proxy:8080)")
    return p.parse_args()


def parse_volumes(raw: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            print(f"ERROR: volume mapping must be KEY=VALUE, got: {item}", file=sys.stderr)
            sys.exit(1)
        k, v = item.split("=", 1)
        result[k.strip()] = v.strip()
    return result


def _parse_build_args(raw: list[str]) -> dict[str, str] | None:
    if not raw:
        return None
    result: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            print(f"ERROR: --build-arg must be KEY=VALUE, got: {item}", file=sys.stderr)
            sys.exit(1)
        k, v = item.split("=", 1)
        result[k.strip()] = v.strip()
    return result


def check_volumes(
    compose_template: str, provided: dict[str, str]
) -> dict[str, str]:
    """Cross-check template volumes against provided mapping. Prompt for confirmation on mismatch."""
    expected = template_engine.extract_template_volumes(compose_template)
    missing = [k for k in expected if k not in provided]
    extra = [k for k in provided if k not in expected]

    if missing or extra:
        if missing:
            print(f"WARNING: Volume(s) declared in template but not provided: {missing}")
        if extra:
            print(f"WARNING: Volume(s) provided but not found in template: {extra}")
        answer = input("Continue anyway? [y/N]: ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)
    return provided


def main() -> None:
    args = parse_args()

    # --- Validate inputs ---
    try:
        validation.validate_name(args.user_name, "user_name")
        validation.validate_name(args.service_name, "service_name")
        validation.validate_label(args.label)
    except validation.ValidationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # --- Resolve project root ---
    # A bare name (no path separators, doesn't exist as-is) resolves to SOURCE_PROJECTS_DIR/{name}
    raw_pr = Path(args.project_root)
    if not raw_pr.is_absolute() and os.sep not in args.project_root and "/" not in args.project_root and not raw_pr.is_dir():
        project_root = (SOURCE_PROJECTS_DIR / args.project_root).resolve()
    else:
        project_root = raw_pr.resolve()
    if not project_root.is_dir():
        print(f"ERROR: project root not found: {project_root}", file=sys.stderr)
        sys.exit(1)

    # --- Resolve compose template (-tc used directly; -fc triggers conversion) ---
    compose_sources: list[str] = []
    if args.compose_template:
        compose_src = project_root / args.compose_template
        if not compose_src.exists():
            print(f"ERROR: compose template not found: {compose_src}", file=sys.stderr)
            sys.exit(1)
        compose_template = str(compose_src)
    else:
        compose_files = [project_root / f for f in args.compose_file]
        for cf in compose_files:
            if not cf.exists():
                print(f"ERROR: compose file not found: {cf}", file=sys.stderr)
                sys.exit(1)
        compose_sources = [str(cf) for cf in compose_files]
        if len(compose_files) == 1:
            src = compose_files[0]
            template_out = str(src.parent / f"{src.stem}.yml.j2")
        else:
            # ≥2 files: ordered merge → ONE template named <first>.merged.yml.j2
            from lib.compose_merge import merge_compose_files
            first = compose_files[0]
            merged_out = str(first.parent / f"{first.stem}.merged.yml")
            template_out = str(first.parent / f"{first.stem}.merged.yml.j2")
            try:
                merge_compose_files(compose_sources, merged_out)
            except RuntimeError as exc:
                print(f"ERROR: compose merge failed: {exc}", file=sys.stderr)
                sys.exit(1)
            src = Path(merged_out)
            print(f"[0/4] Merged {len(compose_files)} compose files: {merged_out}")
        try:
            src_to_key = compose_file_to_template(
                str(src), template_out, service_name_hint=args.service_name
            )
        except ValueError as exc:
            print(f"ERROR: could not convert compose file: {exc}", file=sys.stderr)
            sys.exit(1)
        compose_template = template_out
        print(f"[0/4] Converted compose file to template: {compose_template}")
        if src_to_key:
            col = max(len(k) for k in src_to_key.values())
            for s, k in src_to_key.items():
                print(f"       volume key '{k}'  ←  {s}")
            print("       Pass these with -v KEY=HOST_PATH (or omit to be warned).")

    env_files: list[str] = []
    for ef in args.env_file:
        resolved = str(Path(ef).resolve())
        if not Path(resolved).exists():
            print(f"ERROR: env file not found: {resolved}", file=sys.stderr)
            sys.exit(1)
        env_files.append(resolved)

    # --- Resolve nginx template (-tn used directly; -fn triggers conversion) ---
    # Extract compose service names so proxy_pass targets matching a compose
    # service name can be rewritten to use {{ container_prefix }}.
    _compose_svc_names: list[str] = []
    try:
        _compose_svc_names = get_compose_service_names(compose_template)
    except Exception:
        pass

    nginx_template: str | None = None
    if args.nginx_template:
        nginx_src = project_root / args.nginx_template
        if not nginx_src.exists():
            print(f"ERROR: nginx template not found: {nginx_src}", file=sys.stderr)
            sys.exit(1)
        nginx_template = str(nginx_src)
    elif args.nginx_file:
        nginx_src = project_root / args.nginx_file
        if not nginx_src.exists():
            print(f"ERROR: nginx file not found: {nginx_src}", file=sys.stderr)
            sys.exit(1)

        # Validate: every proxy_pass host must reference a compose service
        if _compose_svc_names:
            _validate_nginx_proxy_targets(nginx_src, _compose_svc_names)

        template_out = str(nginx_src.parent / f"{nginx_src.name}.j2")
        try:
            nginx_file_to_template(
                str(nginx_src), template_out, args.service_name,
                compose_service_names=_compose_svc_names or None,
            )
        except Exception as exc:
            print(f"ERROR: could not convert nginx file: {exc}", file=sys.stderr)
            sys.exit(1)
        nginx_template = template_out
        print(f"[0/4] Converted nginx file to template: {nginx_template}")

    # --- Volume validation / auto-generation ---
    volumes = parse_volumes(args.volume)
    if volumes:
        # Manual mode: validate provided mapping against template
        volumes = check_volumes(compose_template, volumes)
    # else: provisioner will auto-generate under USER_DATA_DIR

    # --- Password prompt ---
    try:
        passwd = auth.prompt_password(args.user_name)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # --- Parse build args ---
    build_args = _parse_build_args(args.build_args_raw)

    # --- HTTPS validation ---
    # Full paths are checked immediately; bare filenames are resolved by the provisioner
    # against /provision/ssl/{domain}/.
    if args.https:
        if not args.fullchain:
            print("ERROR: --https requires --fullchain.", file=sys.stderr)
            sys.exit(1)
        if not args.privkey:
            print("ERROR: --https requires --privkey.", file=sys.stderr)
            sys.exit(1)
        # Quick check for full paths (bare filenames are deferred to provisioner)
        for label, val in [("fullchain", args.fullchain), ("privkey", args.privkey)]:
            p = Path(val)
            if (p.is_absolute() or "/" in val) and not p.is_file():
                print(f"ERROR: --{label} file not found: {val}", file=sys.stderr)
                sys.exit(1)

    # --- Core registration workflow ---
    try:
        result = provisioner.register_user(
            user_name=args.user_name,
            service_name=args.service_name,
            label=args.label,
            compose_template=compose_template,
            output_dir=project_root,
            nginx_output_dir=GENERATED_DIR,
            volumes=volumes or None,
            user_data_dir=USER_DATA_DIR,
            passwd=passwd,
            nginx_template=nginx_template,
            domain=args.domain,
            env_files=env_files or None,
            profiles=args.profile or None,
            compose_sources=compose_sources or None,
            build_args=build_args,
            https=args.https,
            fullchain=args.fullchain,
            privkey=args.privkey,
            ssl_base_dir=os.environ.get("PROVISION_SSL_DIR", str(GENERATED_DIR.parent / "ssl")),
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"ERROR starting containers: {e}", file=sys.stderr)
        sys.exit(1)

    entry = result["entry"]
    print(f"[1/4] Registered user '{args.user_name}' in user_registry.yml")
    print(f"[2/4] Generated compose file: {entry['compose_file_path']}")
    if result["copied_env"]:
        print(f"[2/4] Copied env file to: {result['copied_env']}")
    if nginx_template:
        if entry["passwd"]:
            print(f"[3/4] Generated htpasswd file: {entry['htpasswd_path']}")
        else:
            print("[3/4] No password set; skipping htpasswd file.")
        print(f"[3/4] Generated nginx conf: {entry['nginx_conf_path']}")
        if args.https:
            print(f"[3/4] HTTPS enabled — cert: {entry.get('ssl_certificate_path', '')}")
            print(f"[3/4] HTTPS enabled — key:  {entry.get('ssl_certificate_key_path', '')}")
    else:
        print("[3/4] No nginx template provided; skipping.")
    print(f"\nDone. User '{args.user_name}' registered and containers started.")


if __name__ == "__main__":
    main()
