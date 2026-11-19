"""Unit tests for the DNS-proxy control plane (-api side, G101 T3)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from lib import dns_proxy_config as dpc
from lib import template_engine


@pytest.fixture()
def entry():
    return {
        "service_id": "s1", "user_name": "u", "service_name": "svc",
        "label": "0", "container_names": ["p-svc", "p-dns-proxy"],
        "extra_resolver": None, "extra_network": None,
    }


@pytest.fixture()
def harness(monkeypatch, tmp_path, entry):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dpc.registry, "get_service_by_id", lambda sid: entry)
    monkeypatch.setattr(dpc.registry, "get_all_users", lambda: [entry])
    updates = []
    monkeypatch.setattr(dpc.registry, "update_instance",
                        lambda conn, sid, **fields: updates.append((sid, fields)))
    @contextmanager
    def _conn():
        yield object()
    monkeypatch.setattr(dpc.db, "connection", _conn)
    connects, disconnects = [], []
    monkeypatch.setattr(dpc.docker_ops, "network_connect",
                        lambda name, network: connects.append((name, network)))
    monkeypatch.setattr(dpc.docker_ops, "network_disconnect",
                        lambda name, network: disconnects.append((name, network)))
    monkeypatch.setattr(template_engine, "container_prefix",
                        lambda service, user, label: "p-")
    return {"updates": updates, "connects": connects, "disconnects": disconnects}


def test_write_and_read_config(tmp_path):
    cfg = dpc.config_dir_for("u", "svc", "0", tmp_path)
    dpc.write_config(cfg, resolver_name="subnet-acl-cross-resolver",
                     network="subnet_acl_cross", proxy_ip="10.0.0.2")
    data = dpc.read_config(cfg)
    assert data["enabled"] is True
    assert data["resolver_name"] == "subnet-acl-cross-resolver"
    assert data["network"] == "subnet_acl_cross"
    assert data["proxy_ip"] == "10.0.0.2"
    assert data["generation"]
    assert (cfg / (dpc.PROXY_CONFIG_NAME + ".generated")).exists()


def test_apply_service_writes_enabled_config_and_membership(tmp_path, harness, entry):
    cfg = dpc.config_dir_for("u", "svc", "0", tmp_path)
    dpc.write_config(cfg, resolver_name=None, network=None, proxy_ip="10.0.0.2",
                     enabled=False)
    result = dpc.apply_service("s1", resolver_name="subnet-acl-cross-resolver",
                               network="subnet_acl_cross")
    data = dpc.read_config(cfg)
    assert data["enabled"] is True
    assert data["resolver_name"] == "subnet-acl-cross-resolver"
    assert data["proxy_ip"] == "10.0.0.2"
    assert harness["connects"] == [
        ("p-dns-proxy", "subnet_acl_cross"), ("p-svc", "subnet_acl_cross")]
    assert result["resolver_name"] == "subnet-acl-cross-resolver"
    assert harness["updates"] == [("s1", {
        "extra_resolver": "subnet-acl-cross-resolver",
        "extra_network": "subnet_acl_cross"})]


def test_clear_service_disables_and_disconnects(tmp_path, harness, entry):
    entry["extra_resolver"] = "subnet-acl-cross-resolver"
    entry["extra_network"] = "subnet_acl_cross"
    cfg = dpc.config_dir_for("u", "svc", "0", tmp_path)
    dpc.write_config(cfg, resolver_name="subnet-acl-cross-resolver",
                     network="subnet_acl_cross", proxy_ip="10.0.0.2")
    dpc.clear_service("s1")
    data = dpc.read_config(cfg)
    assert data["enabled"] is False
    assert data["resolver_name"] is None
    assert data["proxy_ip"] == "10.0.0.2"
    assert harness["disconnects"] == [
        ("p-dns-proxy", "subnet_acl_cross"), ("p-svc", "subnet_acl_cross")]
    assert harness["updates"] == [("s1", {
        "extra_resolver": None, "extra_network": None})]


def test_startup_pass_reapplies_only_enabled_services(tmp_path, harness, entry):
    entry["extra_resolver"] = "subnet-acl-cross-resolver"
    entry["extra_network"] = "subnet_acl_cross"
    cfg = dpc.config_dir_for("u", "svc", "0", tmp_path)
    dpc.write_config(cfg, resolver_name=None, network=None, proxy_ip="10.0.0.2",
                     enabled=False)
    result = dpc.startup_pass()
    assert result["applied"] == 1
    assert not result["errors"]
    assert dpc.read_config(cfg)["enabled"] is True


def test_get_state_returns_the_read_model(tmp_path, harness, entry):
    cfg = dpc.config_dir_for("u", "svc", "0", tmp_path)
    dpc.write_config(cfg, resolver_name="subnet-acl-cross-resolver",
                     network="subnet_acl_cross", proxy_ip="10.0.0.2")
    entry["extra_resolver"] = "subnet-acl-cross-resolver"
    entry["extra_network"] = "subnet_acl_cross"
    state = dpc.get_state("s1")
    assert state["service_id"] == "s1"
    assert state["proxy_container"] == "p-dns-proxy"
    assert state["proxy_address"] == "10.0.0.2"
    assert state["enabled"] is True
    assert state["resolver_name"] == "subnet-acl-cross-resolver"
    assert state["config_generation"]
