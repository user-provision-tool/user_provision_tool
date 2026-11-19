"""Unit tests for the per-instance DNS proxy (G101 T1/T2)."""

from __future__ import annotations

import ipaddress
import json
import struct

import yaml

from lib import dns_proxy, dns_proxy_config, subnet_manager


def _query(name: str = "target.example.") -> bytes:
    wire = b"".join(bytes([len(part)]) + part.encode()
                    for part in name.rstrip(".").split(".")) + b"\x00"
    return (struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
            + wire + struct.pack("!HH", 1, 1))


def _positive(packet: bytes) -> bytes:
    end = dns_proxy._question_end(packet)
    header = packet[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0)
    answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4) + b"\x0a\x00\x00\x09"
    return header + packet[12:end] + answer


def _nxdomain(packet: bytes) -> bytes:
    end = dns_proxy._question_end(packet)
    header = packet[:2] + struct.pack("!HHHHH", 0x8183, 1, 0, 0, 0)
    return header + packet[12:end]


class TestRuntime:
    def test_health_name_answers_with_fixed_a(self):
        out = dns_proxy.resolve(_query(dns_proxy.HEALTH_NAME),
                                dns_proxy.Config(enabled=False))
        assert struct.unpack("!H", out[6:8])[0] == 1
        assert out.endswith(b"\x7f\x00\x00\x01")

    def test_positive_cross_answer_is_returned(self, monkeypatch):
        cross = _positive(_query())
        calls = []

        monkeypatch.setattr(dns_proxy, "_resolve_name", lambda name: "10.9.9.9")
        def _exchange(packet, upstream, timeout):
            calls.append(upstream)
            return cross if upstream == "10.9.9.9" else _nxdomain(packet)
        monkeypatch.setattr(dns_proxy, "_udp_exchange", _exchange)

        out = dns_proxy.resolve(_query(), dns_proxy.Config(
            enabled=True, resolver_name="cross-resolver", timeout=1.0))
        assert out == cross
        assert calls == ["10.9.9.9"]

    def test_no_positive_cross_answer_falls_back_to_docker_dns(self, monkeypatch):
        calls = []
        monkeypatch.setattr(dns_proxy, "_resolve_name", lambda name: "10.9.9.9")
        def _exchange(packet, upstream, timeout):
            calls.append(upstream)
            if upstream == "10.9.9.9":
                return _nxdomain(packet)          # not a positive answer
            return _positive(packet)              # Docker DNS answer
        monkeypatch.setattr(dns_proxy, "_udp_exchange", _exchange)

        out = dns_proxy.resolve(_query(), dns_proxy.Config(
            enabled=True, resolver_name="cross-resolver", timeout=1.0))
        assert calls == ["10.9.9.9", "127.0.0.11"]
        assert dns_proxy._positive(out)

    def test_disabled_skips_the_cross_resolver(self, monkeypatch):
        calls = []
        monkeypatch.setattr(dns_proxy, "_udp_exchange",
                            lambda p, upstream, t: calls.append(upstream) or _positive(p))
        dns_proxy.resolve(_query(), dns_proxy.Config(enabled=False))
        assert calls == ["127.0.0.11"]


class TestComposeInjection:
    def _inject(self):
        rendered = yaml.safe_dump({
            "services": {
                "web": {"image": "app", "networks": ["net"]},
                "hostmode": {"image": "app", "network_mode": "host"},
            },
            "networks": {"net": {"name": "net"}},
        }, sort_keys=False)
        return yaml.safe_load(dns_proxy_config.inject_compose(
            rendered,
            network_name="net",
            container_prefix="p-",
            subnet="10.0.0.0/28",
            gateway="10.0.0.1",
            proxy_ip="10.0.0.2",
            ip_range="10.0.0.8/29",
            config_dir="/tmp/cfg",
        ))

    def test_proxy_service_and_app_wiring(self):
        data = self._inject()
        web = data["services"]["web"]
        assert web["dns"] == ["10.0.0.2"]
        assert web["depends_on"]["dns-proxy"] == {"condition": "service_healthy"}
        proxy = data["services"]["dns-proxy"]
        assert proxy["container_name"] == "p-dns-proxy"
        assert "dns" not in proxy and "depends_on" not in proxy
        assert proxy["networks"]["net"]["ipv4_address"] == "10.0.0.2"
        assert proxy["volumes"] == ["/tmp/cfg:/etc/dns-proxy:ro"]
        assert "healthcheck" in proxy

    def test_network_mode_service_is_left_alone(self):
        data = self._inject()
        assert "dns" not in data["services"]["hostmode"]
        assert "depends_on" not in data["services"]["hostmode"]

    def test_network_ipam_has_the_dynamic_range(self):
        data = self._inject()
        cfg = data["networks"]["net"]["ipam"]["config"][0]
        assert cfg["subnet"] == "10.0.0.0/28"
        assert cfg["gateway"] == "10.0.0.1"
        assert cfg["ip_range"] == "10.0.0.8/29"


class TestSubnetReservation:
    def test_proxy_is_outside_the_dynamic_range(self, monkeypatch):
        monkeypatch.setenv("SUBNET_POOLS", "10.0.0.0/16")
        subnet_manager._load_env()
        result = subnet_manager.allocate_subnet(1, [], reserve_proxy=True)
        assert result is not None
        subnet = ipaddress.ip_network(result["subnet"])
        proxy = ipaddress.ip_address(result["proxy_ip"])
        dynamic = ipaddress.ip_network(result["ip_range"], strict=False)
        assert proxy in subnet
        assert proxy not in dynamic
        assert ipaddress.ip_address(result["gateway"]) in subnet
        assert subnet.prefixlen == 28  # proxy + gateway + an aligned block

    def test_legacy_layout_is_unchanged_without_the_flag(self, monkeypatch):
        monkeypatch.setenv("SUBNET_POOLS", "10.0.0.0/16")
        subnet_manager._load_env()
        result = subnet_manager.allocate_subnet(1, [])
        assert result == {"subnet": "10.0.0.0/29", "gateway": "10.0.0.1"}


class TestTcpAndFallbacks:
    def test_tcp_positive_cross_answer_is_returned(self, monkeypatch):
        cross = _positive(_query())
        calls = []
        monkeypatch.setattr(dns_proxy, "_resolve_name", lambda name: "10.9.9.9")
        monkeypatch.setattr(dns_proxy, "_tcp_exchange",
                            lambda p, u, t: calls.append(u) or cross)
        monkeypatch.setattr(dns_proxy, "_udp_exchange",
                            lambda p, u, t: (_ for _ in ()).throw(AssertionError("UDP used")))
        out = dns_proxy.resolve(_query(), dns_proxy.Config(
            enabled=True, resolver_name="cross-resolver", timeout=1.0), tcp=True)
        assert out == cross
        assert calls == ["10.9.9.9"]

    def test_tcp_no_positive_falls_back_to_docker_dns(self, monkeypatch):
        calls = []
        monkeypatch.setattr(dns_proxy, "_resolve_name", lambda name: "10.9.9.9")
        def _exchange(p, u, t):
            calls.append(u)
            return _nxdomain(p) if u == "10.9.9.9" else _positive(p)
        monkeypatch.setattr(dns_proxy, "_tcp_exchange", _exchange)
        out = dns_proxy.resolve(_query(), dns_proxy.Config(
            enabled=True, resolver_name="cross-resolver", timeout=1.0), tcp=True)
        assert calls == ["10.9.9.9", "127.0.0.11"]
        assert dns_proxy._positive(out)

    def test_unresolved_resolver_name_falls_back(self, monkeypatch):
        calls = []
        monkeypatch.setattr(dns_proxy, "_resolve_name", lambda name: None)
        monkeypatch.setattr(dns_proxy, "_udp_exchange",
                            lambda p, u, t: calls.append(u) or _positive(p))
        out = dns_proxy.resolve(_query(), dns_proxy.Config(
            enabled=True, resolver_name="missing", timeout=1.0))
        assert calls == ["127.0.0.11"]
        assert dns_proxy._positive(out)

    def test_docker_dns_failure_is_servfail(self, monkeypatch):
        monkeypatch.setattr(dns_proxy, "_resolve_name", lambda name: None)
        monkeypatch.setattr(dns_proxy, "_udp_exchange", lambda p, u, t: None)
        out = dns_proxy.resolve(_query(), dns_proxy.Config(enabled=False))
        assert dns_proxy._rcode(out) == "SERVFAIL"

    def test_no_cache_two_queries_hit_the_upstream_twice(self, monkeypatch):
        calls = []
        monkeypatch.setattr(dns_proxy, "_udp_exchange",
                            lambda p, u, t: calls.append(u) or _positive(p))
        cfg = dns_proxy.Config(enabled=False)
        dns_proxy.resolve(_query(), cfg)
        dns_proxy.resolve(_query(), cfg)
        assert calls == ["127.0.0.11", "127.0.0.11"]

    def test_config_reader_reloads_on_change(self, tmp_path):
        path = tmp_path / "dns-proxy.json"
        path.write_text(json.dumps({"enabled": False}))
        reader = dns_proxy.ConfigReader(path)
        assert reader.get().enabled is False
        path.write_text(json.dumps({
            "enabled": True, "resolver_name": "subnet-acl-cross-resolver",
            "fallback": "127.0.0.11", "timeout": 1.0,
        }))
        cfg = reader.get()
        assert cfg.enabled is True
        assert cfg.resolver_name == "subnet-acl-cross-resolver"

    def test_missing_config_is_disabled(self, tmp_path):
        reader = dns_proxy.ConfigReader(tmp_path / "missing.json")
        assert reader.get().enabled is False
