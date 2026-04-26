"""Performance contract tests for the allocator.

The README and architectural notes treat ``malloc_key`` and ``free_keys`` as
**O(1)** operations — independent of pool size. These tests measure actual
per-call latency at several pool sizes and assert the scaling factor stays
below an agreed bound. A regression that quietly turned the linked-list
operations into linear scans would otherwise only be caught in production
under a much larger pool than the unit suite normally exercises.

Run with::

    pytest -m benchmark

Notes:
  - Uses fakeredis with the lua extra; absolute timings are ~5-10× slower
    than real Redis but the *ratio* across pool sizes (which is what we
    assert) tracks the implementation's complexity, not the runtime.
  - Each measurement does a warm-up pass first to prime the Lua script
    cache and Python attribute lookups.
  - We measure paired ``malloc + free`` so per-iteration state is restored;
    this avoids drift in pool size between samples within a single size.
"""
from __future__ import annotations

import statistics
import time
from typing import Dict

import pytest
from redis import Redis

from redis_allocator.allocator import RedisAllocator


# Pool sizes spanning ~3 orders of magnitude. Keep the largest reasonable so
# the test runs in seconds even when a regression makes operations slow.
POOL_SIZES = (10, 100, 1000, 5000)
ITERATIONS = 200          # paired malloc+free measurements per size
WARMUP_ITERATIONS = 20

# Per-call scaling threshold: "operations stay constant-time as pool size
# grows from 10 to 5000". Validated against:
#
#   - Real Redis 7 (Docker):       malloc+free 0.69x, extend 0.94x  -> hard O(1)
#   - fakeredis (with conftest's   malloc+free ~1x,   extend ~1x    -> hard O(1)
#     `_install_fakeredis_hset_o1_patch`; without it, fakeredis HSET
#     is O(n) per call due to a `len(h.keys())` upstream bug, see
#     conftest.py for the patch and its rationale)
#
# 3.0 is strict enough to catch a real linear regression on a 500x size
# span (which would scale ~500x), loose enough to absorb normal CI jitter.
MAX_SCALE_FACTOR = 3.0


def _measure_paired_malloc_free(alloc: RedisAllocator, iters: int) -> float:
    """Return the median per-pair time over ``iters`` malloc+free pairs."""
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter()
        k = alloc.malloc_key(timeout=300)
        if k is not None:
            alloc.free_keys(k)
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def _build_pool(redis_client: Redis, prefix: str, size: int) -> RedisAllocator:
    alloc = RedisAllocator(redis_client, prefix, "perf", shared=False)
    alloc.clear()
    alloc.extend([f"key_{i}" for i in range(size)], timeout=3600)
    return alloc


@pytest.mark.benchmark
def test_malloc_free_is_constant_time_in_pool_size(redis_client: Redis):
    """``malloc_key`` + ``free_keys`` must scale at most ``MAX_SCALE_FACTOR``×
    when the pool grows from 10 to 5000 keys."""
    medians: Dict[int, float] = {}
    for size in POOL_SIZES:
        alloc = _build_pool(redis_client, f"perf_{size}", size)
        # Warm-up
        for _ in range(WARMUP_ITERATIONS):
            k = alloc.malloc_key(timeout=300)
            if k is not None:
                alloc.free_keys(k)
        medians[size] = _measure_paired_malloc_free(alloc, ITERATIONS)

    smallest = min(POOL_SIZES)
    largest = max(POOL_SIZES)
    ratio = medians[largest] / medians[smallest]
    table = "\n".join(f"  pool_size={s:>6}: median {medians[s] * 1e6:>8.2f} µs/op"
                      for s in POOL_SIZES)
    assert ratio <= MAX_SCALE_FACTOR, (
        f"malloc+free is not constant-time across pool sizes "
        f"(ratio {ratio:.2f}x > {MAX_SCALE_FACTOR:.1f}x):\n{table}"
    )


@pytest.mark.benchmark
def test_extend_per_key_is_constant(redis_client: Redis):
    """``extend(keys)`` must take time proportional to ``len(keys)`` only,
    not to the existing pool size. That is, per-key cost is constant.
    """
    medians: Dict[int, float] = {}
    BATCH = 50  # keys added per measurement
    for size in POOL_SIZES:
        alloc = _build_pool(redis_client, f"perfext_{size}", size)
        samples = []
        for i in range(ITERATIONS // 4):
            new_keys = [f"new_{size}_{i}_{j}" for j in range(BATCH)]
            t0 = time.perf_counter()
            alloc.extend(new_keys, timeout=3600)
            elapsed = time.perf_counter() - t0
            samples.append(elapsed / BATCH)  # per-key
            # Clean up so the pool size for the next iteration stays at `size`
            alloc.shrink(new_keys)
        medians[size] = statistics.median(samples)

    smallest = min(POOL_SIZES)
    largest = max(POOL_SIZES)
    ratio = medians[largest] / medians[smallest]
    table = "\n".join(f"  pool_size={s:>6}: median {medians[s] * 1e6:>8.2f} µs/key"
                      for s in POOL_SIZES)
    assert ratio <= MAX_SCALE_FACTOR, (
        f"extend per-key cost grows with pool size "
        f"(ratio {ratio:.2f}x > {MAX_SCALE_FACTOR:.1f}x):\n{table}"
    )
