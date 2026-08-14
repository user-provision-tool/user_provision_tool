"""Tests for subnet_manager.py."""

import os
from unittest import mock

import pytest

# Import after potential env modifications
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib import subnet_manager


class TestSubnetSize:
    def test_single_container_gives_slash_29(self):
        # 1 + 2(headroom) + 1(gateway) = 4 usable IPs; /30 has only 2 usable
        # (gateway + 1 host), so nginx (which joins every network) gets no IP.
        assert subnet_manager.subnet_size_for_containers(1) == 29

    def test_two_containers_gives_slash_29(self):
        # 2 + 2(headroom) + 1(gateway) = 5, fits in /29 (6 usable)
        assert subnet_manager.subnet_size_for_containers(2) == 29

    def test_three_containers_gives_slash_29(self):
        # 3 + 2 + 1 = 6, exactly fits in /29 (6 usable)
        assert subnet_manager.subnet_size_for_containers(3) == 29

    def test_four_containers_gives_slash_28(self):
        # 4 + 2 + 1 = 7 -> /28 (14 usable); /29 has only 6
        assert subnet_manager.subnet_size_for_containers(4) == 28

    def test_six_containers_gives_slash_28(self):
        # 6 + 2 + 1 = 9 -> /28 (14 usable)
        assert subnet_manager.subnet_size_for_containers(6) == 28

    def test_fourteen_containers_gives_slash_27(self):
        # 14 + 2 + 1 = 17 -> /27 (30 usable)
        assert subnet_manager.subnet_size_for_containers(14) == 27

    def test_thirty_containers_gives_slash_26(self):
        # 30 + 2 + 1 = 33 -> /26 (62 usable)
        assert subnet_manager.subnet_size_for_containers(30) == 26

    def test_sixty_two_containers_gives_slash_25(self):
        # 62 + 2 + 1 = 65 -> /25 (126 usable)
        assert subnet_manager.subnet_size_for_containers(62) == 25

    def test_large_gives_slash_24(self):
        assert subnet_manager.subnet_size_for_containers(200) == 24

    def test_addr_count(self):
        assert subnet_manager.subnet_addr_count(30) == 4
        assert subnet_manager.subnet_addr_count(29) == 8
        assert subnet_manager.subnet_addr_count(24) == 256


class TestAllocateSubnet:
    @mock.patch.dict(os.environ, {"SUBNET_POOLS": "10.0.0.0/16"})
    def test_allocates_from_pool(self):
        subnet_manager._load_env()
        result = subnet_manager.allocate_subnet(1, [])
        assert result is not None
        assert "subnet" in result
        assert "gateway" in result

    @mock.patch.dict(os.environ, {"SUBNET_POOLS": ""})
    def test_returns_none_when_disabled(self):
        subnet_manager._load_env()
        result = subnet_manager.allocate_subnet(5, [])
        assert result is None

    @mock.patch.dict(os.environ, {"SUBNET_POOLS": "10.0.0.0/16"})
    def test_pool_exhaustion_raises_runtime_error(self):
        subnet_manager._load_env()
        # The whole /16 pool is already allocated → no free block → RuntimeError
        with pytest.raises(RuntimeError):
            subnet_manager.allocate_subnet(1, ["10.0.0.0/16"])

    @mock.patch.dict(os.environ, {"SUBNET_POOLS": "10.0.0.0/16"})
    def test_skips_already_allocated(self):
        subnet_manager._load_env()
        # Allocate first subnet
        r1 = subnet_manager.allocate_subnet(1, [])
        assert r1 is not None
        # Second allocation should get a different subnet
        r2 = subnet_manager.allocate_subnet(1, [r1["subnet"]])
        assert r2 is not None
        assert r2["subnet"] != r1["subnet"]

    @mock.patch.dict(os.environ, {"SUBNET_POOLS": "10.0.0.0/16"})
    def test_alignment_lands_on_aligned_slot(self):
        import ipaddress
        subnet_manager._load_env()
        r = subnet_manager.allocate_subnet(1, [])  # /29 → 2 /30-slots
        assert r is not None
        net = ipaddress.IPv4Network(r["subnet"])
        pool_start = int(ipaddress.IPv4Network("10.0.0.0/16").network_address)
        slot = (int(net.network_address) - pool_start) // 4
        # /29 blocks must be 2-aligned (0, 2, 4, ...)
        assert slot % 2 == 0


class TestPoolStats:
    @mock.patch.dict(os.environ, {"SUBNET_POOLS": "10.0.0.0/16,172.16.0.0/16"})
    def test_returns_pool_stats(self):
        subnet_manager._load_env()
        stats = subnet_manager.get_pool_stats([])
        assert stats["enabled"] is True
        assert len(stats["pools"]) == 2
        assert stats["pools"][0]["total_slots"] > 0

    @mock.patch.dict(os.environ, {"SUBNET_POOLS": ""})
    def test_returns_disabled(self):
        subnet_manager._load_env()
        stats = subnet_manager.get_pool_stats([])
        assert stats["enabled"] is False

    @mock.patch.dict(os.environ, {"SUBNET_POOLS": "10.0.0.0/16"})
    def test_counts_used_slots(self):
        subnet_manager._load_env()
        stats = subnet_manager.get_pool_stats([{"subnet": "10.0.0.0/28"}])
        assert stats["enabled"] is True
        # /28 = 16 addresses = 4 slots
        assert stats["pools"][0]["used_slots"] == 4

    @mock.patch.dict(os.environ, {"SUBNET_POOLS": "10.0.0.0/16"})
    def test_stats_include_spec_fields(self):
        """Gap G2: the §8.3 response contract fields are present."""
        subnet_manager._load_env()
        entry = {"subnet": "10.0.0.0/28", "user_name": "alice", "service_name": "myapp", "label": "0"}
        stats = subnet_manager.get_pool_stats([entry])
        assert stats["enabled"] is True
        pool = stats["pools"][0]
        assert pool["free_slots"] == pool["total_slots"] - pool["used_slots"]
        assert pool["exhausted"] is False
        assert "overall" in stats
        assert stats["overall"]["total_slots"] == stats["overall"]["used_slots"] + stats["overall"]["free_slots"]
        assert stats["allocations"] == [{"user": "alice", "service": "myapp", "label": "0", "subnet": "10.0.0.0/28"}]


class TestGetAllocatedSubnets:
    def test_extracts_subnets(self):
        entries = [
            {"subnet": "10.0.0.0/29", "user_name": "alice"},
            {"subnet": "10.0.0.8/29", "user_name": "bob"},
            {"user_name": "charlie"},  # no subnet
        ]
        subnets = subnet_manager.get_allocated_subnets(entries)
        assert len(subnets) == 2
        assert "10.0.0.0/29" in subnets
        assert "10.0.0.8/29" in subnets
