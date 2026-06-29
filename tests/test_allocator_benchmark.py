"""Performance contract tests for allocator operation complexity.

The README and architectural notes define complexity contracts for allocator
operations. These tests measure actual latency at several pool sizes and assert
the scaling factor stays below an agreed bound. A regression that quietly turns
linked-list operations into full-pool scans would otherwise only be caught in
production under a much larger pool than the unit suite normally exercises.

Run with::

    pytest -m benchmark

Notes:
  - Uses fakeredis with the lua extra; absolute timings are slower than real
    Redis but the ratio across pool sizes tracks implementation complexity.
  - Each measurement does a warm-up or setup pass first to prime script caches.
  - Measurements restore per-iteration state so pool size does not drift within
    a single size sample.
"""
from __future__ import annotations

import statistics
import time
from typing import Dict

import pytest
from redis import Redis

from redis_allocator.allocator import RedisAllocator


# Pool sizes span roughly three orders of magnitude. Keep the largest
# reasonable so the benchmark suite still finishes in seconds when opted in.
POOL_SIZES = (10, 100, 1000, 5000)
ITERATIONS = 200
WARMUP_ITERATIONS = 20
BATCH_SIZE = 10
BATCH_ITERATIONS = 50
ASSIGN_ITERATIONS = 8
GC_COUNT = 20

# Scaling threshold: a real linear regression across a 500x size span would be
# far above this. The value is loose enough for CI jitter and fakeredis overhead.
MAX_SCALE_FACTOR = 3.0
MAX_GC_SCALE_FACTOR = 5.0


def _format_table(medians: Dict[int, float], unit: str) -> str:
    return "\n".join(f"  pool_size={size:>6}: median {medians[size] * 1e6:>8.2f} {unit}" for size in POOL_SIZES)


def _assert_largest_to_smallest_ratio(
    medians: Dict[int, float],
    operation: str,
    unit: str,
    max_scale_factor: float = MAX_SCALE_FACTOR,
):
    smallest = min(POOL_SIZES)
    largest = max(POOL_SIZES)
    ratio = medians[largest] / medians[smallest]
    assert ratio <= max_scale_factor, (
        f"{operation} does not satisfy its scaling contract "
        f"(ratio {ratio:.2f}x > {max_scale_factor:.1f}x):\n{_format_table(medians, unit)}"
    )


def _measure_paired_malloc_free(alloc: RedisAllocator, iters: int) -> float:
    """Return the median per-pair time over ``iters`` malloc+free pairs."""
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter()
        key = alloc.malloc_key(timeout=300)
        if key is not None:
            alloc.free_keys(key)
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def _build_pool(redis_client: Redis, prefix: str, size: int) -> RedisAllocator:
    alloc = RedisAllocator(redis_client, prefix, "perf", shared=False)
    alloc.clear()
    alloc.extend([f"key_{i}" for i in range(size)], timeout=3600)
    return alloc


@pytest.mark.benchmark
def test_malloc_free_is_constant_time_in_pool_size(redis_client: Redis):
    """``malloc_key`` + single-key ``free_keys`` must be constant in pool size."""
    medians: Dict[int, float] = {}
    for size in POOL_SIZES:
        alloc = _build_pool(redis_client, f"perf_{size}", size)
        for _ in range(WARMUP_ITERATIONS):
            key = alloc.malloc_key(timeout=300)
            if key is not None:
                alloc.free_keys(key)
        medians[size] = _measure_paired_malloc_free(alloc, ITERATIONS)

    _assert_largest_to_smallest_ratio(medians, "malloc+free", "us/op")


@pytest.mark.benchmark
def test_free_keys_batch_per_key_is_constant(redis_client: Redis):
    """``free_keys(n)`` must be proportional to ``n`` only."""
    medians: Dict[int, float] = {}
    for size in POOL_SIZES:
        alloc = _build_pool(redis_client, f"perffree_{size}", size)
        samples = []
        for _ in range(BATCH_ITERATIONS):
            held = [alloc.malloc_key(timeout=300) for _ in range(BATCH_SIZE)]
            assert all(key is not None for key in held)
            t0 = time.perf_counter()
            alloc.free_keys(*held)
            samples.append((time.perf_counter() - t0) / BATCH_SIZE)
        medians[size] = statistics.median(samples)

    _assert_largest_to_smallest_ratio(medians, "free_keys per-key cost", "us/key")


@pytest.mark.benchmark
def test_extend_per_key_is_constant(redis_client: Redis):
    """``extend(keys)`` must be proportional to ``len(keys)`` only."""
    medians: Dict[int, float] = {}
    for size in POOL_SIZES:
        alloc = _build_pool(redis_client, f"perfext_{size}", size)
        samples = []
        for index in range(BATCH_ITERATIONS):
            new_keys = [f"new_{size}_{index}_{item}" for item in range(BATCH_SIZE)]
            t0 = time.perf_counter()
            alloc.extend(new_keys, timeout=3600)
            samples.append((time.perf_counter() - t0) / BATCH_SIZE)
            alloc.shrink(new_keys)
        medians[size] = statistics.median(samples)

    _assert_largest_to_smallest_ratio(medians, "extend per-key cost", "us/key")


@pytest.mark.benchmark
def test_shrink_per_key_is_constant(redis_client: Redis):
    """``shrink(keys)`` must be proportional to ``len(keys)`` only."""
    medians: Dict[int, float] = {}
    for size in POOL_SIZES:
        alloc = _build_pool(redis_client, f"perfshrink_{size}", size)
        samples = []
        for index in range(BATCH_ITERATIONS):
            new_keys = [f"shrink_{size}_{index}_{item}" for item in range(BATCH_SIZE)]
            alloc.extend(new_keys, timeout=3600)
            t0 = time.perf_counter()
            alloc.shrink(new_keys)
            samples.append((time.perf_counter() - t0) / BATCH_SIZE)
        medians[size] = statistics.median(samples)

    _assert_largest_to_smallest_ratio(medians, "shrink per-key cost", "us/key")


@pytest.mark.benchmark
def test_assign_per_touched_key_is_constant(redis_client: Redis):
    """``assign(keys)`` must be proportional to current pool size plus ``len(keys)``."""
    medians: Dict[int, float] = {}
    for size in POOL_SIZES:
        alloc = _build_pool(redis_client, f"perfassign_{size}", size)
        left = [f"left_{size}_{item}" for item in range(size)]
        right = [f"right_{size}_{item}" for item in range(size)]
        alloc.assign(left, timeout=3600)
        samples = []
        for index in range(ASSIGN_ITERATIONS):
            target = right if index % 2 == 0 else left
            t0 = time.perf_counter()
            alloc.assign(target, timeout=3600)
            samples.append((time.perf_counter() - t0) / (size + len(target)))
        medians[size] = statistics.median(samples)

    _assert_largest_to_smallest_ratio(medians, "assign per-touched-key cost", "us/key")


@pytest.mark.benchmark
def test_gc_fixed_count_is_constant_in_pool_size(redis_client: Redis):
    """``gc(count)`` must be bounded by ``count``, not total pool size."""
    medians: Dict[int, float] = {}
    for size in POOL_SIZES:
        alloc = _build_pool(redis_client, f"perfgc_{size}", size)
        samples = []
        for _ in range(BATCH_ITERATIONS):
            t0 = time.perf_counter()
            alloc.gc(count=GC_COUNT)
            samples.append(time.perf_counter() - t0)
        medians[size] = statistics.median(samples)

    _assert_largest_to_smallest_ratio(medians, "gc fixed-count cost", "us/op", MAX_GC_SCALE_FACTOR)
