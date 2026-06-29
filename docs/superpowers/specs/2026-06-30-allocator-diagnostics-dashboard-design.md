# Redis Allocator Diagnostics and Live Dashboard Design

Date: 2026-06-30

## Objective

Build a diagnostics subsystem for `redis-allocator` that lets operators inspect a live allocator under multi-process load without changing the allocator hot path. The tool should answer four questions:

1. Is the allocator pool structurally healthy?
2. How much capacity is free, allocated, locked, expired, or orphaned?
3. What Redis pressure and memory footprint is the allocator producing?
4. Which optimization or repair actions should be considered next?

The first implementation should expose a reusable Python API, machine-readable JSON snapshots, and a local real-time Web dashboard.

## Recommended Approach

Use a hybrid design:

- V1 is observer-only. It reads allocator and Redis state atomically where structural consistency matters, but never mutates allocator state.
- V1 also computes pressure by comparing consecutive snapshots and Redis `INFO` commandstats deltas.
- A later optional instrumentation layer can wrap allocator calls for precise per-operation latency and call counts.

This keeps the initial tool safe for production diagnostics while leaving room for deeper profiling later.

## Non-Goals

- No automatic repair in V1.
- No Redis Cluster support.
- No persistent metrics database in V1.
- No mandatory runtime dependencies for ordinary `redis_allocator` imports.
- No changes to allocator `malloc` / `free` / `extend` / `assign` hot-path behavior.

## Package Structure

Add a new package namespace:

```text
redis_allocator/
  diagnostics/
    __init__.py
    collector.py
    models.py
    invariants.py
    dashboard.py
    cli.py
    static/
      dashboard.html
```

### `models.py`

Defines dataclasses that can be converted to JSON:

- `AllocatorDiagnosticsSnapshot`
- `PoolDiagnostics`
- `IntegrityDiagnostics`
- `BindingDiagnostics`
- `OrphanDiagnostics`
- `RedisDiagnostics`
- `PressureDiagnostics`
- `Recommendation`

Each model exposes `to_dict()` and `to_json(indent: int | None = None)`.

### `collector.py`

Defines `RedisAllocatorDiagnostics`.

Constructor:

```python
RedisAllocatorDiagnostics(
    redis,
    prefix: str,
    suffix: str = "allocator",
    shared: bool = False,
    sample_limit: int = 20,
)
```

Primary methods:

```python
snapshot(previous: AllocatorDiagnosticsSnapshot | None = None) -> AllocatorDiagnosticsSnapshot
snapshot_json(previous: AllocatorDiagnosticsSnapshot | None = None, indent: int | None = 2) -> str
```

The collector should construct a lightweight `RedisAllocator` internally only to reuse key naming, not to allocate or mutate resources.

### `invariants.py`

Contains runtime-safe invariant checks extracted from the test-only helper logic:

- forward and backward free-list traversal match.
- all free hash entries are reachable from head.
- head and tail pointers are well-formed.
- allocated-marker entries use the exact `#ALLOCATED||#ALLOCATED` shape.
- free entries do not have lock keys.
- allocated entries in non-shared mode should have lock keys unless expired or reclaimable.

The runtime module must not import from `tests`.

### `dashboard.py`

Runs a local HTTP server and serves:

- `GET /` - HTML dashboard.
- `GET /api/snapshot` - current JSON snapshot.
- `GET /api/events` - Server-Sent Events stream with snapshot updates.

Use only Python standard library for V1 (`http.server`, `socketserver`, `threading`, `json`, `time`) to avoid adding heavy dependencies.

### `cli.py`

Expose a console script:

```bash
redis-allocator-diagnose \
  --redis-url redis://localhost:6379/0 \
  --prefix myapp \
  --suffix allocator \
  --interval 1 \
  --host 127.0.0.1 \
  --port 8765
```

CLI modes:

- `--json`: print one snapshot JSON and exit.
- default: start the live dashboard.
- `--sample-limit N`: cap sample arrays in JSON.

## Snapshot Collection

Structural allocator state must be read with one Lua EVAL:

- head pointer.
- tail pointer.
- all pool hash entries.
- per-pool-key lock existence.
- soft-binding key/value pairs that match the allocator cache prefix.

The Lua script returns a compact array. Python parses the array into typed models and runs invariant checks. This avoids false-positive corruption reports caused by reading head, tail, hash entries, and locks with separate Redis commands while other processes are mutating the allocator.

Advisory Redis state can be read outside that atomic script:

- `INFO memory`
- `INFO clients`
- `INFO commandstats`
- `SLOWLOG GET`
- `SCAN` for allocator lock-key orphans, bounded by `sample_limit`

## JSON Shape

Top-level JSON:

```json
{
  "timestamp": 1782840000.0,
  "identity": {
    "prefix": "myapp",
    "suffix": "allocator",
    "shared": false
  },
  "health": "healthy",
  "integrity": {
    "ok": true,
    "violations": []
  },
  "pool": {
    "total": 100,
    "free": 70,
    "allocated_markers": 30,
    "locked": 30,
    "expired_entries": 0,
    "permanent_locks": 2,
    "reclaimable": 0
  },
  "bindings": {
    "total": 10,
    "stale": 1,
    "samples": []
  },
  "orphans": {
    "count": 0,
    "samples": []
  },
  "redis": {
    "used_memory": 123456,
    "connected_clients": 4,
    "commandstats": {},
    "slowlog": []
  },
  "pressure": {
    "interval_seconds": 1.0,
    "redis_ops_per_sec": 120.0,
    "allocator_keyspace_delta": 0,
    "command_deltas": {}
  },
  "recommendations": []
}
```

Health levels:

- `healthy`: no structural violations and no severe advisory warnings.
- `degraded`: no confirmed corruption, but suspicious pressure, stale bindings, orphan locks, or excessive permanent locks exist.
- `corrupt`: structural invariant violations exist.

## Dashboard UX

First screen is the actual monitoring surface, not a landing page.

Layout:

- Header: allocator identity, Redis URL host/db, refresh interval, health badge.
- Metric row: total pool size, free, allocated, locked, orphan locks, permanent locks, Redis memory.
- Trend area: recent free/locked/orphan counts and Redis ops/sec.
- Integrity panel: invariant status, violation list, sampled broken entries.
- Pressure panel: commandstats deltas and slowlog samples.
- Recommendations panel: plain actionable messages.
- JSON drawer: current snapshot for copying into issues or CI artifacts.

The dashboard should degrade gracefully if Redis is unreachable by showing a stale/error state while continuing to retry.

## Recommendations Engine

V1 recommendations are deterministic rules:

- If health is `corrupt`, recommend stopping writers and capturing JSON before repair.
- If orphan locks are increasing, recommend checking `assign()` churn and evicted resources.
- If permanent locks exceed a threshold, recommend auditing `timeout <= 0` usage.
- If free pool is near zero, recommend increasing pool size or reducing allocation timeout.
- If `assign()`-related Redis command deltas dominate, recommend using incremental `extend()` where possible.
- If slowlog contains allocator scripts, recommend lowering `gc(count)` or inspecting pool size and stale-head cleanup.

## Testing Strategy

Unit tests:

- Parse pool entries and detect invariant violations.
- Produce JSON with stable fields and bounded sample arrays.
- Compute pressure deltas from two snapshots.
- Detect stale soft bindings and orphan locks.
- Verify `--json` CLI emits parseable JSON.

Integration tests with fakeredis:

- Healthy allocator snapshot.
- Corrupted pool states created by direct Redis writes.
- Orphan lock states.
- Dashboard HTTP endpoints return snapshot JSON.

Performance tests:

- Snapshot collection is linear in pool size.
- Dashboard polling interval does not mutate allocator keys.
- Sample limits cap expensive orphan/binding detail output.

## Rollout Plan

1. Implement `models.py`, invariant parsing, and collector snapshot JSON.
2. Add CLI `--json` mode.
3. Add local dashboard server with `/api/snapshot` and `/api/events`.
4. Add static dashboard HTML.
5. Document usage in README and Sphinx API docs.
6. Optionally add instrumentation wrapper in a later phase.

## Open Decisions Resolved

- Static HTML-only reporting is not sufficient for multi-process diagnostics, so it is not the primary interface.
- The collector is observer-only in V1 to avoid making a diagnostic tool part of the failure mode.
- The dashboard uses standard-library HTTP in V1 to keep installation lightweight.
- Exact allocator operation latency requires optional instrumentation and is deferred.
