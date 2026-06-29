"""Tests for live allocator diagnostics collection."""

import json

from redis_allocator.allocator import RedisAllocator
from redis_allocator.diagnostics import RedisAllocatorDiagnostics


def test_collector_reports_healthy_pool(redis_client):
    allocator = RedisAllocator(redis_client, "healthy", "allocator", shared=False)
    allocator.extend(["a", "b"])

    diagnostics = RedisAllocatorDiagnostics(redis_client, "healthy", "allocator", shared=False)
    snapshot = diagnostics.snapshot()

    assert snapshot.health == "healthy"
    assert snapshot.identity.prefix == "healthy"
    assert snapshot.pool.total == 2
    assert snapshot.pool.free == 2
    assert snapshot.integrity.ok is True
    assert json.loads(snapshot.to_json())["pool"]["total"] == 2


def test_collector_reports_corrupt_free_list(redis_client):
    allocator = RedisAllocator(redis_client, "corrupt", "allocator", shared=False)
    allocator.extend(["a"])
    redis_client.set(allocator._key_str("a"), "1", ex=60)

    diagnostics = RedisAllocatorDiagnostics(redis_client, "corrupt", "allocator", shared=False)
    snapshot = diagnostics.snapshot()

    assert snapshot.health == "corrupt"
    assert snapshot.integrity.ok is False
    assert snapshot.integrity.violations


def test_collector_reports_orphan_locks_and_stale_bindings(redis_client):
    allocator = RedisAllocator(redis_client, "advisory", "allocator", shared=False)
    allocator.extend(["a"])
    redis_client.set(allocator._key_str("old"), "1", ex=60)
    allocator.update_soft_bind("worker", "missing", timeout=60)

    diagnostics = RedisAllocatorDiagnostics(redis_client, "advisory", "allocator", shared=False, sample_limit=5)
    snapshot = diagnostics.snapshot()

    assert snapshot.health == "degraded"
    assert snapshot.orphans.count == 1
    assert snapshot.bindings.total == 1
    assert snapshot.bindings.stale == 1


def test_advisory_orphan_scan_is_bounded(redis_client):
    allocator = RedisAllocator(redis_client, "bounded", "allocator", shared=False)
    allocator.extend(["a"])
    for index in range(10):
        redis_client.set(allocator._key_str(f"orphan-{index}"), "1", ex=60)

    diagnostics = RedisAllocatorDiagnostics(redis_client, "bounded", "allocator", shared=False, sample_limit=2, scan_limit=3)
    snapshot = diagnostics.snapshot()

    assert snapshot.orphans.count == 3
    assert snapshot.orphans.scanned == 3
    assert snapshot.orphans.truncated is True
    assert len(snapshot.orphans.samples) == 2


def test_snapshot_does_not_mutate_allocator_keys(redis_client):
    allocator = RedisAllocator(redis_client, "readonly", "allocator", shared=False)
    allocator.extend(["a", "b"])
    before = sorted(redis_client.scan_iter(match="readonly|allocator*"))

    diagnostics = RedisAllocatorDiagnostics(redis_client, "readonly", "allocator", shared=False)
    diagnostics.snapshot()

    after = sorted(redis_client.scan_iter(match="readonly|allocator*"))
    assert after == before
