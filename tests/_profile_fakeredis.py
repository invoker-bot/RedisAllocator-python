"""Locate the source of fakeredis's non-linear extend cost.

Three measurements:
  1. Plain HSET against a hash of varying size — does fakeredis itself scale?
  2. Plain EVAL with a no-op Lua loop — is the lupa bridge per-call cost
     constant?
  3. The actual allocator extend, broken down per-EVAL-redis.call.
"""
from __future__ import annotations

import os
import statistics
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import fakeredis  # noqa: E402

from redis_allocator.allocator import RedisAllocator  # noqa: E402


SIZES = (10, 100, 1000, 5000)


def bench_plain_hset(sizes):
    print("\n=== plain HSET into a hash of size N ===")
    for n in sizes:
        r = fakeredis.FakeRedis(decode_responses=True)
        for i in range(n):
            r.hset("h", f"k{i}", "v")
        samples = []
        for _ in range(500):
            t0 = time.perf_counter()
            r.hset("h", "probe", "v")
            samples.append(time.perf_counter() - t0)
            r.hdel("h", "probe")
        print(f"  n={n:>5}: median {statistics.median(samples) * 1e6:>7.2f} µs")


def bench_lua_noop_loop(batch_sizes):
    print("\n=== Lua loop of N redis.call('TIME') (constant per-call cost?) ===")
    r = fakeredis.FakeRedis(decode_responses=True)
    for batch in batch_sizes:
        script = f"""
        for i = 1, {batch} do
            redis.call("TIME")
        end
        """
        s = r.register_script(script)
        samples = []
        for _ in range(50):
            t0 = time.perf_counter()
            s()
            samples.append(time.perf_counter() - t0)
        med = statistics.median(samples)
        print(f"  batch={batch:>4}: median {med * 1e6:>9.2f} µs total, {med / batch * 1e6:>7.2f} µs/call")


def bench_lua_hset_loop(pool_sizes):
    print("\n=== Lua loop of HSET into a pre-populated hash of size N ===")
    for n in pool_sizes:
        r = fakeredis.FakeRedis(decode_responses=True)
        # Pre-populate hash
        for i in range(n):
            r.hset("h", f"k{i}", "v")
        script = """
        for i = 1, 50 do
            redis.call("HSET", KEYS[1], "probe_" .. i, "v")
        end
        for i = 1, 50 do
            redis.call("HDEL", KEYS[1], "probe_" .. i)
        end
        """
        s = r.register_script(script)
        samples = []
        for _ in range(50):
            t0 = time.perf_counter()
            s(keys=["h"])
            samples.append(time.perf_counter() - t0)
        med = statistics.median(samples) / 50  # per-HSET
        print(f"  hash_size={n:>5}: median {med * 1e6:>7.2f} µs/HSET")


def bench_allocator_extend(sizes):
    print("\n=== allocator.extend() in fakeredis ===")
    for n in sizes:
        r = fakeredis.FakeRedis(decode_responses=True)
        alloc = RedisAllocator(r, f"prof_{n}", "p", shared=False)
        alloc.clear()
        alloc.extend([f"k{i}" for i in range(n)], timeout=3600)
        samples = []
        for i in range(50):
            new_keys = [f"new_{n}_{i}_{j}" for j in range(50)]
            t0 = time.perf_counter()
            alloc.extend(new_keys, timeout=3600)
            samples.append(time.perf_counter() - t0)
            alloc.shrink(new_keys)
        med = statistics.median(samples) / 50  # per key
        print(f"  pool_size={n:>5}: median {med * 1e6:>7.2f} µs/key")


if __name__ == "__main__":
    bench_plain_hset(SIZES)
    bench_lua_noop_loop([10, 50, 200, 1000])
    bench_lua_hset_loop(SIZES)
    bench_allocator_extend(SIZES)
