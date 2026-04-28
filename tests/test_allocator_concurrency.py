"""Concurrent pool-invariant tests using fakeredis + threads.

fakeredis serializes individual commands with an internal RLock, so each
EVAL is atomic (like real Redis) but multi-threaded EVALs interleave
between commands — the same model real Redis exposes to multi-process
clients. ``abandoner`` threads grab keys and never free them to simulate a
crashed worker.
"""
from __future__ import annotations

import random
import threading
import time
from typing import List

import fakeredis
import pytest

from redis_allocator.allocator import RedisAllocator
from tests._bughunt_helpers import check_invariants_with_snapshot


# Knobs kept small so the tests run in seconds, not minutes.
NUM_WORKERS = 8
NUM_ABANDONERS = 2  # threads that grab keys and never free them (crash sim)
OPS_PER_WORKER = 600
WATCHER_INTERVAL_SEC = 0.005
INITIAL_POOL_SIZE = 20


def _make_shared_redis() -> tuple[fakeredis.FakeServer, list]:
    """Return (server, client_factory). All clients share one in-memory server."""
    server = fakeredis.FakeServer()

    def make_client():
        return fakeredis.FakeRedis(server=server, decode_responses=True)

    return server, make_client


def _worker_loop(
    make_client,
    worker_id: int,
    stop_event: threading.Event,
    abandon_held: bool,
    ops_count: int,
    log: list,
) -> None:
    """One worker: random allocator ops until stop. Abandoners never free."""
    rng = random.Random(worker_id * 0x9E3779B1)
    redis = make_client()
    alloc = RedisAllocator(redis, "concurrent", "stress", shared=False)
    held: List[str] = []
    ops = 0

    try:
        while ops < ops_count and not stop_event.is_set():
            action = rng.choices(
                ["malloc", "free", "extend", "shrink", "assign", "gc"],
                weights=[35, 35 if not abandon_held else 0, 15, 5, 5, 5],
                k=1,
            )[0]
            try:
                if action == "malloc":
                    k = alloc.malloc_key(timeout=rng.choice([5, 10, 30]))
                    if k is not None:
                        held.append(k)
                elif action == "free" and held:
                    idx = rng.randrange(len(held))
                    k = held.pop(idx)
                    alloc.free_keys(k)
                elif action == "extend":
                    new_keys = [f"w{worker_id}_{ops}_{i}" for i in range(rng.randint(1, 4))]
                    alloc.extend(new_keys, timeout=rng.choice([30, 60, -1]))
                elif action == "shrink" and held:
                    target = rng.choice(held)
                    alloc.shrink([target])
                    if target in held:
                        held.remove(target)
                elif action == "assign":
                    new_keys = [f"a{worker_id}_{ops}_{i}" for i in range(rng.randint(2, 5))]
                    alloc.assign(new_keys, timeout=rng.choice([30, 60, -1]))
                elif action == "gc":
                    alloc.gc(count=rng.randint(1, 20))
            except Exception as e:
                log.append(f"w{worker_id} op={action} EXC {type(e).__name__}: {e}")
            ops += 1
            # Yield occasionally so the GIL hands off to other threads even
            # without a sleep — keeps interleaving dense.
            if (ops & 0x1F) == 0:
                time.sleep(0)
    except Exception as e:
        log.append(f"w{worker_id} FATAL {type(e).__name__}: {e}")


def _watcher_loop(
    make_client,
    stop_event: threading.Event,
    violations_box: list,
) -> None:
    """Snapshot pool until first violation; capture violation atomically.

    Uses ``check_invariants_with_snapshot`` so the snapshot we report is the
    exact one the violation was computed against (no race between detection
    and dump).
    """
    redis = make_client()
    alloc = RedisAllocator(redis, "concurrent", "stress", shared=False)
    while not stop_event.is_set():
        violations, snap = check_invariants_with_snapshot(alloc)
        if violations:
            violations_box.append((time.time(), violations, snap))
            stop_event.set()
            return
        time.sleep(WATCHER_INTERVAL_SEC)


@pytest.mark.concurrency
def test_concurrent_pool_invariants_preserved_under_threads():
    """All structural invariants must hold under concurrent malloc/free/etc."""
    server, make_client = _make_shared_redis()
    seed_redis = make_client()
    alloc = RedisAllocator(seed_redis, "concurrent", "stress", shared=False)
    alloc.extend([f"init_{i}" for i in range(INITIAL_POOL_SIZE)], timeout=300)

    stop_event = threading.Event()
    violations_box: list = []
    log: list = []

    threads = []
    for i in range(NUM_WORKERS):
        t = threading.Thread(
            target=_worker_loop,
            args=(make_client, i, stop_event, False, OPS_PER_WORKER, log),
            daemon=True,
            name=f"worker-{i}",
        )
        threads.append(t)
    for i in range(NUM_ABANDONERS):
        t = threading.Thread(
            target=_worker_loop,
            args=(make_client, 100 + i, stop_event, True, OPS_PER_WORKER, log),
            daemon=True,
            name=f"abandoner-{i}",
        )
        threads.append(t)
    watcher = threading.Thread(
        target=_watcher_loop,
        args=(make_client, stop_event, violations_box),
        daemon=True,
        name="watcher",
    )

    watcher.start()
    for t in threads:
        t.start()

    # Bound runtime so a quiet pass doesn't hang CI.
    deadline = time.time() + 15
    while time.time() < deadline and not stop_event.is_set():
        time.sleep(0.05)

    stop_event.set()
    for t in threads:
        t.join(timeout=2)
    watcher.join(timeout=2)

    # Assertion: we EXPECT NO violations. If any appear under load, that is a
    # real regression and the test should fail loudly.
    if violations_box:
        ts, viol, snap = violations_box[0]
        detail = "\n".join(f"  - {v}" for v in viol)
        pytest.fail(
            f"Pool invariants violated under concurrent load:\n"
            f"head={snap.head!r} tail={snap.tail!r} entries={len(snap.entries)}\n{detail}"
        )
