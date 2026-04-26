"""Ad-hoc benchmark: same scaling test as test_allocator_benchmark, but on
real Redis (Docker container at 127.0.0.1:6399).

Not a pytest test (filename has no ``test_`` prefix). Run directly::

    python tests/_perf_real_redis.py

Compares both malloc+free and extend across pool sizes 10/100/1000/5000.
"""
from __future__ import annotations

import os
import statistics
import sys
import time

# Allow direct script execution from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from redis import Redis  # noqa: E402

from redis_allocator.allocator import RedisAllocator  # noqa: E402


HOST = os.environ.get("STRESS_REDIS_HOST", "127.0.0.1")
PORT = int(os.environ.get("STRESS_REDIS_PORT", "6399"))
POOL_SIZES = (10, 100, 1000, 5000)
ITERATIONS = 200
WARMUP = 20
EXTEND_BATCH = 50
EXTEND_ITERATIONS = 50


def _connect() -> Redis:
    r = Redis(host=HOST, port=PORT, decode_responses=True)
    r.ping()
    return r


def _build(redis: Redis, prefix: str, size: int) -> RedisAllocator:
    alloc = RedisAllocator(redis, prefix, "perf", shared=False)
    alloc.clear()
    # Wipe any lingering lock keys from earlier runs.
    for k in redis.scan_iter(match=f"{prefix}|perf:*"):
        redis.delete(k)
    alloc.extend([f"k{i}" for i in range(size)], timeout=3600)
    return alloc


def bench_malloc_free(redis: Redis):
    print("\n=== malloc + free (paired) ===")
    medians = {}
    for size in POOL_SIZES:
        alloc = _build(redis, f"benchmf_{size}", size)
        for _ in range(WARMUP):
            k = alloc.malloc_key(timeout=300)
            if k is not None:
                alloc.free_keys(k)
        samples = []
        for _ in range(ITERATIONS):
            t0 = time.perf_counter()
            k = alloc.malloc_key(timeout=300)
            if k is not None:
                alloc.free_keys(k)
            samples.append(time.perf_counter() - t0)
        medians[size] = statistics.median(samples)
        print(f"  pool_size={size:>6}: median {medians[size] * 1e6:>8.2f} µs/op")
    ratio = medians[max(POOL_SIZES)] / medians[min(POOL_SIZES)]
    print(f"  scaling ratio ({max(POOL_SIZES)}/{min(POOL_SIZES)}): {ratio:.2f}x")
    return ratio


def bench_extend(redis: Redis):
    print("\n=== extend (per-key cost) ===")
    medians = {}
    for size in POOL_SIZES:
        alloc = _build(redis, f"benchext_{size}", size)
        samples = []
        for i in range(EXTEND_ITERATIONS):
            new_keys = [f"new_{size}_{i}_{j}" for j in range(EXTEND_BATCH)]
            t0 = time.perf_counter()
            alloc.extend(new_keys, timeout=3600)
            elapsed = time.perf_counter() - t0
            samples.append(elapsed / EXTEND_BATCH)
            alloc.shrink(new_keys)
        medians[size] = statistics.median(samples)
        print(f"  pool_size={size:>6}: median {medians[size] * 1e6:>8.2f} µs/key")
    ratio = medians[max(POOL_SIZES)] / medians[min(POOL_SIZES)]
    print(f"  scaling ratio ({max(POOL_SIZES)}/{min(POOL_SIZES)}): {ratio:.2f}x")
    return ratio


def main() -> int:
    print(f"Connecting to {HOST}:{PORT} ...")
    try:
        redis = _connect()
    except Exception as e:
        print(f"FATAL: cannot reach Redis: {e}")
        return 2
    info = redis.info("server")
    print(f"Server: redis {info['redis_version']}")

    mf_ratio = bench_malloc_free(redis)
    ex_ratio = bench_extend(redis)

    print("\n=== Summary ===")
    print(f"  malloc+free scaling: {mf_ratio:.2f}x  ({'O(1) ✓' if mf_ratio <= 3 else 'NOT O(1) ✗'})")
    print(f"  extend per-key:      {ex_ratio:.2f}x  ({'O(1) ✓' if ex_ratio <= 3 else 'NOT O(1) ✗'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
