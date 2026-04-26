"""Real-Redis multi-process stress test for pool structural corruption.

Run with::

    # 1. Start a Redis container (host port 6399 to avoid clashing with locals):
    docker run --rm -d --name allocator-stress -p 6399:6379 redis:7-alpine

    # 2. Run the stress driver (requires the test extras installed):
    python tests/stress_pool_corruption.py

    # 3. When done:
    docker rm -f allocator-stress

The driver:

  - Spawns N worker processes that each randomly malloc / free / extend /
    shrink / assign / gc against a shared RedisAllocator.
  - Periodically SIGKILLs random workers (and respawns them) to simulate
    crashes mid-sequence — the user-reported scenario.
  - Runs an invariant-watcher process that snapshots the pool every second
    and reports any ``check_invariants`` violation immediately.
  - Stops at the first violation OR after ``DURATION`` seconds.

What we are hunting: pool structural corruption (head/tail pointing to
non-existent keys, broken prev/next reciprocity, ``#ALLOCATED`` markers
appearing in free-list nodes, etc.) caused by the interleaving of multiple
EVALs across processes plus mid-sequence terminations.

This script intentionally does NOT use pytest — it is a long-running stress
driver, not a unit test. Output goes to stdout for live monitoring.
"""
from __future__ import annotations

import os
import random
import signal
import sys
import time
import traceback
from multiprocessing import Process, Manager
from typing import List

# Make the in-tree package importable when running from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from redis import Redis  # noqa: E402

from redis_allocator.allocator import RedisAllocator  # noqa: E402
from tests._bughunt_helpers import check_invariants, check_invariants_with_snapshot, snapshot  # noqa: E402


# ---------------- knobs ----------------------------------------------------
REDIS_HOST = os.environ.get("STRESS_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("STRESS_REDIS_PORT", "6399"))
ALLOC_PREFIX = "stresspool"
ALLOC_SUFFIX = "stress"
NUM_WORKERS = int(os.environ.get("STRESS_WORKERS", "8"))
DURATION_SEC = int(os.environ.get("STRESS_DURATION", "60"))
KILL_INTERVAL_SEC = float(os.environ.get("STRESS_KILL_INTERVAL", "0.7"))
WATCHER_INTERVAL_SEC = float(os.environ.get("STRESS_WATCH_INTERVAL", "0.5"))
INITIAL_POOL_KEYS = [f"init_{i}" for i in range(20)]
# ---------------------------------------------------------------------------


def _connect() -> Redis:
    return Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def _make_allocator(redis: Redis) -> RedisAllocator:
    # shared=False so the bug surface includes lock-key races.
    return RedisAllocator(redis, ALLOC_PREFIX, ALLOC_SUFFIX, shared=False)


def worker(worker_id: int, stop_flag, log_queue) -> None:
    """Random allocator operations until told to stop or killed."""
    rng = random.Random(os.getpid() ^ (worker_id * 0x9E3779B1))
    redis = _connect()
    alloc = _make_allocator(redis)
    held: List[str] = []
    ops = 0

    def log(msg: str) -> None:
        try:
            log_queue.put(f"[w{worker_id} pid={os.getpid()}] {msg}")
        except Exception:
            pass

    log("started")
    try:
        while not stop_flag.value:
            action = rng.choices(
                ["malloc", "free", "extend", "shrink", "assign", "gc"],
                weights=[35, 35, 15, 5, 5, 5],
                k=1,
            )[0]
            try:
                if action == "malloc":
                    k = alloc.malloc_key(timeout=rng.choice([5, 10, 30, 60]))
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
                    new_keys = [f"a{worker_id}_{ops}_{i}" for i in range(rng.randint(2, 6))]
                    alloc.assign(new_keys, timeout=rng.choice([30, 60, -1]))
                elif action == "gc":
                    alloc.gc(count=rng.randint(1, 20))
            except Exception as e:
                # Capture but keep going — any exception is a signal worth logging.
                log(f"op={action} EXC {type(e).__name__}: {e}")
            ops += 1
            # Tight loop with small jitter to maximize EVAL interleaving.
            if rng.random() < 0.05:
                time.sleep(rng.uniform(0, 0.005))
    except Exception:
        log(f"FATAL\n{traceback.format_exc()}")
    log(f"exiting after {ops} ops")


def murderer(worker_pids, stop_flag, log_queue, kill_interval: float) -> None:
    """Periodically SIGKILL a random live worker."""
    log_queue.put("[murderer] started")
    while not stop_flag.value:
        time.sleep(kill_interval)
        live = [p for p in list(worker_pids) if p > 0]
        if not live:
            continue
        target = random.choice(live)
        try:
            os.kill(target, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
            log_queue.put(f"[murderer] killed pid {target}")
        except (ProcessLookupError, OSError) as e:
            log_queue.put(f"[murderer] pid {target} already gone ({e})")
    log_queue.put("[murderer] exiting")


def watcher(stop_flag, log_queue, violations_box, watch_interval: float) -> None:
    """Snapshot the pool periodically; report invariant violations."""
    redis = _connect()
    alloc = _make_allocator(redis)
    log_queue.put("[watcher] started")
    while not stop_flag.value:
        time.sleep(watch_interval)
        try:
            v, snap = check_invariants_with_snapshot(alloc)
            if v:
                violations_box.append((time.time(), v, dict(snap.entries),
                                       snap.head, snap.tail))
                log_queue.put(
                    f"[watcher] !!! {len(v)} INVARIANT VIOLATIONS @ {time.strftime('%H:%M:%S')}"
                )
                for line in v:
                    log_queue.put(f"[watcher]   - {line}")
                log_queue.put(f"[watcher]   head={snap.head!r} tail={snap.tail!r} entries={len(snap.entries)}")
                stop_flag.value = True  # Stop the run on first detected violation
                break
        except Exception as e:
            log_queue.put(f"[watcher] EXC {type(e).__name__}: {e}")
    log_queue.put("[watcher] exiting")


def log_drain(log_queue, stop_flag) -> None:
    """Drain log messages from workers into stdout."""
    while not stop_flag.value or not log_queue.empty():
        try:
            msg = log_queue.get(timeout=0.2)
            print(msg, flush=True)
        except Exception:
            continue


def main() -> int:
    print(f"Connecting to Redis at {REDIS_HOST}:{REDIS_PORT} ...", flush=True)
    try:
        r = _connect()
        r.ping()
    except Exception as e:
        print(f"FATAL: cannot reach Redis: {e}", file=sys.stderr)
        print(
            "Start one with:\n"
            f"  docker run --rm -d --name allocator-stress -p {REDIS_PORT}:6379 redis:7-alpine",
            file=sys.stderr,
        )
        return 2

    # Wipe and seed pool.
    alloc = _make_allocator(r)
    alloc.clear()
    # Also wipe any lock keys from previous runs.
    for k in r.scan_iter(match=f"{ALLOC_PREFIX}|{ALLOC_SUFFIX}:*"):
        r.delete(k)
    alloc.extend(INITIAL_POOL_KEYS, timeout=300)
    print(f"Pool seeded with {len(INITIAL_POOL_KEYS)} keys", flush=True)
    print(f"Workers: {NUM_WORKERS} | Duration: {DURATION_SEC}s | Kill interval: {KILL_INTERVAL_SEC}s", flush=True)

    manager = Manager()
    log_queue = manager.Queue()
    stop_flag = manager.Value("b", False)
    violations_box = manager.list()
    worker_pids = manager.list([0] * NUM_WORKERS)

    # Drain logs in a daemon thread (in main process to avoid extra serialization cost).
    import threading
    drain = threading.Thread(target=log_drain, args=(log_queue, stop_flag), daemon=True)
    drain.start()

    # Spawn workers.
    workers: List[Process] = []
    for i in range(NUM_WORKERS):
        p = Process(target=worker, args=(i, stop_flag, log_queue))
        p.start()
        workers.append(p)
        worker_pids[i] = p.pid

    # Spawn murderer + watcher.
    m = Process(target=murderer, args=(worker_pids, stop_flag, log_queue, KILL_INTERVAL_SEC))
    m.start()
    w = Process(target=watcher, args=(stop_flag, log_queue, violations_box, WATCHER_INTERVAL_SEC))
    w.start()

    # Respawn killed workers until stop time.
    deadline = time.time() + DURATION_SEC
    try:
        while time.time() < deadline and not stop_flag.value:
            time.sleep(0.5)
            for i, p in enumerate(workers):
                if not p.is_alive():
                    log_queue.put(f"[main] worker {i} (pid {p.pid}) dead, respawning")
                    p.join()
                    np = Process(target=worker, args=(i, stop_flag, log_queue))
                    np.start()
                    workers[i] = np
                    worker_pids[i] = np.pid
    finally:
        log_queue.put("[main] stopping")
        stop_flag.value = True
        for p in workers + [m, w]:
            if p.is_alive():
                try:
                    p.terminate()
                except Exception:
                    pass
        for p in workers + [m, w]:
            p.join(timeout=5)

    print("\n========== STRESS RUN COMPLETE ==========", flush=True)
    if violations_box:
        ts, vlist, entries, head, tail = violations_box[0]
        print(f"FAILED: pool corruption at {time.strftime('%H:%M:%S', time.localtime(ts))}", flush=True)
        print(f"head={head!r} tail={tail!r} entries={len(entries)}", flush=True)
        print("Violations:")
        for v in vlist:
            print(f"  - {v}")
        print("Pool snapshot (truncated to 30 entries):")
        for k, e in sorted(entries.items())[:30]:
            print(f"  {k!r}: prev={e.prev!r} next={e.next!r} expiry={e.expiry}")
        return 1

    # Final invariant check.
    final_v = check_invariants(alloc)
    if final_v:
        snap = snapshot(alloc)
        print("FAILED: post-run invariant violations:")
        for v in final_v:
            print(f"  - {v}")
        print(f"head={snap.head!r} tail={snap.tail!r} entries={len(snap.entries)}")
        return 1
    print("OK: no invariant violations observed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
