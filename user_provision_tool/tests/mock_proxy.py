"""Simple forward HTTP/HTTPS proxy for testing provision-api proxy support.

Starts a multi-threaded proxy server that forwards HTTP requests and handles
HTTPS CONNECT tunnelling.  Useful for integration tests that need a real
proxy endpoint without depending on external infrastructure.

Usage as a context manager::

    with MockProxy(port=18888) as proxy:
        print(f"Proxy running on {proxy.url}")
        # run tests that go through the proxy
        # proxy.history logs all connections

Or standalone::

    proxy = MockProxy(port=0)   # 0 = OS-assigned port
    proxy.start()
    try:
        ...
    finally:
        proxy.stop()
"""

from __future__ import annotations

import http.server
import select
import socket
import socketserver
import ssl
import threading
import time
import urllib.request
from typing import Any


class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """TCP server that spawns a new thread per connection."""
    allow_reuse_address = True
    daemon_threads = True


class _ForwardHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    """Handles HTTP GET/POST/etc by forwarding to the destination and relaying
    the response back to the client.  Also handles CONNECT for HTTPS tunnels."""

    proxy_host: str = ""
    proxy_port: int = 0
    history: list[dict[str, Any]] = []

    # Increase timeouts to avoid hangs during slow builds
    timeout = 120

    # Answer every request locally instead of forwarding upstream. The tests that
    # use this proxy exercise its plumbing (that a request traverses it, is
    # recorded, and is relayed back) — not example.com. Forwarding made them
    # depend on outbound internet: a slow upstream exceeded the client's 10s
    # timeout and the suite went intermittently red.
    offline: bool = False

    def _forward_request(self) -> None:
        """Read the incoming request, forward it, and write the response."""
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""

        # Log the request
        self.history.append({
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers),
            "body_len": len(body),
            "timestamp": time.time(),
        })

        if self.offline:
            payload = b"mock-proxy: offline\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return

        # Build the target URL
        url = self.path
        if not url.startswith("http://") and not url.startswith("https://"):
            # Reconstruct absolute URL from Host header + path
            host = self.headers.get("Host", "localhost")
            url = f"http://{host}{self.path}"

        req = urllib.request.Request(
            url, data=body, method=self.command, headers=dict(self.headers)
        )
        # Remove hop-by-hop headers that urllib adds / shouldn't forward
        for h in ("Connection", "Proxy-Connection", "Host"):
            if h in req.headers:  # type: ignore[operator]
                del req.headers[h]  # type: ignore[arg-type]

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # type: ignore[arg-type]
                self.send_response(resp.status)  # type: ignore[arg-type]
                for k, v in resp.headers.items():  # type: ignore[union-attr]
                    if k.lower() not in ("transfer-encoding", "connection", "proxy-connection"):
                        self.send_header(k, v)
                self.end_headers()
                # Stream the body in chunks
                while True:
                    chunk = resp.read(65536)  # type: ignore[union-attr]
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception:
            self.send_error(502, "Bad Gateway")
            return

    def do_CONNECT(self) -> None:
        """Handle HTTPS CONNECT tunnelling."""
        host, port_str = self.path.split(":")
        port = int(port_str)
        self.history.append({
            "method": "CONNECT",
            "host": host,
            "port": port,
            "timestamp": time.time(),
        })
        try:
            remote = socket.create_connection((host, port), timeout=self.timeout)
            self.send_response(200, "Connection Established")
            self.end_headers()
            # Bidirectional relay
            self._relay(self.connection, remote)
        except Exception:
            self.send_error(502, "Bad Gateway")
        finally:
            if "remote" in locals():
                remote.close()

    def _relay(self, client: socket.socket, remote: socket.socket) -> None:
        """Relay data in both directions between client and remote."""
        client.setblocking(False)
        remote.setblocking(False)
        sockets = [client, remote]
        try:
            while True:
                rlist, _, xlist = select.select(sockets, [], sockets, 60)
                if xlist:
                    break
                if not rlist:
                    break
                for s in rlist:
                    data = s.recv(65536)
                    if not data:
                        return
                    dest = remote if s is client else client
                    try:
                        dest.sendall(data)
                    except OSError:
                        return
        except Exception:
            pass

    # Forward all HTTP methods to our generic handler
    do_GET = _forward_request
    do_POST = _forward_request
    do_PUT = _forward_request
    do_DELETE = _forward_request
    do_HEAD = _forward_request
    do_OPTIONS = _forward_request
    do_PATCH = _forward_request

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default logging to stderr (tests produce enough noise)."""
        pass


class MockProxy:
    """A simple forward HTTP/HTTPS proxy server for testing.

    Example::

        with MockProxy() as proxy:
            # proxy.url  → "http://127.0.0.1:PORT"
            # proxy.history → list of proxied requests
            ...
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, offline: bool = False):
        self.host = host
        self._port: int = port
        self._offline = offline
        self._server: _ThreadedTCPServer | None = None
        self._thread: threading.Thread | None = None
        self.history: list[dict[str, Any]] = []

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self._port}"

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> MockProxy:
        """Start the proxy server in a background daemon thread."""
        # Patch the handler class with our shared state
        handler = _ForwardHTTPRequestHandler
        handler.proxy_host = self.host
        handler.proxy_port = self._port
        handler.history = self.history
        handler.offline = self._offline

        self._server = _ThreadedTCPServer((self.host, self._port), handler)
        self._port = self._server.server_address[1]
        handler.proxy_port = self._port

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Shut down the proxy server."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._thread = None

    def __enter__(self) -> MockProxy:
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.stop()

    def clear_history(self) -> None:
        self.history.clear()

    def request_count(self) -> int:
        return len(self.history)
