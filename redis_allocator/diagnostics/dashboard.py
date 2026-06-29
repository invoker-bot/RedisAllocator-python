"""Local web dashboard for allocator diagnostics."""

from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


STATIC_DIR = Path(__file__).with_name("static")
DASHBOARD_HTML = STATIC_DIR / "dashboard.html"


class DiagnosticsHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying diagnostics dependencies."""

    def __init__(self, server_address, request_handler, diagnostics, interval: float):
        super().__init__(server_address, request_handler)
        self.diagnostics = diagnostics
        self.interval = interval
        self.previous_snapshot = None
        self.snapshot_lock = threading.Lock()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve dashboard HTML, JSON snapshots, and SSE events."""

    server: DiagnosticsHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        """Silence default stderr request logging."""
        return None

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _snapshot_payload(self) -> dict[str, Any]:
        with self.server.snapshot_lock:
            snapshot = self.server.diagnostics.snapshot(previous=self.server.previous_snapshot)
            self.server.previous_snapshot = snapshot
            return snapshot.to_dict()

    def do_GET(self) -> None:
        """Handle dashboard GET requests."""
        if self.path == "/":
            body = DASHBOARD_HTML.read_bytes()
            self._send_bytes(HTTPStatus.OK, body, "text/html; charset=utf-8")
            return
        if self.path == "/api/snapshot":
            body = json.dumps(self._snapshot_payload(), sort_keys=True).encode("utf-8")
            self._send_bytes(HTTPStatus.OK, body, "application/json; charset=utf-8")
            return
        if self.path == "/api/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    payload = json.dumps(self._snapshot_payload(), sort_keys=True)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(self.server.interval)
            except OSError:
                return
        self._send_bytes(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")


def create_dashboard_server(diagnostics, host: str = "127.0.0.1", port: int = 8765, interval: float = 1.0) -> DiagnosticsHTTPServer:
    """Create a diagnostics dashboard server."""
    return DiagnosticsHTTPServer((host, port), DashboardRequestHandler, diagnostics, interval)


def serve_dashboard(diagnostics, host: str = "127.0.0.1", port: int = 8765, interval: float = 1.0) -> None:
    """Run the dashboard server until interrupted."""
    server = create_dashboard_server(diagnostics, host=host, port=port, interval=interval)
    actual_host, actual_port = server.server_address
    print(f"RedisAllocator diagnostics dashboard: http://{actual_host}:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
