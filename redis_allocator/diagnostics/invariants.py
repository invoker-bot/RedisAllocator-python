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
    """Parse a ``prev||next||expiry`` pool hash value."""
    rest, _, expiry_str = raw.rpartition(POOL_VALUE_SEP)
    prev, _, next_key = rest.partition(POOL_VALUE_SEP)
    try:
        expiry = int(expiry_str)
    except (TypeError, ValueError):
        expiry = -1
    return PoolEntry(prev=prev, next=next_key, expiry=expiry)


def _walk_forward(snapshot: RawAllocatorSnapshot) -> tuple[list[str], str | None]:
    if not snapshot.head:
        return [], None
    visited: list[str] = []
    seen: set[str] = set()
    current = snapshot.head
    for _ in range(len(snapshot.entries) + 1):
        if not current:
            return visited, None
        if current in seen:
            return visited, current
        entry = snapshot.entries.get(current)
        if entry is None:
            visited.append(f"<missing:{current}>")
            return visited, None
        if entry.is_allocated_marker:
            visited.append(f"<allocated:{current}>")
            return visited, None
        visited.append(current)
        seen.add(current)
        current = entry.next
    return visited, current


def _walk_backward(snapshot: RawAllocatorSnapshot) -> tuple[list[str], str | None]:
    if not snapshot.tail:
        return [], None
    visited: list[str] = []
    seen: set[str] = set()
    current = snapshot.tail
    for _ in range(len(snapshot.entries) + 1):
        if not current:
            visited.reverse()
            return visited, None
        if current in seen:
            visited.reverse()
            return visited, current
        entry = snapshot.entries.get(current)
        if entry is None:
            visited.append(f"<missing:{current}>")
            visited.reverse()
            return visited, None
        if entry.is_allocated_marker:
            visited.append(f"<allocated:{current}>")
            visited.reverse()
            return visited, None
        visited.append(current)
        seen.add(current)
        current = entry.prev
    visited.reverse()
    return visited, current


def check_invariants(snapshot: RawAllocatorSnapshot) -> list[str]:
    """Return structural invariant violations."""
    violations: list[str] = []
    forward, forward_cycle = _walk_forward(snapshot)
    backward, backward_cycle = _walk_backward(snapshot)

    if forward_cycle is not None:
        violations.append(f"forward walk cycle detected at {forward_cycle!r}")
    if backward_cycle is not None:
        violations.append(f"backward walk cycle detected at {backward_cycle!r}")
    if forward != backward:
        violations.append(f"forward/backward mismatch: forward={forward}, backward={backward}")

    free_via_walk = {key for key in forward if not key.startswith("<")}
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
