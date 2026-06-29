"""Tests for diagnostics CLI."""

import json

import fakeredis

from redis_allocator.allocator import RedisAllocator
from redis_allocator.diagnostics.cli import main


def test_cli_json_outputs_snapshot(monkeypatch, capsys):
    redis_client = fakeredis.FakeRedis(decode_responses=True)
    allocator = RedisAllocator(redis_client, "cli", "allocator", shared=False)
    allocator.extend(["a", "b"])

    monkeypatch.setattr("redis_allocator.diagnostics.cli.Redis.from_url", lambda url, decode_responses=True: redis_client)

    exit_code = main([
        "--redis-url",
        "redis://localhost:6379/0",
        "--prefix",
        "cli",
        "--suffix",
        "allocator",
        "--json",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["identity"]["prefix"] == "cli"
    assert payload["pool"]["total"] == 2
