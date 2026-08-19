"""Subnet manager module — per-service minimal subnet allocation engine.

Design: subnet-management-design.md v3.0 Section 3-5.

Given a set of /16 pools (SUBNET_POOLS env var) and a container count, this module
dynamically sizes the smallest subnet that fits, allocates a subnet from the pool
using a bitmap, and tracks allocations via the registry YAML.

When SUBNET_POOLS is empty or unset, allocation is disabled (returns empty dict).
"""

from __future__ import annotations

import ipaddress
import os
import threading
from typing import Any


# ---- Configuration ----

SUBNET_POOLS: list[str] = []
SUBNET_HEADROOM: int = 2

_lock = threading.Lock()


def _load_env() -> None:
    """(Re)load pool config from environment. Idempotent."""
    global SUBNET_POOLS, SUBNET_HEADROOM
    pools_raw = os.environ.get("SUBNET_POOLS", "")
    if pools_raw.strip():
        SUBNET_POOLS = [p.strip() for p in pools_raw.split(",") if p.strip()]
    else:
        SUBNET_POOLS = []
    try:
        SUBNET_HEADROOM = int(os.environ.get("SUBNET_HEADROOM", "2"))
    except ValueError:
        SUBNET_HEADROOM = 2


# Load once at import
_load_env()


# ---- Sizing ----

def subnet_size_for_containers(container_count: int) -> int:
    """Return the smallest prefix-length (/30 .. /24) that fits *container_count*.

    Formula:
        needed = containers + HEADROOM + 1(gateway)   — USABLE host IPs

    ``needed`` counts *usable* host addresses (not network/broadcast). It must
    include the provision-nginx container, which joins EVERY user network to
    reverse-proxy the service (that is one of the two ``HEADROOM`` IPs), plus
    one IP for the subnet gateway (``.1``). A single-container service therefore
    needs 1 + 2(headroom) + 1(gateway) = 4 usable IPs, which requires a /29
    (6 usable) — a /30 has only 2 usable (gateway + 1 host) and leaves nginx
    with no address (it joins with an invalid IP, breaking DNS resolution).

    Match against USABLE host addresses per prefix:
        /30 = 2 usable,  /29 = 6 usable,  /28 = 14 usable, /27 = 30 usable,
        /26 = 62 usable, /25 = 126 usable, /24 = 254 usable
    """
    needed = container_count + SUBNET_HEADROOM + 1
    if needed <= 2:
        return 30
    if needed <= 6:
        return 29
    if needed <= 14:
        return 28
    if needed <= 30:
        return 27
    if needed <= 62:
        return 26
    if needed <= 126:
        return 25
    return 24


def subnet_addr_count(prefix_len: int) -> int:
    """Return total addresses in a subnet of given prefix length."""
    return 2 ** (32 - prefix_len)


# ---- Bitmap ----

def _slots_in_pool(pool_cidr: str) -> int:
    """Number of /30 slots in a /16 pool = 2^(30-16) = 16384."""
    try:
        net = ipaddress.IPv4Network(pool_cidr, strict=False)
    except ValueError:
        return 0
    if net.prefixlen > 16:
        return 0
    return 2 ** (30 - net.prefixlen)


def _build_bitmap(pool_cidr: str, allocated_subnets: list[str]) -> list[bool]:
    """Return a boolean bitmap where True = slot occupied.

    Each slot is a /30 within the pool. Allocated subnets larger than /30
    occupy multiple consecutive slots.
    """
    total = _slots_in_pool(pool_cidr)
    if total == 0:
        return []
    bitmap = [False] * total

    try:
        pool_net = ipaddress.IPv4Network(pool_cidr, strict=False)
    except ValueError:
        return bitmap

    pool_start = int(pool_net.network_address)

    for subnet_str in allocated_subnets:
        try:
            subnet = ipaddress.IPv4Network(subnet_str, strict=False)
        except ValueError:
            continue
        # Only mark if within this pool
        if not pool_net.supernet_of(subnet):
            continue
        slot_start = (int(subnet.network_address) - pool_start) // 4
        slot_count = max(1, subnet.num_addresses // 4)
        for i in range(slot_start, min(slot_start + slot_count, total)):
            bitmap[i] = True

    return bitmap


def _aligned_block_size(prefix_len: int) -> int:
    """Number of /30 slots needed for a subnet of given prefix length."""
    return subnet_addr_count(prefix_len) // 4


def allocate_subnet(
    container_count: int,
    allocated_subnets: list[str],
) -> dict[str, str] | None:
    """Allocate a subnet from the pool.

    Parameters
    ----------
    container_count : int
        Number of containers in the compose file.
    allocated_subnets : list[str]
        Subnets already allocated (from registry), e.g. ["10.0.0.0/29", ...].

    Returns
    -------
    dict | None
        {"subnet": "10.0.0.0/29", "gateway": "10.0.0.1"}; None if subnet
        management is disabled (SUBNET_POOLS empty).

    Raises
    ------
    RuntimeError
        If pools are configured but every free block is exhausted (Gap G6 —
        surfacing the "all pools exhausted" error instead of silently
        falling back to Docker auto-assign).
    """
    _load_env()
    if not SUBNET_POOLS:
        return None

    prefix_len = subnet_size_for_containers(container_count)
    block_slots = _aligned_block_size(prefix_len)

    with _lock:
        for pool_cidr in SUBNET_POOLS:
            try:
                pool_net = ipaddress.IPv4Network(pool_cidr, strict=False)
            except ValueError:
                continue

            total = _slots_in_pool(pool_cidr)
            if total == 0:
                continue

            bitmap = _build_bitmap(pool_cidr, allocated_subnets)

            # Linear scan with alignment
            pool_start = int(pool_net.network_address)
            for slot_idx in range(0, total - block_slots + 1):
                # Alignment check: block must be aligned to its own size
                if slot_idx % block_slots != 0:
                    continue
                # Check if all slots are free
                if any(bitmap[slot_idx:slot_idx + block_slots]):
                    continue
                # Allocate
                subnet_addr = pool_start + slot_idx * 4
                subnet = ipaddress.IPv4Network((subnet_addr, prefix_len))
                gateway = str(subnet.network_address + 1)
                return {"subnet": str(subnet), "gateway": gateway}

    raise RuntimeError(
        f"No free /{prefix_len} subnet block in any pool: {','.join(SUBNET_POOLS)}"
    )


def get_allocated_subnets(registry_entries: list[dict[str, Any]]) -> list[str]:
    """Extract allocated subnet CIDRs from registry entries."""
    subnets: list[str] = []
    for entry in registry_entries:
        subnet = entry.get("subnet", "")
        if subnet:
            subnets.append(subnet)
    return subnets


def get_host_allocated_subnets() -> list[str]:
    """Return subnets of live Docker networks that fall inside ``SUBNET_POOLS``.

    The registry is the provision system's own source of truth, but it cannot
    know about networks created by another stack on the same host (e.g. an
    integration test running a fresh registry alongside the live stack, or a
    recovery after the registry file was lost). Allocating blindly from the
    registry would pick subnets that already exist on the host and Docker then
    fails with ``Pool overlaps with other one on this address space``.

    We therefore treat every Docker network whose IPAM subnet lies inside any
    configured pool as already-allocated. Returns ``[]`` when subnet management
    is disabled or Docker inspection is unavailable.
    """
    _load_env()
    if not SUBNET_POOLS:
        return []
    try:
        from . import docker_ops as _dops  # lazy — avoid circular import

        pools = []
        for pool_cidr in SUBNET_POOLS:
            try:
                pools.append(ipaddress.IPv4Network(pool_cidr, strict=False))
            except ValueError:
                continue
        if not pools:
            return []

        host_subnets: list[str] = []
        for net_name in _dops.network_list():
            info = _dops.network_inspect(net_name)
            if not info:
                continue
            # IPAM can be None on some networks (e.g. the default bridge) —
            # guard before indexing Config.
            for cfg in (info.get("IPAM") or {}).get("Config", []) or []:
                cidr = cfg.get("Subnet", "")
                if not cidr:
                    continue
                try:
                    net = ipaddress.IPv4Network(cidr, strict=False)
                except ValueError:
                    continue
                if any(p.supernet_of(net) for p in pools):
                    host_subnets.append(str(net))
        return host_subnets
    except Exception:
        # Never block registration because Docker inspection failed.
        return []


def get_pool_stats(
    registry_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return pool usage statistics for the dashboard.

    Returns
    -------
    dict with keys:
        enabled: bool
        pools: list of {cidr, total_slots, used_slots, free_slots, used_pct, exhausted}
        overall: {total_slots, used_slots, free_slots}
        allocations: list of {user, service, label, subnet}
        headroom: int
    """
    _load_env()
    allocated = get_allocated_subnets(registry_entries)

    if not SUBNET_POOLS:
        return {"enabled": False, "pools": [], "headroom": SUBNET_HEADROOM, "message": "Subnet management disabled"}

    pools = []
    total_all = used_all = 0
    for pool_cidr in SUBNET_POOLS:
        total = _slots_in_pool(pool_cidr)
        bitmap = _build_bitmap(pool_cidr, allocated)
        used = sum(1 for b in bitmap if b)
        free = max(0, total - used)
        pct = round(used / total * 100, 1) if total > 0 else 0
        total_all += total
        used_all += used
        pools.append({
            "cidr": pool_cidr,
            "total_slots": total,
            "used_slots": used,
            "free_slots": free,
            "used_pct": pct,
            "exhausted": used >= total,
        })

    allocations = [
        {
            "user": e.get("user_name", ""),
            "service": e.get("service_name", ""),
            "label": e.get("label", ""),
            "subnet": e.get("subnet", ""),
        }
        for e in registry_entries
        if e.get("subnet")
    ]

    return {
        "enabled": True,
        "pools": pools,
        "overall": {
            "total_slots": total_all,
            "used_slots": used_all,
            "free_slots": total_all - used_all,
        },
        "allocations": allocations,
        "headroom": SUBNET_HEADROOM,
    }


def discover_subnet_from_docker(
    network_name: str,
) -> dict[str, str] | None:
    """Attempt to discover an existing subnet from Docker network inspection.

    Used for legacy compatibility: services deployed before subnet management
    was enabled still have Docker-assigned subnets that should be reflected
    in API responses.

    Requires the docker_ops module (imported lazily to avoid circular imports).
    """
    from . import docker_ops as _dops

    info = _dops.network_inspect(network_name)
    if not info:
        return None

    ipam = info.get("IPAM", {})
    configs = ipam.get("Config", [])
    if not configs:
        return None

    subnet_cidr = configs[0].get("Subnet", "")
    gateway = configs[0].get("Gateway", "")
    if not subnet_cidr:
        return None

    return {"subnet": subnet_cidr, "gateway": gateway}
