# Allocator Diagnostics Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an observer-only diagnostics subsystem with Python API, JSON snapshots, and a local real-time dashboard for live RedisAllocator instances.

**Architecture:** Add a focused `redis_allocator.diagnostics` package. The collector reads allocator pool structure and lock state with one atomic Lua script, then reads advisory Redis pressure, binding, and orphan-lock details with bounded Python-side scans. The dashboard is a standard-library HTTP/SSE server that consumes the same collector API.

**Tech Stack:** Python 3.10+ dataclasses, redis-py, fakeredis tests, standard-library `argparse`, `http.server`, `socketserver`, `json`, `threading`, and static HTML/CSS/JavaScript.

---

## File Structure

- Create `redis_allocator/diagnostics/__init__.py`
  - Public exports for diagnostics models, collector, and dashboard helpers.
- Create `redis_allocator/diagnostics/models.py`
  - JSON-serializable dataclasses for snapshots, metrics, recommendations, and samples.
- Create `redis_allocator/diagnostics/invariants.py`
  - Runtime-safe parser and invariant checker extracted from test helper ideas, without importing `tests`.
- Create `redis_allocator/diagnostics/collector.py`
  - `RedisAllocatorDiagnostics` collector, atomic Lua snapshot, advisory Redis metrics, pressure deltas, and recommendations.
- Create `redis_allocator/diagnostics/cli.py`
  - `redis-allocator-diagnose` CLI with `--json` and dashboard modes.
- Create `redis_allocator/diagnostics/dashboard.py`
  - Standard-library HTTP server and Server-Sent Events endpoint.
- Create `redis_allocator/diagnostics/static/dashboard.html`
  - Lightweight monitoring UI that polls/streams JSON snapshots.
- Modify `redis_allocator/__init__.py`
  - Export `RedisAllocatorDiagnostics` without changing existing public names.
- Modify `setup.py`
  - Add `console_scripts` entry point.
- Modify `README.md`
  - Add diagnostics API and dashboard usage.
- Modify `docs/source/api/index.rst`
  - Include diagnostics API page.
- Create `docs/source/api/diagnostics.rst`
  - Sphinx API docs and JSON shape.
- Create tests:
  - `tests/test_diagnostics_models.py`
  - `tests/test_diagnostics_invariants.py`
  - `tests/test_diagnostics_collector.py`
  - `tests/test_diagnostics_cli.py`
  - `tests/test_diagnostics_dashboard.py`

Implementation note: pool structure and per-pool-key lock state must be read atomically in one Lua EVAL. Soft bindings, Redis `INFO`, `SLOWLOG`, and orphan lock scans are advisory diagnostics and should be bounded by `sample_limit`; do not run an unbounded keyspace scan inside Lua on every dashboard refresh.

---

### Task 1: Diagnostics Models

**Files:**
- Create: `redis_allocator/diagnostics/__init__.py`
- Create: `redis_allocator/diagnostics/models.py`
- Test: `tests/test_diagnostics_models.py`

- [ ] **Step 1: Write failing model serialization tests**

Create `tests/test_diagnostics_models.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_diagnostics_models.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'redis_allocator.diagnostics'`.

- [ ] **Step 3: Implement models**

Create `redis_allocator/diagnostics/models.py`:

```python
"""JSON-serializable diagnostics models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


def _to_plain(value: Any) -> Any:
    """Convert dataclasses, lists, and dictionaries into JSON-safe values."""
    if is_dataclass(value):
        return {k: _to_plain(v) for k, v in asdict(value).items()}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    return value


class JsonModel:
    """Mixin for diagnostics models that can be encoded as JSON."""

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return _to_plain(self)

    def to_json(self, indent: int | None = None) -> str:
        """Return a JSON string representation."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


@dataclass
class IdentityDiagnostics(JsonModel):
    """Allocator identity for a diagnostics snapshot."""

    prefix: str
    suffix: str
    shared: bool


@dataclass
class IntegrityDiagnostics(JsonModel):
    """Structural integrity status."""

    ok: bool = True
    violations: list[str] = field(default_factory=list)


@dataclass
class PoolDiagnostics(JsonModel):
    """Pool capacity and lock counters."""

    total: int = 0
    free: int = 0
    allocated_markers: int = 0
    locked: int = 0
    expired_entries: int = 0
    permanent_locks: int = 0
    reclaimable: int = 0


@dataclass
class BindingDiagnostics(JsonModel):
    """Soft-binding counters and bounded samples."""

    total: int = 0
    stale: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class OrphanDiagnostics(JsonModel):
    """Lock keys that no longer belong to the allocator pool."""

    count: int = 0
    samples: list[str] = field(default_factory=list)


@dataclass
class RedisDiagnostics(JsonModel):
    """Redis-level advisory metrics."""

    used_memory: int = 0
    connected_clients: int = 0
    commandstats: dict[str, Any] = field(default_factory=dict)
    slowlog: list[Any] = field(default_factory=list)


@dataclass
class PressureDiagnostics(JsonModel):
    """Pressure deltas between two diagnostics snapshots."""

    interval_seconds: float = 0.0
    redis_ops_per_sec: float = 0.0
    allocator_keyspace_delta: int = 0
    command_deltas: dict[str, int] = field(default_factory=dict)


@dataclass
class Recommendation(JsonModel):
    """Actionable diagnostics recommendation."""

    severity: str
    message: str
    code: str


@dataclass
class AllocatorDiagnosticsSnapshot(JsonModel):
    """Complete diagnostics snapshot."""

    timestamp: float
    identity: IdentityDiagnostics
    health: str
    integrity: IntegrityDiagnostics
    pool: PoolDiagnostics
    bindings: BindingDiagnostics
    orphans: OrphanDiagnostics
    redis: RedisDiagnostics
    pressure: PressureDiagnostics
    recommendations: list[Recommendation] = field(default_factory=list)
```

Create `redis_allocator/diagnostics/__init__.py`:

```python
"""Diagnostics helpers for live RedisAllocator instances."""

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

__all__ = [
    "AllocatorDiagnosticsSnapshot",
    "BindingDiagnostics",
    "IdentityDiagnostics",
    "IntegrityDiagnostics",
    "OrphanDiagnostics",
    "PoolDiagnostics",
    "PressureDiagnostics",
    "Recommendation",
    "RedisDiagnostics",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_diagnostics_models.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add redis_allocator/diagnostics/__init__.py redis_allocator/diagnostics/models.py tests/test_diagnostics_models.py
git commit -m "feat: add diagnostics snapshot models"
```

---

### Task 2: Runtime Invariant Checker

**Files:**
- Create: `redis_allocator/diagnostics/invariants.py`
- Test: `tests/test_diagnostics_invariants.py`

- [ ] **Step 1: Write failing invariant tests**

Create `tests/test_diagnostics_invariants.py`:

```python
"""Tests for runtime diagnostics invariant checking."""

from redis_allocator.diagnostics.invariants import (
    RawAllocatorSnapshot,
    check_invariants,
    parse_pool_entry,
    summarize_pool,
)


def test_parse_pool_entry_handles_free_and_allocated_entries():
    free_entry = parse_pool_entry("a||b||123")
    allocated_entry = parse_pool_entry("#ALLOCATED||#ALLOCATED||-1")

    assert free_entry.prev == "a"
    assert free_entry.next == "b"
    assert free_entry.expiry == 123
    assert free_entry.is_free_node
    assert allocated_entry.is_allocated_marker


def test_healthy_snapshot_has_no_violations_and_expected_counts():
    raw = RawAllocatorSnapshot(
        head="a",
        tail="b",
        entries={
            "a": parse_pool_entry("||b||-1"),
            "b": parse_pool_entry("a||||-1"),
            "c": parse_pool_entry("#ALLOCATED||#ALLOCATED||-1"),
        },
        lock_keys={"a": False, "b": False, "c": True},
        lock_ttls={"a": -2, "b": -2, "c": 30},
        shared=False,
        now=100,
    )

    assert check_invariants(raw) == []
    pool = summarize_pool(raw)
    assert pool.total == 3
    assert pool.free == 2
    assert pool.allocated_markers == 1
    assert pool.locked == 1


def test_free_node_with_lock_is_reported():
    raw = RawAllocatorSnapshot(
        head="a",
        tail="a",
        entries={"a": parse_pool_entry("||||-1")},
        lock_keys={"a": True},
        lock_ttls={"a": 30},
        shared=False,
        now=100,
    )

    violations = check_invariants(raw)

    assert any("free entry has lock key" in item for item in violations)


def test_forward_backward_mismatch_is_reported():
    raw = RawAllocatorSnapshot(
        head="a",
        tail="b",
        entries={
            "a": parse_pool_entry("||b||-1"),
            "b": parse_pool_entry("x||||-1"),
        },
        lock_keys={"a": False, "b": False},
        lock_ttls={"a": -2, "b": -2},
        shared=False,
        now=100,
    )

    violations = check_invariants(raw)

    assert any("forward/backward mismatch" in item for item in violations)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_diagnostics_invariants.py -q
```

Expected: FAIL with `ModuleNotFoundError` or missing symbols from `redis_allocator.diagnostics.invariants`.

- [ ] **Step 3: Implement invariant checker**

Create `redis_allocator/diagnostics/invariants.py`:

```python
"""Runtime allocator invariant checks for diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field

from redis_allocator.allocator import ALLOCATED_SENTINEL, POOL_VALUE_SEP
from redis_allocator.diagnostics.models import PoolDiagnostics


@dataclass
class PoolEntry:
    """Parsed allocator pool hash entry."""

    prev: str
    next: str
    expiry: int

    @property
    def is_allocated_marker(self) -> bool:
        """Whether this entry represents an allocated pool member."""
        return self.prev == ALLOCATED_SENTINEL and self.next == ALLOCATED_SENTINEL

    @property
    def is_free_node(self) -> bool:
        """Whether this entry is part of the free-list shape."""
        return not self.is_allocated_marker


@dataclass
class RawAllocatorSnapshot:
    """Raw atomic allocator state before diagnostics summarization."""

    head: str
    tail: str
    entries: dict[str, PoolEntry] = field(default_factory=dict)
    lock_keys: dict[str, bool] = field(default_factory=dict)
    lock_ttls: dict[str, int] = field(default_factory=dict)
    shared: bool = False
    now: int = 0


def parse_pool_entry(raw: str) -> PoolEntry:
    """Parse ``prev||next||expiry`` without mirroring Lua's greedy matcher."""
    rest, _, expiry_str = raw.rpartition(POOL_VALUE_SEP)
    prev, _, next_key = rest.partition(POOL_VALUE_SEP)
    try:
        expiry = int(expiry_str)
    except (TypeError, ValueError):
        expiry = -1
    return PoolEntry(prev=prev, next=next_key, expiry=expiry)


def _walk_forward(snapshot: RawAllocatorSnapshot) -> list[str]:
    if not snapshot.head:
        return []
    visited: list[str] = []
    seen: set[str] = set()
    current = snapshot.head
    for _ in range(len(snapshot.entries) + 1):
        if not current or current in seen:
            break
        entry = snapshot.entries.get(current)
        if entry is None:
            visited.append(f"<missing:{current}>")
            break
        if entry.is_allocated_marker:
            visited.append(f"<allocated:{current}>")
            break
        visited.append(current)
        seen.add(current)
        current = entry.next
    return visited


def _walk_backward(snapshot: RawAllocatorSnapshot) -> list[str]:
    if not snapshot.tail:
        return []
    visited: list[str] = []
    seen: set[str] = set()
    current = snapshot.tail
    for _ in range(len(snapshot.entries) + 1):
        if not current or current in seen:
            break
        entry = snapshot.entries.get(current)
        if entry is None:
            visited.append(f"<missing:{current}>")
            break
        if entry.is_allocated_marker:
            visited.append(f"<allocated:{current}>")
            break
        visited.append(current)
        seen.add(current)
        current = entry.prev
    visited.reverse()
    return visited


def check_invariants(snapshot: RawAllocatorSnapshot) -> list[str]:
    """Return structural invariant violations."""
    violations: list[str] = []
    forward = _walk_forward(snapshot)
    backward = _walk_backward(snapshot)

    if forward != backward:
        violations.append(f"forward/backward mismatch: forward={forward}, backward={backward}")

    free_via_walk = set(forward)
    free_via_entries = {key for key, entry in snapshot.entries.items() if entry.is_free_node}
    if free_via_walk != free_via_entries:
        violations.append(
            f"walked free set differs from hash free set: walked={sorted(free_via_walk)}, entries={sorted(free_via_entries)}"
        )

    if not snapshot.entries:
        if snapshot.head or snapshot.tail:
            violations.append(f"empty pool has non-empty pointers: head={snapshot.head!r}, tail={snapshot.tail!r}")
        return violations

    if bool(snapshot.head) != bool(snapshot.tail):
        violations.append(f"head/tail pointer imbalance: head={snapshot.head!r}, tail={snapshot.tail!r}")

    if snapshot.head:
        head_entry = snapshot.entries.get(snapshot.head)
        if head_entry is None:
            violations.append(f"head points to missing entry: {snapshot.head!r}")
        elif head_entry.is_free_node and head_entry.prev != "":
            violations.append(f"head entry prev is not empty: head={snapshot.head!r}, prev={head_entry.prev!r}")

    if snapshot.tail:
        tail_entry = snapshot.entries.get(snapshot.tail)
        if tail_entry is None:
            violations.append(f"tail points to missing entry: {snapshot.tail!r}")
        elif tail_entry.is_free_node and tail_entry.next != "":
            violations.append(f"tail entry next is not empty: tail={snapshot.tail!r}, next={tail_entry.next!r}")

    for key, entry in snapshot.entries.items():
        locked = snapshot.lock_keys.get(key, False)
        if entry.is_free_node and locked:
            violations.append(f"free entry has lock key: {key!r}")
        if entry.prev == ALLOCATED_SENTINEL and entry.next != ALLOCATED_SENTINEL:
            violations.append(f"half-allocated entry: {key!r}")
        if entry.next == ALLOCATED_SENTINEL and entry.prev != ALLOCATED_SENTINEL:
            violations.append(f"half-allocated entry: {key!r}")

    return violations


def summarize_pool(snapshot: RawAllocatorSnapshot) -> PoolDiagnostics:
    """Return pool counters for a raw snapshot."""
    free = 0
    allocated = 0
    locked = 0
    expired = 0
    permanent = 0
    reclaimable = 0
    for key, entry in snapshot.entries.items():
        is_locked = snapshot.lock_keys.get(key, False)
        ttl = snapshot.lock_ttls.get(key, -2)
        if entry.is_free_node:
            free += 1
        else:
            allocated += 1
        if is_locked:
            locked += 1
        if entry.expiry > 0 and entry.expiry <= snapshot.now:
            expired += 1
        if is_locked and ttl == -1:
            permanent += 1
        if entry.is_allocated_marker and not is_locked:
            reclaimable += 1
    return PoolDiagnostics(
        total=len(snapshot.entries),
        free=free,
        allocated_markers=allocated,
        locked=locked,
        expired_entries=expired,
        permanent_locks=permanent,
        reclaimable=reclaimable,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_diagnostics_invariants.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add redis_allocator/diagnostics/invariants.py tests/test_diagnostics_invariants.py
git commit -m "feat: add allocator diagnostics invariants"
```

---

### Task 3: Collector Snapshot and JSON API

**Files:**
- Create: `redis_allocator/diagnostics/collector.py`
- Modify: `redis_allocator/diagnostics/__init__.py`
- Test: `tests/test_diagnostics_collector.py`

- [ ] **Step 1: Write failing collector tests**

Create `tests/test_diagnostics_collector.py`:

```python
"""Tests for RedisAllocator diagnostics collection."""

import json

from redis_allocator.allocator import RedisAllocator
from redis_allocator.diagnostics import RedisAllocatorDiagnostics


def test_collector_returns_healthy_snapshot(redis_client):
    allocator = RedisAllocator(redis_client, "diag", "allocator", shared=False)
    allocator.extend(["a", "b", "c"])
    locked = allocator.malloc_key(timeout=30)
    assert locked == "a"

    diagnostics = RedisAllocatorDiagnostics(redis_client, "diag", "allocator", shared=False)
    snapshot = diagnostics.snapshot()

    assert snapshot.health == "healthy"
    assert snapshot.integrity.ok is True
    assert snapshot.pool.total == 3
    assert snapshot.pool.free == 2
    assert snapshot.pool.allocated_markers == 1
    assert snapshot.pool.locked == 1
    assert json.loads(snapshot.to_json())["pool"]["total"] == 3


def test_collector_reports_corruption_from_direct_redis_write(redis_client):
    allocator = RedisAllocator(redis_client, "broken", "allocator", shared=False)
    allocator.extend(["a", "b"])
    redis_client.hset(allocator._pool_str(), "a", "missing||b||-1")

    diagnostics = RedisAllocatorDiagnostics(redis_client, "broken", "allocator", shared=False)
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


def test_snapshot_does_not_mutate_allocator_keys(redis_client):
    allocator = RedisAllocator(redis_client, "readonly", "allocator", shared=False)
    allocator.extend(["a", "b"])
    before = sorted(redis_client.scan_iter(match="readonly|allocator*"))

    diagnostics = RedisAllocatorDiagnostics(redis_client, "readonly", "allocator", shared=False)
    diagnostics.snapshot()

    after = sorted(redis_client.scan_iter(match="readonly|allocator*"))
    assert after == before
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_diagnostics_collector.py -q
```

Expected: FAIL with missing `RedisAllocatorDiagnostics`.

- [ ] **Step 3: Implement collector**

Create `redis_allocator/diagnostics/collector.py`:

```python
"""Live diagnostics collector for RedisAllocator."""

from __future__ import annotations

import time
from typing import Any

from redis_allocator.diagnostics.invariants import RawAllocatorSnapshot, check_invariants, parse_pool_entry, summarize_pool
from redis_allocator.diagnostics.models import (
    AllocatorDiagnosticsSnapshot,
    BindingDiagnostics,
    IdentityDiagnostics,
    IntegrityDiagnostics,
    OrphanDiagnostics,
    PressureDiagnostics,
    Recommendation,
    RedisDiagnostics,
)


_ATOMIC_POOL_SNAPSHOT_LUA = """
local head = redis.call("GET", KEYS[1]) or ""
local tail = redis.call("GET", KEYS[2]) or ""
local kvs = redis.call("HGETALL", KEYS[3])
local lock_prefix = ARGV[1]
local server_time = tonumber(redis.call("TIME")[1])
local result = {head, tail, server_time}
for i = 1, #kvs, 2 do
    local pool_key = kvs[i]
    local entry_value = kvs[i + 1]
    local lock_key = lock_prefix .. pool_key
    local locked = redis.call("EXISTS", lock_key)
    local ttl = redis.call("TTL", lock_key)
    table.insert(result, pool_key)
    table.insert(result, entry_value)
    table.insert(result, locked)
    table.insert(result, ttl)
end
return result
"""


class RedisAllocatorDiagnostics:
    """Collect diagnostics for one RedisAllocator prefix/suffix pair."""

    def __init__(self, redis, prefix: str, suffix: str = "allocator", shared: bool = False, sample_limit: int = 20):
        self.redis = redis
        self.prefix = prefix
        self.suffix = suffix
        self.shared = shared
        self.sample_limit = sample_limit
        self._snapshot_script = redis.register_script(_ATOMIC_POOL_SNAPSHOT_LUA)

    def _pool_str(self) -> str:
        return f"{self.prefix}|{self.suffix}|pool"

    def _pool_pointer_str(self, head: bool) -> str:
        return f"{self._pool_str()}|{'head' if head else 'tail'}"

    def _key_str(self, key: str) -> str:
        return f"{self.prefix}|{self.suffix}:{key}"

    def _soft_bind_prefix(self) -> str:
        return f"{self.prefix}|{self.suffix}-cache:bind:"

    def _collect_raw_pool(self) -> RawAllocatorSnapshot:
        raw = self._snapshot_script(
            keys=[
                self._pool_pointer_str(True),
                self._pool_pointer_str(False),
                self._pool_str(),
            ],
            args=[self._key_str("")],
        )
        head = raw[0] or ""
        tail = raw[1] or ""
        now = int(raw[2] or time.time())
        entries = {}
        lock_keys = {}
        lock_ttls = {}
        index = 3
        while index < len(raw):
            key = raw[index]
            value = raw[index + 1]
            locked = bool(raw[index + 2])
            ttl = int(raw[index + 3])
            entries[key] = parse_pool_entry(value)
            lock_keys[key] = locked
            lock_ttls[key] = ttl
            index += 4
        return RawAllocatorSnapshot(
            head=head,
            tail=tail,
            entries=entries,
            lock_keys=lock_keys,
            lock_ttls=lock_ttls,
            shared=self.shared,
            now=now,
        )

    def _collect_bindings(self, raw: RawAllocatorSnapshot) -> BindingDiagnostics:
        prefix = self._soft_bind_prefix()
        total = 0
        stale = 0
        samples: list[dict[str, Any]] = []
        for binding_key in self.redis.scan_iter(match=f"{prefix}*"):
            total += 1
            name = binding_key[len(prefix):]
            value = self.redis.get(binding_key)
            is_stale = value is None or value not in raw.entries or (not self.shared and raw.lock_keys.get(value, False))
            if is_stale:
                stale += 1
                if len(samples) < self.sample_limit:
                    samples.append({"name": name, "key": value})
        return BindingDiagnostics(total=total, stale=stale, samples=samples)

    def _collect_orphans(self, raw: RawAllocatorSnapshot) -> OrphanDiagnostics:
        lock_prefix = self._key_str("")
        count = 0
        samples: list[str] = []
        for lock_key in self.redis.scan_iter(match=f"{lock_prefix}*"):
            pool_key = lock_key[len(lock_prefix):]
            if pool_key not in raw.entries:
                count += 1
                if len(samples) < self.sample_limit:
                    samples.append(lock_key)
        return OrphanDiagnostics(count=count, samples=samples)

    def _safe_info(self, section: str) -> dict[str, Any]:
        try:
            return self.redis.info(section)
        except Exception:
            return {}

    def _safe_slowlog(self) -> list[Any]:
        try:
            return self.redis.slowlog_get(self.sample_limit)
        except Exception:
            return []

    def _collect_redis(self) -> RedisDiagnostics:
        memory = self._safe_info("memory")
        clients = self._safe_info("clients")
        commandstats = self._safe_info("commandstats")
        return RedisDiagnostics(
            used_memory=int(memory.get("used_memory", 0) or 0),
            connected_clients=int(clients.get("connected_clients", 0) or 0),
            commandstats=commandstats,
            slowlog=self._safe_slowlog(),
        )

    def _command_calls(self, commandstats: dict[str, Any]) -> dict[str, int]:
        calls: dict[str, int] = {}
        for name, value in commandstats.items():
            command = name.replace("cmdstat_", "")
            if isinstance(value, dict):
                calls[command] = int(value.get("calls", 0) or 0)
            elif isinstance(value, str):
                parts = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
                calls[command] = int(parts.get("calls", 0) or 0)
        return calls

    def _pressure(self, previous: AllocatorDiagnosticsSnapshot | None, current_redis: RedisDiagnostics, total_keys: int) -> PressureDiagnostics:
        if previous is None:
            return PressureDiagnostics()
        interval = max(time.time() - previous.timestamp, 0.0)
        if interval <= 0:
            return PressureDiagnostics()
        previous_calls = self._command_calls(previous.redis.commandstats)
        current_calls = self._command_calls(current_redis.commandstats)
        deltas = {
            command: current_calls.get(command, 0) - previous_calls.get(command, 0)
            for command in sorted(set(previous_calls) | set(current_calls))
        }
        positive_deltas = {command: delta for command, delta in deltas.items() if delta > 0}
        return PressureDiagnostics(
            interval_seconds=interval,
            redis_ops_per_sec=sum(positive_deltas.values()) / interval,
            allocator_keyspace_delta=total_keys - previous.pool.total,
            command_deltas=positive_deltas,
        )

    def _recommendations(
        self,
        integrity: IntegrityDiagnostics,
        pool,
        bindings: BindingDiagnostics,
        orphans: OrphanDiagnostics,
        pressure: PressureDiagnostics,
    ) -> list[Recommendation]:
        recommendations: list[Recommendation] = []
        if not integrity.ok:
            recommendations.append(Recommendation("critical", "Stop writers and capture diagnostics JSON before any repair attempt.", "integrity-corrupt"))
        if orphans.count > 0:
            recommendations.append(Recommendation("warning", "Orphan locks exist; audit assign/shrink churn and evicted resources.", "orphan-locks"))
        if pool.permanent_locks > 0:
            recommendations.append(Recommendation("info", "Permanent locks exist; audit timeout <= 0 usage.", "permanent-locks"))
        if pool.total > 0 and pool.free / pool.total < 0.1:
            recommendations.append(Recommendation("warning", "Free pool is below 10%; increase capacity or reduce allocation timeout.", "low-free-capacity"))
        if bindings.stale > 0:
            recommendations.append(Recommendation("info", "Stale soft bindings exist; consider unbinding inactive names.", "stale-bindings"))
        if pressure.command_deltas.get("eval", 0) > 100:
            recommendations.append(Recommendation("info", "High EVAL volume observed; inspect allocation rate and dashboard interval.", "high-eval-pressure"))
        return recommendations

    def snapshot(self, previous: AllocatorDiagnosticsSnapshot | None = None) -> AllocatorDiagnosticsSnapshot:
        raw = self._collect_raw_pool()
        violations = check_invariants(raw)
        integrity = IntegrityDiagnostics(ok=not violations, violations=violations)
        pool = summarize_pool(raw)
        bindings = self._collect_bindings(raw)
        orphans = self._collect_orphans(raw)
        redis_info = self._collect_redis()
        pressure = self._pressure(previous, redis_info, pool.total)
        recommendations = self._recommendations(integrity, pool, bindings, orphans, pressure)
        if not integrity.ok:
            health = "corrupt"
        elif recommendations:
            health = "degraded"
        else:
            health = "healthy"
        return AllocatorDiagnosticsSnapshot(
            timestamp=time.time(),
            identity=IdentityDiagnostics(prefix=self.prefix, suffix=self.suffix, shared=self.shared),
            health=health,
            integrity=integrity,
            pool=pool,
            bindings=bindings,
            orphans=orphans,
            redis=redis_info,
            pressure=pressure,
            recommendations=recommendations,
        )

    def snapshot_json(self, previous: AllocatorDiagnosticsSnapshot | None = None, indent: int | None = 2) -> str:
        """Return one diagnostics snapshot as JSON."""
        return self.snapshot(previous=previous).to_json(indent=indent)
```

Update `redis_allocator/diagnostics/__init__.py`:

```python
"""Diagnostics helpers for live RedisAllocator instances."""

from redis_allocator.diagnostics.collector import RedisAllocatorDiagnostics
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

__all__ = [
    "AllocatorDiagnosticsSnapshot",
    "BindingDiagnostics",
    "IdentityDiagnostics",
    "IntegrityDiagnostics",
    "OrphanDiagnostics",
    "PoolDiagnostics",
    "PressureDiagnostics",
    "Recommendation",
    "RedisAllocatorDiagnostics",
    "RedisDiagnostics",
]
```

- [ ] **Step 4: Run collector tests**

Run:

```bash
pytest tests/test_diagnostics_collector.py -q
```

Expected: PASS.

- [ ] **Step 5: Run related diagnostics tests**

Run:

```bash
pytest tests/test_diagnostics_models.py tests/test_diagnostics_invariants.py tests/test_diagnostics_collector.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add redis_allocator/diagnostics/__init__.py redis_allocator/diagnostics/collector.py tests/test_diagnostics_collector.py
git commit -m "feat: collect allocator diagnostics snapshots"
```

---

### Task 4: CLI JSON Mode

**Files:**
- Create: `redis_allocator/diagnostics/cli.py`
- Modify: `setup.py`
- Modify: `redis_allocator/__init__.py`
- Test: `tests/test_diagnostics_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/test_diagnostics_cli.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_diagnostics_cli.py -q
```

Expected: FAIL with missing `redis_allocator.diagnostics.cli`.

- [ ] **Step 3: Implement CLI**

Create `redis_allocator/diagnostics/cli.py`:

```python
"""Command-line entry point for allocator diagnostics."""

from __future__ import annotations

import argparse

from redis import Redis

from redis_allocator.diagnostics.collector import RedisAllocatorDiagnostics


def build_parser() -> argparse.ArgumentParser:
    """Build the diagnostics CLI parser."""
    parser = argparse.ArgumentParser(description="Inspect a live RedisAllocator instance.")
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--suffix", default="allocator")
    parser.add_argument("--shared", action="store_true")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="Print one JSON snapshot and exit.")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the diagnostics CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    redis_client = Redis.from_url(args.redis_url, decode_responses=True)
    diagnostics = RedisAllocatorDiagnostics(
        redis_client,
        prefix=args.prefix,
        suffix=args.suffix,
        shared=args.shared,
        sample_limit=args.sample_limit,
    )
    if args.json:
        print(diagnostics.snapshot_json(indent=2))
        return 0

    from redis_allocator.diagnostics.dashboard import serve_dashboard

    serve_dashboard(diagnostics, host=args.host, port=args.port, interval=args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Modify `setup.py` to add an entry point inside `setup(...)`:

```python
    entry_points={
        'console_scripts': [
            'redis-allocator-diagnose=redis_allocator.diagnostics.cli:main',
        ],
    },
```

Modify `redis_allocator/__init__.py` to export the collector:

```python
from redis_allocator.diagnostics import RedisAllocatorDiagnostics
```

Add `"RedisAllocatorDiagnostics"` to `__all__`.

- [ ] **Step 4: Run CLI tests**

Run:

```bash
pytest tests/test_diagnostics_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Run diagnostics test subset**

Run:

```bash
pytest tests/test_diagnostics_models.py tests/test_diagnostics_invariants.py tests/test_diagnostics_collector.py tests/test_diagnostics_cli.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add redis_allocator/diagnostics/cli.py redis_allocator/__init__.py setup.py tests/test_diagnostics_cli.py
git commit -m "feat: add allocator diagnostics CLI"
```

---

### Task 5: Dashboard HTTP Server

**Files:**
- Create: `redis_allocator/diagnostics/dashboard.py`
- Test: `tests/test_diagnostics_dashboard.py`

- [ ] **Step 1: Write failing dashboard endpoint tests**

Create `tests/test_diagnostics_dashboard.py`:

```python
"""Tests for diagnostics dashboard server."""

import json
import threading
import time
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_diagnostics_dashboard.py -q
```

Expected: FAIL with missing `redis_allocator.diagnostics.dashboard`.

- [ ] **Step 3: Implement dashboard server**

Create `redis_allocator/diagnostics/dashboard.py`:

```python
"""Local web dashboard for allocator diagnostics."""

from __future__ import annotations

import json
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


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve dashboard HTML, JSON snapshots, and SSE events."""

    server: DiagnosticsHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        """Silence default stderr request logging."""

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _snapshot_payload(self) -> dict[str, Any]:
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
            while True:
                payload = json.dumps(self._snapshot_payload(), sort_keys=True)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(self.server.interval)
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
```

- [ ] **Step 4: Create minimal static HTML**

Create `redis_allocator/diagnostics/static/dashboard.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RedisAllocator Diagnostics</title>
</head>
<body>
  <main>
    <h1>RedisAllocator Diagnostics</h1>
    <p>Live snapshot source: <code>/api/snapshot</code></p>
    <pre id="snapshot">Loading...</pre>
  </main>
  <script>
    async function refresh() {
      const response = await fetch('/api/snapshot');
      const payload = await response.json();
      document.getElementById('snapshot').textContent = JSON.stringify(payload, null, 2);
    }
    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
```

- [ ] **Step 5: Run dashboard tests**

Run:

```bash
pytest tests/test_diagnostics_dashboard.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add redis_allocator/diagnostics/dashboard.py redis_allocator/diagnostics/static/dashboard.html tests/test_diagnostics_dashboard.py
git commit -m "feat: serve allocator diagnostics dashboard"
```

---

### Task 6: Dashboard UI Polish

**Files:**
- Modify: `redis_allocator/diagnostics/static/dashboard.html`
- Test: `tests/test_diagnostics_dashboard.py`

- [ ] **Step 1: Add dashboard UI contract test**

Append to `tests/test_diagnostics_dashboard.py`:

```python
def test_dashboard_html_contains_expected_panels():
    html = Path("redis_allocator/diagnostics/static/dashboard.html").read_text(encoding="utf-8")

    assert "data-panel=\"health\"" in html
    assert "data-panel=\"pool\"" in html
    assert "data-panel=\"integrity\"" in html
    assert "data-panel=\"pressure\"" in html
    assert "data-panel=\"recommendations\"" in html
```

Add import at the top:

```python
from pathlib import Path
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_diagnostics_dashboard.py::test_dashboard_html_contains_expected_panels -q
```

Expected: FAIL because the static HTML does not contain the panel markers.

- [ ] **Step 3: Replace static HTML with usable dashboard**

Replace `redis_allocator/diagnostics/static/dashboard.html` with:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RedisAllocator Diagnostics</title>
  <style>
    :root { color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; background: #f6f7f9; color: #1f2933; }
    body { margin: 0; }
    header { padding: 18px 24px; background: #ffffff; border-bottom: 1px solid #d7dce2; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    h1 { margin: 0; font-size: 20px; }
    main { padding: 20px 24px; display: grid; gap: 16px; }
    .badge { border-radius: 4px; padding: 6px 10px; font-weight: 700; text-transform: uppercase; font-size: 12px; background: #dfe7fd; color: #173b7a; }
    .badge.healthy { background: #dff4e8; color: #126b3a; }
    .badge.degraded { background: #fff1c7; color: #7a5200; }
    .badge.corrupt { background: #ffe0df; color: #9f1d1d; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
    .card { background: #ffffff; border: 1px solid #d7dce2; border-radius: 6px; padding: 14px; }
    .label { color: #687382; font-size: 12px; text-transform: uppercase; }
    .value { font-size: 28px; font-weight: 700; margin-top: 8px; }
    .section-title { font-size: 15px; font-weight: 700; margin-bottom: 10px; }
    pre { white-space: pre-wrap; overflow: auto; max-height: 360px; background: #111827; color: #e5e7eb; border-radius: 6px; padding: 12px; }
    ul { margin: 0; padding-left: 18px; }
    canvas { width: 100%; height: 120px; border: 1px solid #d7dce2; border-radius: 4px; }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>RedisAllocator Diagnostics</h1>
      <div id="identity" class="label">Waiting for snapshot</div>
    </div>
    <div id="health" class="badge" data-panel="health">loading</div>
  </header>
  <main>
    <section class="grid" data-panel="pool">
      <div class="card"><div class="label">Total</div><div id="pool-total" class="value">-</div></div>
      <div class="card"><div class="label">Free</div><div id="pool-free" class="value">-</div></div>
      <div class="card"><div class="label">Locked</div><div id="pool-locked" class="value">-</div></div>
      <div class="card"><div class="label">Orphans</div><div id="orphan-count" class="value">-</div></div>
      <div class="card"><div class="label">Permanent Locks</div><div id="permanent-locks" class="value">-</div></div>
      <div class="card"><div class="label">Redis Memory</div><div id="redis-memory" class="value">-</div></div>
    </section>
    <section class="card" data-panel="pressure">
      <div class="section-title">Pressure</div>
      <canvas id="trend" width="900" height="120"></canvas>
      <pre id="pressure-json">{}</pre>
    </section>
    <section class="card" data-panel="integrity">
      <div class="section-title">Integrity</div>
      <ul id="violations"><li>No snapshot yet</li></ul>
    </section>
    <section class="card" data-panel="recommendations">
      <div class="section-title">Recommendations</div>
      <ul id="recommendations"><li>No snapshot yet</li></ul>
    </section>
    <section class="card">
      <div class="section-title">Current JSON</div>
      <pre id="snapshot-json">{}</pre>
    </section>
  </main>
  <script>
    const history = [];
    const maxHistory = 60;

    function setText(id, value) {
      document.getElementById(id).textContent = value;
    }

    function renderList(id, items, emptyText) {
      const node = document.getElementById(id);
      node.innerHTML = '';
      const values = items.length ? items : [emptyText];
      for (const item of values) {
        const li = document.createElement('li');
        li.textContent = typeof item === 'string' ? item : `${item.severity}: ${item.message}`;
        node.appendChild(li);
      }
    }

    function drawTrend() {
      const canvas = document.getElementById('trend');
      const context = canvas.getContext('2d');
      context.clearRect(0, 0, canvas.width, canvas.height);
      if (history.length < 2) return;
      const maxTotal = Math.max(...history.map(item => Math.max(item.free, item.locked, item.orphans)), 1);
      const series = [
        ['free', '#126b3a'],
        ['locked', '#173b7a'],
        ['orphans', '#9f1d1d']
      ];
      for (const [field, color] of series) {
        context.beginPath();
        context.strokeStyle = color;
        history.forEach((item, index) => {
          const x = (index / (maxHistory - 1)) * canvas.width;
          const y = canvas.height - (item[field] / maxTotal) * (canvas.height - 12) - 6;
          if (index === 0) context.moveTo(x, y);
          else context.lineTo(x, y);
        });
        context.stroke();
      }
    }

    function render(payload) {
      const health = document.getElementById('health');
      health.textContent = payload.health;
      health.className = `badge ${payload.health}`;
      setText('identity', `${payload.identity.prefix}|${payload.identity.suffix} shared=${payload.identity.shared}`);
      setText('pool-total', payload.pool.total);
      setText('pool-free', payload.pool.free);
      setText('pool-locked', payload.pool.locked);
      setText('orphan-count', payload.orphans.count);
      setText('permanent-locks', payload.pool.permanent_locks);
      setText('redis-memory', payload.redis.used_memory);
      renderList('violations', payload.integrity.violations, 'No integrity violations');
      renderList('recommendations', payload.recommendations, 'No recommendations');
      setText('pressure-json', JSON.stringify(payload.pressure, null, 2));
      setText('snapshot-json', JSON.stringify(payload, null, 2));
      history.push({ free: payload.pool.free, locked: payload.pool.locked, orphans: payload.orphans.count });
      while (history.length > maxHistory) history.shift();
      drawTrend();
    }

    async function refresh() {
      const response = await fetch('/api/snapshot');
      render(await response.json());
    }

    if (window.EventSource) {
      const source = new EventSource('/api/events');
      source.onmessage = event => render(JSON.parse(event.data));
      source.onerror = () => refresh();
    } else {
      refresh();
      setInterval(refresh, 1000);
    }
  </script>
</body>
</html>
```

- [ ] **Step 4: Run dashboard tests**

Run:

```bash
pytest tests/test_diagnostics_dashboard.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add redis_allocator/diagnostics/static/dashboard.html tests/test_diagnostics_dashboard.py
git commit -m "feat: add live diagnostics dashboard UI"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md`
- Create: `docs/source/api/diagnostics.rst`
- Modify: `docs/source/api/index.rst`
- Test: docs build command

- [ ] **Step 1: Add README usage section**

Add this section after the allocator behavior contracts in `README.md`:

~~~markdown
## Live Diagnostics

`redis-allocator` includes an observer-only diagnostics API and local dashboard for inspecting live allocator state under multi-process load.

Python API:

```python
from redis import Redis
from redis_allocator import RedisAllocatorDiagnostics

redis = Redis.from_url("redis://localhost:6379/0", decode_responses=True)
diagnostics = RedisAllocatorDiagnostics(redis, prefix="myapp", suffix="allocator")
snapshot = diagnostics.snapshot()
print(snapshot.to_json(indent=2))
```

One-shot JSON:

```bash
redis-allocator-diagnose --redis-url redis://localhost:6379/0 --prefix myapp --suffix allocator --json
```

Live dashboard:

```bash
redis-allocator-diagnose --redis-url redis://localhost:6379/0 --prefix myapp --suffix allocator --interval 1 --port 8765
```

The diagnostics collector reads pool structure and per-pool-key lock state atomically with one Lua script. Advisory metrics such as Redis memory, commandstats, slowlog, soft bindings, and orphan locks are sampled separately. V1 never mutates allocator state and does not repair corruption.
~~~

- [ ] **Step 2: Add Sphinx diagnostics page**

Create `docs/source/api/diagnostics.rst`:

```rst
Diagnostics Module
==================

.. module:: redis_allocator.diagnostics

The diagnostics subsystem provides observer-only snapshots and a local dashboard
for live RedisAllocator instances.

Python API
----------

.. code-block:: python

   from redis import Redis
   from redis_allocator import RedisAllocatorDiagnostics

   redis = Redis.from_url("redis://localhost:6379/0", decode_responses=True)
   diagnostics = RedisAllocatorDiagnostics(redis, prefix="myapp", suffix="allocator")
   snapshot = diagnostics.snapshot()
   print(snapshot.to_json(indent=2))

CLI
---

.. code-block:: bash

   redis-allocator-diagnose --redis-url redis://localhost:6379/0 --prefix myapp --suffix allocator --json
   redis-allocator-diagnose --redis-url redis://localhost:6379/0 --prefix myapp --suffix allocator --interval 1 --port 8765

Atomicity Boundary
------------------

The collector reads allocator pool structure and per-pool-key lock state in one
Lua EVAL. Advisory Redis metrics, soft bindings, and orphan-lock samples are
collected separately and bounded by the configured sample limit.

API Reference
-------------

.. autoclass:: redis_allocator.diagnostics.RedisAllocatorDiagnostics
   :members:
```

Modify `docs/source/api/index.rst`:

```rst
   diagnostics
```

Add it under the existing API toctree.

- [ ] **Step 3: Run docs build**

Run:

```bash
$env:GIT_CONFIG_COUNT='1'; $env:GIT_CONFIG_KEY_0='safe.directory'; $env:GIT_CONFIG_VALUE_0='D:/Projects/GitHub/RedisAllocator-python'; python -m sphinx -b html docs\source docs\build\html
```

Expected: build succeeds. Existing warnings may remain; no new diagnostics page error should appear.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/source/api/diagnostics.rst docs/source/api/index.rst
git commit -m "docs: document allocator diagnostics"
```

---

### Task 8: Full Verification and Cleanup

**Files:**
- No new files unless prior tasks reveal a narrow documentation correction.

- [ ] **Step 1: Run diagnostics test suite**

Run:

```bash
pytest tests/test_diagnostics_models.py tests/test_diagnostics_invariants.py tests/test_diagnostics_collector.py tests/test_diagnostics_cli.py tests/test_diagnostics_dashboard.py -q
```

Expected: PASS.

- [ ] **Step 2: Run default test suite**

Run:

```bash
pytest -q
```

Expected: all default tests pass. Current baseline before this plan was `188 passed, 5 skipped, 15 deselected`.

- [ ] **Step 3: Run lint**

Run:

```bash
flake8 redis_allocator tests --max-line-length=150
```

Expected: no output and exit code 0.

- [ ] **Step 4: Run docs build**

Run:

```bash
$env:GIT_CONFIG_COUNT='1'; $env:GIT_CONFIG_KEY_0='safe.directory'; $env:GIT_CONFIG_VALUE_0='D:/Projects/GitHub/RedisAllocator-python'; python -m sphinx -b html docs\source docs\build\html
```

Expected: build succeeds. Existing warnings from old RST/docstring formatting may remain.

- [ ] **Step 5: Inspect git status**

Run:

```bash
git -c safe.directory=D:/Projects/GitHub/RedisAllocator-python status --short
```

Expected: only intentional files are modified. Do not stage the pre-existing untracked `AGENTS.md` unless the user explicitly requests it.

- [ ] **Step 6: Commit final verification note if needed**

If Task 8 required no file changes, do not create an empty commit. If a small docs correction was needed, commit it:

```bash
git add README.md docs/source/api/diagnostics.rst docs/source/api/index.rst
git commit -m "docs: refine diagnostics verification notes"
```
