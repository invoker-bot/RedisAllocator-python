"""Tests for diagnostics JSON models."""

import json

from redis_allocator.diagnostics.models import (
    AllocatorDiagnosticsSnapshot,
    BindingDiagnostics,
    IdentityDiagnostics,
    IntegrityDiagnostics,
    OrphanDiagnostics,
    PoolDiagnostics,
    PressureDiagnostics,
    Recommendation,
    RedisDiagnostics,
)


def test_snapshot_to_dict_and_json_are_stable():
    snapshot = AllocatorDiagnosticsSnapshot(
        timestamp=100.5,
        identity=IdentityDiagnostics(prefix="app", suffix="allocator", shared=False),
        health="degraded",
        integrity=IntegrityDiagnostics(ok=False, violations=["free node locked"]),
        pool=PoolDiagnostics(total=3, free=1, allocated_markers=2, locked=2, expired_entries=0, permanent_locks=1, reclaimable=0),
        bindings=BindingDiagnostics(total=2, stale=1, samples=[{"name": "worker", "key": "missing"}]),
        orphans=OrphanDiagnostics(count=1, samples=["app|allocator:old"]),
        redis=RedisDiagnostics(used_memory=1234, connected_clients=2, commandstats={"cmdstat_eval": {"calls": 7}}, slowlog=[]),
        pressure=PressureDiagnostics(interval_seconds=1.0, redis_ops_per_sec=4.0, allocator_keyspace_delta=0, command_deltas={"eval": 2}),
        recommendations=[Recommendation(severity="warning", message="Audit permanent locks", code="permanent-locks")],
    )

    payload = snapshot.to_dict()

    assert payload["identity"] == {"prefix": "app", "suffix": "allocator", "shared": False}
    assert payload["integrity"]["violations"] == ["free node locked"]
    assert payload["recommendations"][0]["code"] == "permanent-locks"
    assert json.loads(snapshot.to_json()) == payload
