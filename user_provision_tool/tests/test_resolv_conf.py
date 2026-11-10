"""G83 — the generic extra-resolver / extra-network manager.

The Docker socket is faked: these are unit tests of the fan-out, the atomic
write-and-verify, the baseline restore, and the "runs only for field-set
services" rules. The live multi-container path is exercised by §7's
integration trace.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from lib import resolv_conf

TWO = "nameserver 172.28.0.3\nnameserver 127.0.0.11\n"
DEFAULT = "nameserver 127.0.0.11\noptions ndots:0\n"


class FakeDocker:
    def __init__(self, containers, running=None):
        self.files = {c: DEFAULT for c in containers}
        self.running = {c: (running or {}).get(c, True) for c in containers}
        self.connected: set[str] = set()
        self.writes: list[str] = []

    def read(self, container, path):
        return self.files[container]

    def write(self, container, path, content):
        self.files[container] = content
        self.writes.append(container)

    def is_connected(self, network, container):
        return container in self.connected

    def connect(self, container, network):
        self.connected.add(container)


@pytest.fixture()
def fake(monkeypatch):
    def _install(entry, running=None):
        f = FakeDocker(entry["container_names"], running)
        stored: dict = {}

        monkeypatch.setattr(resolv_conf.registry, "get_service_by_id",
                            lambda sid: entry)
        monkeypatch.setattr(resolv_conf, "_running", lambda c: f.running[c])
        monkeypatch.setattr(resolv_conf.docker_ops, "container_read_file", f.read)
        monkeypatch.setattr(resolv_conf.docker_ops, "container_write_file", f.write)
        monkeypatch.setattr(resolv_conf.docker_ops, "network_connected_to_container",
                            f.is_connected)
        monkeypatch.setattr(resolv_conf.docker_ops, "network_connect", f.connect)
        monkeypatch.setattr(resolv_conf.docker_ops, "network_disconnect",
                            lambda container, network: f.connected.discard(container))

        @contextmanager
        def _conn():
            yield object()

        monkeypatch.setattr(resolv_conf.db, "connection", _conn)
        monkeypatch.setattr(resolv_conf.registry, "update_instance",
                            lambda conn, sid, **kw: stored.update(kw))
        return f, stored
    return _install


def _entry(*containers):
    return {"service_id": "sid", "deleted_at": None,
            "container_names": list(containers)}


def test_apply_writes_two_lines_every_container_and_stores(fake):
    f, stored = fake(_entry("c1", "c2"))
    out = resolv_conf.apply_service("sid", "172.28.0.3", "subnet_acl_cross")
    assert f.files["c1"] == TWO and f.files["c2"] == TWO
    assert f.connected == {"c1", "c2"}
    assert stored == {"extra_resolver": "172.28.0.3",
                      "extra_network": "subnet_acl_cross"}
    assert out["containers"] == ["c1", "c2"]


def test_apply_skips_non_running_and_stores_pending(fake):
    """G91 — a stopped container is pending, not a failure."""
    f, stored = fake(_entry("c1", "c2"), running={"c2": False})
    out = resolv_conf.apply_service("sid", "172.28.0.3", "subnet_acl_cross")
    assert out["containers"] == ["c1"]
    assert out["skipped"] == ["c2"]
    assert out["applied"] == 1 and out["pending"] == 1 and out["stored"] is True
    assert stored["extra_resolver"] == "172.28.0.3"

    # Zero running containers: the pair is still persisted for the monitor.
    f2, stored2 = fake(_entry("x"), running={"x": False})
    out2 = resolv_conf.apply_service("sid", "172.28.0.3", "subnet_acl_cross")
    assert out2["applied"] == 0 and out2["stored"] is True
    assert out2["containers"] == [] and out2["skipped"] == ["x"]
    assert stored2 == {"extra_resolver": "172.28.0.3",
                       "extra_network": "subnet_acl_cross"}
    assert f2.writes == []


def test_apply_failure_does_not_overwrite_the_stored_pair(fake, monkeypatch):
    """G91 — the store happens only after every running container succeeded."""
    f, stored = fake(_entry("c1", "c2"))
    stored["extra_resolver"] = "old-resolver"
    stored["extra_network"] = "subnet_acl_cross"

    def _boom(container, resolver_ip, network):
        raise RuntimeError("docker write failed")

    monkeypatch.setattr(resolv_conf, "_apply_one", _boom)
    with pytest.raises(RuntimeError):
        resolv_conf.apply_service("sid", "172.28.0.3", "subnet_acl_cross")
    assert stored == {"extra_resolver": "old-resolver",
                      "extra_network": "subnet_acl_cross"}


def test_clear_restores_captured_default_and_disconnects(fake):
    entry = _entry("c1")
    f, stored = fake(entry)
    resolv_conf.apply_service("sid", "172.28.0.3", "subnet_acl_cross")
    assert f.files["c1"] == TWO
    # The stored pair now belongs to the entry the clear reads back.
    entry["extra_resolver"] = "172.28.0.3"
    entry["extra_network"] = "subnet_acl_cross"
    resolv_conf.clear_service("sid")
    assert f.files["c1"] == DEFAULT
    assert stored == {"extra_resolver": None, "extra_network": None}
    assert f.connected == set()


def test_reapply_skips_stopped_and_uses_stored_values(fake):
    f, _ = fake(_entry("c1", "c2"), running={"c2": False})
    entry = _entry("c1", "c2") | {"extra_resolver": "10.0.0.9",
                                  "extra_network": "net9"}
    out = resolv_conf.reapply_entry(entry)
    assert out["containers"] == ["c1"]
    assert f.files["c1"] == "nameserver 10.0.0.9\nnameserver 127.0.0.11\n"
    assert "c2" not in f.connected


def test_startup_pass_only_field_set_services(fake, monkeypatch):
    f, _ = fake(_entry("c1"))
    set_entry = _entry("c1") | {"extra_resolver": "172.28.0.3",
                                "extra_network": "subnet_acl_cross"}
    plain = {"service_id": "plain", "deleted_at": None,
             "container_names": [], "status": "running"}
    monkeypatch.setattr(resolv_conf.registry, "get_all_users",
                        lambda: [set_entry, plain])
    result = resolv_conf.startup_pass()
    assert result["applied"] == 1
    assert f.files["c1"] == TWO


def test_compose_recreate_name_is_matched():
    by_container = {"subnet-acl-svc-user_a-0-app": {"service_id": "s"}}
    assert resolv_conf._match_container(
        "abc123_subnet-acl-svc-user_a-0-app", by_container
    ) == "subnet-acl-svc-user_a-0-app"
    assert resolv_conf._match_container("other", by_container) is None
