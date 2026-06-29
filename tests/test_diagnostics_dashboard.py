"""Tests for diagnostics dashboard server."""

import json
import threading
from urllib.request import urlopen

from redis_allocator.allocator import RedisAllocator
from redis_allocator.diagnostics import RedisAllocatorDiagnostics
from redis_allocator.diagnostics.dashboard import create_dashboard_server


def test_dashboard_serves_snapshot_json(redis_client):
    allocator = RedisAllocator(redis_client, "dash", "allocator", shared=False)
    allocator.extend(["a", "b"])
    diagnostics = RedisAllocatorDiagnostics(redis_client, "dash", "allocator", shared=False)
    server = create_dashboard_server(diagnostics, host="127.0.0.1", port=0, interval=0.1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        with urlopen(f"http://{host}:{port}/api/snapshot", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["identity"]["prefix"] == "dash"
        assert payload["pool"]["total"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_serves_html(redis_client):
    allocator = RedisAllocator(redis_client, "dashhtml", "allocator", shared=False)
    allocator.extend(["a"])
    diagnostics = RedisAllocatorDiagnostics(redis_client, "dashhtml", "allocator", shared=False)
    server = create_dashboard_server(diagnostics, host="127.0.0.1", port=0, interval=0.1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        with urlopen(f"http://{host}:{port}/", timeout=2) as response:
            html = response.read().decode("utf-8")
        assert "RedisAllocator Diagnostics" in html
        assert "/api/snapshot" in html
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
