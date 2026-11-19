"""Per-instance DNS proxy (GRILLING G101).

The proxy is injected into every service's tenant network as a compose
sub-service.  The app containers get ``dns: [<reserved proxy address>]``; the
proxy:

* answers a reserved health name with a fixed A record (the compose healthcheck
  backs the app's ``depends_on: service_healthy``);
* for every other name, when enabled and the cross resolver is configured,
  asks the cross resolver **by container name** and returns a positive answer
  (NOERROR with at least one answer record);
* for every other result (REFUSED, NXDOMAIN, NODATA, SERVFAIL, timeout) asks
  its own Docker embedded DNS at 127.0.0.11 and returns that answer or failure;
* does not cache, does not classify names, and has no service-domain list.

The proxy's config file is the -api's runtime projection:

    {"enabled": bool, "resolver_name": str | None,
     "fallback": "127.0.0.11", "timeout": 2.0}

It is re-read on every query when the file changes (mtime/size), so an atomic
replace by the -api is effective on the next query — no process restart.

Run as::

    python -m lib.dns_proxy /etc/dns-proxy/dns-proxy.json
    python -m lib.dns_proxy --health /etc/dns-proxy/dns-proxy.json 127.0.0.1
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import socketserver
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("provision-api.dns_proxy")

DEFAULT_CONFIG = "/etc/dns-proxy/dns-proxy.json"
DEFAULT_FALLBACK = "127.0.0.11"
DEFAULT_TIMEOUT = 2.0
LISTEN_PORT = 53
HEALTH_NAME = "dns-proxy-health.invalid."
HEALTH_ANSWER = "127.0.0.1"


# ─── Config ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    resolver_name: str | None = None
    fallback: str = DEFAULT_FALLBACK
    timeout: float = DEFAULT_TIMEOUT


class ConfigReader:
    """Read the config file, re-reading it when it changes on disk.

    A single-file bind mount is not used (the -api replaces the file
    atomically); the proxy mounts the *directory*, so the new inode is
    visible.  This reader re-stats the file on each query and reloads on
    (mtime_ns, size) change.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._config = Config()

    def get(self) -> Config:
        try:
            st = self.path.stat()
        except OSError:
            # Missing config = disabled; never crash the resolver.
            return Config(enabled=False)
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if stamp == self._stamp:
                return self._config
            try:
                raw = json.loads(self.path.read_text())
            except (OSError, ValueError):
                return self._config
            cfg = Config(
                enabled=bool(raw.get("enabled")),
                resolver_name=(str(raw["resolver_name"]).strip()
                               if raw.get("resolver_name") else None),
                fallback=str(raw.get("fallback") or DEFAULT_FALLBACK),
                timeout=float(raw.get("timeout") or DEFAULT_TIMEOUT),
            )
            self._stamp = stamp
            self._config = cfg
            return cfg


# ─── DNS packet helpers ──────────────────────────────────────────────────────


def _question_end(packet: bytes) -> int | None:
    """Return the offset just past the first question, or None."""
    if len(packet) < 12:
        return None
    qdcount = struct.unpack("!H", packet[4:6])[0]
    if qdcount < 1:
        return None
    off = 12
    while off < len(packet):
        length = packet[off]
        if length == 0:
            off += 1
            break
        if length & 0xC0:  # compression pointer — not valid in a question
            return None
        off += 1 + length
    else:
        return None
    return off + 4 if off + 4 <= len(packet) else None


def _question_name(packet: bytes) -> str | None:
    """Decode the first question name (lower-case, trailing dot), or None."""
    end = _question_end(packet)
    if end is None:
        return None
    off = 12
    labels: list[str] = []
    while off < end:
        length = packet[off]
        if length == 0:
            break
        if length & 0xC0:
            return None
        labels.append(packet[off + 1:off + 1 + length].decode("ascii", "replace"))
        off += 1 + length
    return ".".join(labels).lower() + "." if labels else "."


def _question_type(packet: bytes) -> int | None:
    end = _question_end(packet)
    if end is None or end < 2:
        return None
    return struct.unpack("!H", packet[end - 4:end - 2])[0]


def _positive(response: bytes) -> bool:
    """NOERROR with at least one answer record."""
    if len(response) < 12:
        return False
    flags, qdcount, ancount = struct.unpack("!HHH", response[2:8])
    rcode = flags & 0x000F
    return bool(flags & 0x8000) and rcode == 0 and ancount >= 1


def _health_response(packet: bytes) -> bytes:
    """Build a NOERROR/A answer for the reserved health name."""
    end = _question_end(packet)
    if end is None:
        return _servfail(packet)
    # Header: copy ID, QR=1, AA=1, RD copied, RA=1, rcode 0; QD=1, AN=1.
    flags = 0x8400 | (struct.unpack("!H", packet[2:4])[0] & 0x0100)
    header = packet[0:2] + struct.pack("!HHHHH", flags, 1, 1, 0, 0)
    question = packet[12:end]
    # Answer: name pointer to the question, type A/class IN, TTL 5, RDLEN 4.
    answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 5, 4) + socket.inet_aton(HEALTH_ANSWER)
    return header + question + answer


def _servfail(packet: bytes) -> bytes:
    if len(packet) < 12:
        return packet
    flags = 0x8182  # QR=1, RD=1, RA=1, rcode SERVFAIL
    return packet[0:2] + struct.pack("!H", flags) + packet[4:12]


def _rcode(response: bytes | None) -> str:
    if not response or len(response) < 12:
        return "no-response"
    rcode = struct.unpack("!H", response[2:4])[0] & 0x000F
    return {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN",
            4: "NOTIMP", 5: "REFUSED"}.get(rcode, f"rcode{rcode}")


# ─── Upstream forwarding ─────────────────────────────────────────────────────


def _resolve_name(name: str) -> str | None:
    """Resolve an upstream container name through this netns's Docker DNS."""
    try:
        infos = socket.getaddrinfo(name, LISTEN_PORT, socket.AF_INET,
                                  socket.SOCK_DGRAM)
    except OSError:
        return None
    for info in infos:
        addr = info[4][0]
        if addr:
            return str(addr)
    return None


def _udp_exchange(packet: bytes, upstream: str, timeout: float) -> bytes | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (upstream, LISTEN_PORT))
        data, _ = sock.recvfrom(65535)
        return data
    except OSError:
        return None
    finally:
        sock.close()


def _tcp_exchange(packet: bytes, upstream: str, timeout: float) -> bytes | None:
    try:
        with socket.create_connection((upstream, LISTEN_PORT), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(struct.pack("!H", len(packet)) + packet)
            header = _recv_exact(sock, 2)
            if header is None:
                return None
            (length,) = struct.unpack("!H", header)
            return _recv_exact(sock, length)
    except OSError:
        return None


def _recv_exact(sock: socket.socket, count: int) -> bytes | None:
    chunks: list[bytes] = []
    while count:
        try:
            chunk = sock.recv(count)
        except OSError:
            return None
        if not chunk:
            return None
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def resolve(packet: bytes, cfg: Config, *, tcp: bool = False,
            reader: ConfigReader | None = None) -> bytes:
    """Run one query through the proxy's decision tree."""
    started = time.monotonic()
    name = _question_name(packet)
    qtype = _question_type(packet)
    upstream = "-"
    try:
        if name == HEALTH_NAME and qtype in (1, None):
            answer = _health_response(packet)
            upstream = "health"
            return answer
        if cfg.enabled and cfg.resolver_name:
            resolver_ip = _resolve_name(cfg.resolver_name)
            if resolver_ip:
                upstream = resolver_ip
                exchange = _tcp_exchange if tcp else _udp_exchange
                response = exchange(packet, resolver_ip, cfg.timeout)
                if response is not None and _positive(response):
                    return response
        upstream = cfg.fallback
        exchange = _tcp_exchange if tcp else _udp_exchange
        response = exchange(packet, cfg.fallback, cfg.timeout)
        return response if response is not None else _servfail(packet)
    finally:
        _log.info(
            "query name=%s type=%s upstream=%s rcode=%s duration_ms=%.1f",
            name or "-", qtype if qtype is not None else "-", upstream,
            _rcode(locals().get("answer", locals().get("response"))),
            (time.monotonic() - started) * 1000.0,
        )


# ─── Servers ─────────────────────────────────────────────────────────────────


class _UDPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        cfg = self.server.reader.get()  # type: ignore[attr-defined]
        sock.sendto(resolve(data, cfg), self.client_address)


class _TCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock = self.request
        try:
            sock.settimeout(5.0)
            header = _recv_exact(sock, 2)
            if header is None:
                return
            (length,) = struct.unpack("!H", header)
            packet = _recv_exact(sock, length)
            if packet is None:
                return
            cfg = self.server.reader.get()  # type: ignore[attr-defined]
            response = resolve(packet, cfg, tcp=True)
            sock.sendall(struct.pack("!H", len(response)) + response)
        except OSError:
            return


class _UDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(config_path: str, bind: str = "0.0.0.0") -> None:
    reader = ConfigReader(config_path)
    udp = _UDPServer((bind, LISTEN_PORT), _UDPHandler)
    tcp = _TCPServer((bind, LISTEN_PORT), _TCPHandler)
    udp.reader = reader  # type: ignore[attr-defined]
    tcp.reader = reader  # type: ignore[attr-defined]
    threading.Thread(target=tcp.serve_forever, daemon=True).start()
    _log.info("dns-proxy listening on %s:%s (config %s)", bind, LISTEN_PORT, config_path)
    udp.serve_forever()


def healthcheck(config_path: str, host: str = "127.0.0.1") -> int:
    """Send one UDP query for the health name to the proxy; 0 when it answers."""
    query_id = 0x5A5A
    name_wire = b"".join(
        bytes([len(label)]) + label.encode()
        for label in HEALTH_NAME.rstrip(".").split(".")
    ) + b"\x00"
    packet = (struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
              + name_wire + struct.pack("!HH", 1, 1))
    response = _udp_exchange(packet, host, 2.0)
    if response is None or len(response) < 12:
        return 1
    try:
        _, _, qd, an = struct.unpack("!HHHH", response[:8])
    except struct.error:
        return 1
    return 0 if qd == 1 and an >= 1 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="per-instance DNS proxy")
    parser.add_argument("config", nargs="?", default=DEFAULT_CONFIG)
    parser.add_argument("--health", action="store_true",
                        help="query the proxy's health name and exit 0/1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.health:
        return healthcheck(args.config, args.host)
    try:
        serve(args.config, args.bind)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    sys.exit(main())
