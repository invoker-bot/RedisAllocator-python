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


def test_free_list_cycle_is_reported():
    raw = RawAllocatorSnapshot(
        head="a",
        tail="b",
        entries={
            "a": parse_pool_entry("b||b||-1"),
            "b": parse_pool_entry("a||a||-1"),
        },
        lock_keys={"a": False, "b": False},
        lock_ttls={"a": -2, "b": -2},
        shared=False,
        now=100,
    )

    violations = check_invariants(raw)

    assert any("cycle" in item for item in violations)
