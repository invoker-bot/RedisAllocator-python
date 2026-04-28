"""Invariant-checking helpers for the allocator bug-hunt suite.

These helpers reconstruct the allocator's pool state in pure Python (independent
of the Lua implementation under test) so that we can assert hard invariants
after every operation. Any violation indicates either a bug in the allocator
implementation or a bug in the test setup.
"""
import weakref
from dataclasses import dataclass, field
from typing import Dict, List

from redis_allocator.allocator import (
    ALLOCATED_SENTINEL,
    POOL_VALUE_SEP,
    RedisAllocator,
)


@dataclass
class PoolEntry:
    """Parsed value of a single hash entry in the pool.

    The on-disk format is ``"prev||next||expiry"``. When prev and next both
    equal ``#ALLOCATED`` the entry is in the allocated state; otherwise it is
    a node of the doubly-linked free list.
    """
    prev: str
    next: str
    expiry: int

    @property
    def is_allocated_marker(self) -> bool:
        return self.prev == ALLOCATED_SENTINEL and self.next == ALLOCATED_SENTINEL

    @property
    def is_free_node(self) -> bool:
        return not self.is_allocated_marker


@dataclass
class PoolSnapshot:
    """All allocator-related Redis state at a point in time."""
    head: str
    tail: str
    entries: Dict[str, PoolEntry] = field(default_factory=dict)
    lock_keys: Dict[str, bool] = field(default_factory=dict)
    soft_bindings: Dict[str, str] = field(default_factory=dict)

    @property
    def free_keys(self) -> List[str]:
        return [k for k, v in self.entries.items() if v.is_free_node]

    @property
    def allocated_keys(self) -> List[str]:
        return [k for k, v in self.entries.items() if v.is_allocated_marker]


def _parse_entry(raw: str) -> PoolEntry:
    """Parse an on-disk pool hash value.

    Uses ``rpartition``/``partition`` instead of the Lua side's greedy
    ``(.*)||(.*)||(.*)`` regex so the helper observes — rather than mirrors
    — that mis-parse if it occurs.
    """
    rest, _, expiry_str = raw.rpartition(POOL_VALUE_SEP)
    prev, _, nxt = rest.partition(POOL_VALUE_SEP)
    try:
        expiry = int(expiry_str)
    except ValueError:
        expiry = -1
    return PoolEntry(prev=prev, next=nxt, expiry=expiry)


_ATOMIC_SNAPSHOT_LUA = """
local head = redis.call("GET", KEYS[1]) or ""
local tail = redis.call("GET", KEYS[2]) or ""
local kvs = redis.call("HGETALL", KEYS[3])
local key_prefix = ARGV[1]
local result = {head, tail}
for i = 1, #kvs, 2 do
    local k = kvs[i]
    local v = kvs[i + 1]
    local locked = redis.call("EXISTS", key_prefix .. k)
    table.insert(result, k)
    table.insert(result, v)
    table.insert(result, locked)
end
return result
"""

# Cache one Script per Redis client. WeakKeyDictionary so entries vanish
# when the client is GC'd (long-running test sessions create many clients).
_SNAPSHOT_SCRIPT_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _snapshot_script(redis):
    cached = _SNAPSHOT_SCRIPT_CACHE.get(redis)
    if cached is None:
        cached = redis.register_script(_ATOMIC_SNAPSHOT_LUA)
        _SNAPSHOT_SCRIPT_CACHE[redis] = cached
    return cached


def snapshot(allocator: RedisAllocator) -> PoolSnapshot:
    """Capture allocator state atomically via a single Lua EVAL.

    head, tail, every hash entry and per-key lock-existence are all read
    inside one indivisible Redis command. This eliminates the
    "two-reads-with-an-EVAL-in-between" reporting hazard that previously
    produced false-positive invariant violations under concurrent load.

    Soft bindings are still scanned outside the atomic block — they're
    informational and not part of the structural invariants the watcher
    monitors.
    """
    redis = allocator.redis
    pool_key = allocator._pool_str()
    key_prefix = allocator._key_str("")
    raw = _snapshot_script(redis)(
        keys=[
            allocator._pool_pointer_str(True),
            allocator._pool_pointer_str(False),
            pool_key,
        ],
        args=[key_prefix],
    )
    head = raw[0] if raw[0] else ""
    tail = raw[1] if raw[1] else ""
    entries: dict = {}
    lock_keys: dict = {}
    i = 2
    while i < len(raw):
        k = raw[i]
        v = raw[i + 1]
        locked = bool(raw[i + 2])
        entries[k] = _parse_entry(v)
        lock_keys[k] = locked
        i += 3

    cache_prefix = allocator._soft_bind_name("")
    bindings = {}
    for binding_key in redis.scan_iter(match=f"{cache_prefix}*"):
        name = binding_key[len(cache_prefix):]
        value = redis.get(binding_key)
        if value is not None:
            bindings[name] = value

    return PoolSnapshot(
        head=head,
        tail=tail,
        entries=entries,
        lock_keys=lock_keys,
        soft_bindings=bindings,
    )


def _walk_forward(snap: PoolSnapshot) -> List[str]:
    """Walk the free list from head via ``next`` pointers.

    Bounded by ``len(entries)`` so a corrupted cycle cannot hang the test.
    Stops as soon as a pointer references an unknown key or an allocated marker.
    """
    if not snap.head:
        return []
    visited: List[str] = []
    seen = set()
    current = snap.head
    for _ in range(len(snap.entries) + 1):
        if not current or current in seen:
            break
        if current not in snap.entries:
            visited.append(f"<missing:{current}>")
            break
        entry = snap.entries[current]
        if entry.is_allocated_marker:
            visited.append(f"<allocated:{current}>")
            break
        visited.append(current)
        seen.add(current)
        if entry.next == "":
            break
        current = entry.next
    return visited


def _walk_backward(snap: PoolSnapshot) -> List[str]:
    """Walk the free list from tail via ``prev`` pointers (returned head→tail)."""
    if not snap.tail:
        return []
    visited: List[str] = []
    seen = set()
    current = snap.tail
    for _ in range(len(snap.entries) + 1):
        if not current or current in seen:
            break
        if current not in snap.entries:
            visited.append(f"<missing:{current}>")
            break
        entry = snap.entries[current]
        if entry.is_allocated_marker:
            visited.append(f"<allocated:{current}>")
            break
        visited.append(current)
        seen.add(current)
        if entry.prev == "":
            break
        current = entry.prev
    visited.reverse()
    return visited


def check_invariants_with_snapshot(
    allocator: RedisAllocator,
) -> tuple[List[str], PoolSnapshot]:
    """Same as :func:`check_invariants` but also returns the snapshot used.

    Useful when callers want to dump the *exact* state in which the
    violations were observed (avoiding the "two snapshots, race in between"
    reporting hazard).
    """
    snap = snapshot(allocator)
    return _check_against(allocator, snap), snap


def check_invariants(allocator: RedisAllocator) -> List[str]:
    """Return a list of violated-invariant descriptions (empty if all hold)."""
    snap = snapshot(allocator)
    return _check_against(allocator, snap)


def _check_against(allocator: RedisAllocator, snap: "PoolSnapshot") -> List[str]:
    violations: List[str] = []

    forward = _walk_forward(snap)
    backward = _walk_backward(snap)

    # INV-1: forward and backward traversals visit the same node sequence.
    if forward != backward:
        violations.append(
            f"INV-1 forward/backward mismatch:\n"
            f"  forward (head->next): {forward}\n"
            f"  backward (tail->prev): {backward}"
        )

    free_via_walk = set(forward)
    free_via_entries = set(snap.free_keys)

    # INV-1b: walking covers exactly the free nodes recorded in the hash.
    if free_via_walk != free_via_entries:
        violations.append(
            f"INV-1b walked free set != hash free set:\n"
            f"  walked:  {sorted(free_via_walk)}\n"
            f"  entries: {sorted(free_via_entries)}"
        )

    # INV-2: head/tail pointer well-formedness.
    if snap.entries:
        if forward:
            head_entry = snap.entries.get(snap.head)
            if head_entry and head_entry.is_free_node and head_entry.prev != "":
                violations.append(
                    f"INV-2 head node {snap.head!r} has non-empty prev={head_entry.prev!r}"
                )
            tail_entry = snap.entries.get(snap.tail)
            if tail_entry and tail_entry.is_free_node and tail_entry.next != "":
                violations.append(
                    f"INV-2 tail node {snap.tail!r} has non-empty next={tail_entry.next!r}"
                )
        else:
            # No traversable free list. Both pointers must agree by being empty.
            if snap.head != "" or snap.tail != "":
                # Free list has zero walkable nodes but pointers still claim something.
                if free_via_entries:
                    pass  # Free entries exist but are unreachable — caught by INV-1b above.
                else:
                    violations.append(
                        f"INV-2 empty free list but pointers non-empty: "
                        f"head={snap.head!r} tail={snap.tail!r}"
                    )

    # INV-2b: a single-node free list must satisfy head == tail.
    if len(forward) == 1 and snap.head != snap.tail:
        violations.append(
            f"INV-2b single-node free list but head ({snap.head!r}) != tail ({snap.tail!r})"
        )

    # INV-3: every entry is either a free node or the allocated sentinel — no
    # mixed states such as (#ALLOCATED, real_key) or (real_key, #ALLOCATED).
    for k, e in snap.entries.items():
        if e.is_allocated_marker:
            continue
        if e.prev == ALLOCATED_SENTINEL or e.next == ALLOCATED_SENTINEL:
            violations.append(
                f"INV-3 entry {k!r} has half-allocated state: prev={e.prev!r} next={e.next!r}"
            )

    # INV-4: in non-shared mode, no key may simultaneously be in the free list
    # and hold a lock. Shared mode intentionally allows it.
    if not allocator.shared:
        intersection = sorted(k for k in free_via_entries if snap.lock_keys.get(k))
        if intersection:
            violations.append(
                f"INV-4 free list and locked set overlap (non-shared mode): {intersection}"
            )

    # INV-5: every soft binding must reference a key that exists in the pool.
    for name, target in snap.soft_bindings.items():
        if target not in snap.entries:
            violations.append(
                f"INV-5 soft binding {name!r} -> {target!r} points to a key not in pool"
            )

    return violations


def assert_invariants(allocator: RedisAllocator, context: str = "") -> None:
    """Assert that all pool invariants hold; on failure print a full snapshot."""
    violations = check_invariants(allocator)
    if not violations:
        return
    snap = snapshot(allocator)
    msg_lines = [f"Pool invariants violated{f' after: {context}' if context else ''}:"]
    msg_lines.extend(f"  - {v}" for v in violations)
    msg_lines.append("Snapshot:")
    msg_lines.append(f"  head={snap.head!r} tail={snap.tail!r}")
    for k, e in sorted(snap.entries.items()):
        locked = snap.lock_keys.get(k, False)
        msg_lines.append(
            f"  entry {k!r}: prev={e.prev!r} next={e.next!r} expiry={e.expiry} locked={locked}"
        )
    for name, target in sorted(snap.soft_bindings.items()):
        msg_lines.append(f"  soft_bind {name!r} -> {target!r}")
    raise AssertionError("\n".join(msg_lines))
