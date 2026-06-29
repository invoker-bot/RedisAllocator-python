"""Ad-hoc benchmark: scaling contracts on real Redis.

Run against a Docker container at 127.0.0.1:6399::

    docker run --rm -d --name allocator-stress -p 6399:6379 redis:7-alpine
    python tests/_perf_real_redis.py
    docker rm -f allocator-stress

The pytest benchmark suite uses fakeredis for repeatability in CI. This script
measures the same contracts against real Redis for local validation.
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
BATCH_SIZE = 10
BATCH_ITERATIONS = 50
ASSIGN_ITERATIONS = 8
GC_COUNT = 20
MAX_SCALE_FACTOR = 3.0
MAX_GC_SCALE_FACTOR = 5.0


def _connect() -> Redis:
    redis = Redis(host=HOST, port=PORT, decode_responses=True)
    redis.ping()
    return redis


def _build(redis: Redis, prefix: str, size: int) -> RedisAllocator:
    alloc = RedisAllocator(redis, prefix, "perf", shared=False)
    alloc.clear()
    for key in redis.scan_iter(match=f"{prefix}|perf:*"):
        redis.delete(key)
    alloc.extend([f"k{i}" for i in range(size)], timeout=3600)
    return alloc


def _print_ratio(name: str, medians: dict[int, float], unit: str, max_scale_factor: float = MAX_SCALE_FACTOR) -> float:
    for size in POOL_SIZES:
        print(f"  pool_size={size:>6}: median {medians[size] * 1e6:>8.2f} {unit}")
    ratio = medians[max(POOL_SIZES)] / medians[min(POOL_SIZES)]
    status = "PASS" if ratio <= max_scale_factor else "FAIL"
    print(f"  scaling ratio ({max(POOL_SIZES)}/{min(POOL_SIZES)}): {ratio:.2f}x [{status}]")
    print(f"  contract: {name} <= {max_scale_factor:.1f}x")
    return ratio


def bench_malloc_free(redis: Redis) -> float:
    print("\n=== malloc + single-key free ===")
    medians = {}
    for size in POOL_SIZES:
        alloc = _build(redis, f"benchmf_{size}", size)
        for _ in range(WARMUP):
            key = alloc.malloc_key(timeout=300)
            if key is not None:
                alloc.free_keys(key)
        samples = []
        for _ in range(ITERATIONS):
            t0 = time.perf_counter()
            key = alloc.malloc_key(timeout=300)
            if key is not None:
                alloc.free_keys(key)
            samples.append(time.perf_counter() - t0)
        medians[size] = statistics.median(samples)
    return _print_ratio("O(1) in pool size", medians, "us/op")


def bench_free_keys(redis: Redis) -> float:
    print("\n=== free_keys batch per-key cost ===")
    medians = {}
    for size in POOL_SIZES:
        alloc = _build(redis, f"benchfree_{size}", size)
        samples = []
        for _ in range(BATCH_ITERATIONS):
            held = [alloc.malloc_key(timeout=300) for _ in range(BATCH_SIZE)]
            t0 = time.perf_counter()
            alloc.free_keys(*held)
            samples.append((time.perf_counter() - t0) / BATCH_SIZE)
        medians[size] = statistics.median(samples)
    return _print_ratio("O(n) in freed key count only", medians, "us/key")


def bench_extend(redis: Redis) -> float:
    print("\n=== extend per-key cost ===")
    medians = {}
    for size in POOL_SIZES:
        alloc = _build(redis, f"benchext_{size}", size)
        samples = []
        for index in range(BATCH_ITERATIONS):
            new_keys = [f"new_{size}_{index}_{item}" for item in range(BATCH_SIZE)]
            t0 = time.perf_counter()
            alloc.extend(new_keys, timeout=3600)
            samples.append((time.perf_counter() - t0) / BATCH_SIZE)
            alloc.shrink(new_keys)
        medians[size] = statistics.median(samples)
    return _print_ratio("O(n) in added key count only", medians, "us/key")


def bench_shrink(redis: Redis) -> float:
    print("\n=== shrink per-key cost ===")
    medians = {}
    for size in POOL_SIZES:
        alloc = _build(redis, f"benchshrink_{size}", size)
        samples = []
        for index in range(BATCH_ITERATIONS):
            new_keys = [f"shrink_{size}_{index}_{item}" for item in range(BATCH_SIZE)]
            alloc.extend(new_keys, timeout=3600)
            t0 = time.perf_counter()
            alloc.shrink(new_keys)
            samples.append((time.perf_counter() - t0) / BATCH_SIZE)
        medians[size] = statistics.median(samples)
    return _print_ratio("O(n) in removed key count only", medians, "us/key")


def bench_assign(redis: Redis) -> float:
    print("\n=== assign per touched key cost ===")
    medians = {}
    for size in POOL_SIZES:
        alloc = _build(redis, f"benchassign_{size}", size)
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
    return _print_ratio("O(current_pool_size + assigned_key_count)", medians, "us/key")


def bench_gc(redis: Redis) -> float:
    print("\n=== gc fixed-count cost ===")
    medians = {}
    for size in POOL_SIZES:
        alloc = _build(redis, f"benchgc_{size}", size)
        samples = []
        for _ in range(BATCH_ITERATIONS):
            t0 = time.perf_counter()
            alloc.gc(count=GC_COUNT)
            samples.append(time.perf_counter() - t0)
        medians[size] = statistics.median(samples)
    return _print_ratio("O(count), not O(total_pool_size)", medians, "us/op", MAX_GC_SCALE_FACTOR)


def main() -> int:
    print(f"Connecting to {HOST}:{PORT} ...")
    try:
        redis = _connect()
    except Exception as exc:
        print(f"FATAL: cannot reach Redis: {exc}")
        return 2
    info = redis.info("server")
    print(f"Server: redis {info['redis_version']}")

    ratios = {
        "malloc+free": (bench_malloc_free(redis), MAX_SCALE_FACTOR),
        "free_keys": (bench_free_keys(redis), MAX_SCALE_FACTOR),
        "extend": (bench_extend(redis), MAX_SCALE_FACTOR),
        "shrink": (bench_shrink(redis), MAX_SCALE_FACTOR),
        "assign": (bench_assign(redis), MAX_SCALE_FACTOR),
        "gc": (bench_gc(redis), MAX_GC_SCALE_FACTOR),
    }

    print("\n=== Summary ===")
    failed = False
    for name, (ratio, max_scale_factor) in ratios.items():
        status = "PASS" if ratio <= max_scale_factor else "FAIL"
        print(f"  {name:<14}: {ratio:.2f}x [{status}]")
        failed = failed or ratio > max_scale_factor
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
