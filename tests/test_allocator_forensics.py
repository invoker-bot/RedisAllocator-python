"""Forensic concurrent stress: dumps the last N allocator method calls
(with thread / args / post-state) on the first invariant violation.
"""
from __future__ import annotations

import functools
import random
import threading
import time
from collections import deque
from typing import Deque, List

import fakeredis
import pytest

from redis_allocator.allocator import RedisAllocator
from tests._bughunt_helpers import check_invariants_with_snapshot


NUM_WORKERS = 4
OPS_PER_WORKER = 800
INITIAL_POOL_SIZE = 6
TRACE_TAIL = 60

_TRACE_LOCK = threading.Lock()


def _make_shared_redis():
    server = fakeredis.FakeServer()
    return server, lambda: fakeredis.FakeRedis(server=server, decode_responses=True)


def _instrument(alloc: RedisAllocator, trace) -> None:
    """Wrap allocator methods so each call is appended to ``trace``.

    Captures: thread name, monotonic timestamp, method, args, post-state
    head/tail, return value (truncated). Pre-state is implicit in the
    previous trace entry's post-state — keeps log size manageable.
    """
    methods = ("malloc_key", "free_keys", "extend", "shrink", "assign", "gc")

    for name in methods:
        original = getattr(alloc, name)

        @functools.wraps(original)
        def traced(*args, _orig=original, _name=name, **kwargs):
            try:
                ret = _orig(*args, **kwargs)
            except Exception as e:
                with _TRACE_LOCK:
                    trace.append((
                        threading.current_thread().name,
                        time.monotonic(),
                        _name,
                        args,
                        kwargs,
                        ("EXC", type(e).__name__, str(e)[:120]),
                        None,
                    ))
                raise
            try:
                head = alloc.redis.get(alloc._pool_pointer_str(True)) or ""
                tail = alloc.redis.get(alloc._pool_pointer_str(False)) or ""
            except Exception:
                head = tail = "?"
            with _TRACE_LOCK:
                trace.append((
                    threading.current_thread().name,
                    time.monotonic(),
                    _name,
                    args,
                    kwargs,
                    ret,
                    (head, tail),
                ))
            return ret

        setattr(alloc, name, traced)


def _worker_loop(
    make_client,
    worker_id: int,
    stop_event: threading.Event,
    trace,
    ops_count: int,
) -> None:
    rng = random.Random(worker_id * 0x9E3779B1)
    redis = make_client()
    alloc = RedisAllocator(redis, "forensic", "t", shared=False)
    _instrument(alloc, trace)
    held: List[str] = []
    ops = 0

    while ops < ops_count and not stop_event.is_set():
        action = rng.choices(
            ["malloc", "free", "extend", "shrink", "assign", "gc"],
            weights=[40, 35, 10, 5, 5, 5],
            k=1,
        )[0]
        try:
            if action == "malloc":
                k = alloc.malloc_key(timeout=rng.choice([5, 10]))
                if k is not None:
                    held.append(k)
            elif action == "free" and held:
                k = held.pop(rng.randrange(len(held)))
                alloc.free_keys(k)
            elif action == "extend":
                new_keys = [f"w{worker_id}_{ops}_{i}" for i in range(rng.randint(1, 3))]
                alloc.extend(new_keys, timeout=rng.choice([30, -1]))
            elif action == "shrink" and held:
                target = rng.choice(held)
                alloc.shrink([target])
                if target in held:
                    held.remove(target)
            elif action == "assign":
                new_keys = [f"a{worker_id}_{ops}_{i}" for i in range(rng.randint(2, 4))]
                alloc.assign(new_keys, timeout=rng.choice([30, -1]))
            elif action == "gc":
                alloc.gc(count=rng.randint(1, 10))
        except Exception:
            pass  # tracer already logged the exception
        ops += 1
        if (ops & 0x1F) == 0:
            time.sleep(0)


def _watcher_loop(
    make_client,
    stop_event: threading.Event,
    violation_box: list,
) -> None:
    redis = make_client()
    alloc = RedisAllocator(redis, "forensic", "t", shared=False)
    while not stop_event.is_set():
        violations, snap = check_invariants_with_snapshot(alloc)
        if violations:
            violation_box.append((time.monotonic(), violations, snap))
            stop_event.set()
            return
        time.sleep(0.003)


def _format_trace_tail(trace, n: int) -> str:
    with _TRACE_LOCK:
        # deque doesn't support slicing; materialize once then take the tail.
        tail = list(trace)[-n:]
    lines = []
    for thr, ts, name, args, kwargs, ret, post in tail:
        ret_repr = repr(ret)
        if len(ret_repr) > 80:
            ret_repr = ret_repr[:77] + "..."
        post_repr = f"post={post!r}" if post else ""
        lines.append(f"  {ts:.6f} [{thr:>10}] {name:<10} args={args!r} kwargs={kwargs!r} -> {ret_repr} {post_repr}")
    return "\n".join(lines)


@pytest.mark.concurrency
def test_forensic_no_violations_with_trace_on_failure():
    """Same contract as the concurrency invariant test, but on violation
    dumps the last ``TRACE_TAIL`` allocator method calls for forensics."""
    server, make_client = _make_shared_redis()
    seed_redis = make_client()
    alloc = RedisAllocator(seed_redis, "forensic", "t", shared=False)
    alloc.extend([f"k{i}" for i in range(INITIAL_POOL_SIZE)], timeout=120)

    trace: Deque = deque(maxlen=TRACE_TAIL + 200)
    violation_box: list = []
    stop_event = threading.Event()

    threads = []
    for i in range(NUM_WORKERS):
        t = threading.Thread(
            target=_worker_loop,
            args=(make_client, i, stop_event, trace, OPS_PER_WORKER),
            daemon=True,
            name=f"w{i}",
        )
        threads.append(t)
    watcher = threading.Thread(
        target=_watcher_loop,
        args=(make_client, stop_event, violation_box),
        daemon=True,
        name="watch",
    )

    watcher.start()
    for t in threads:
        t.start()

    deadline = time.time() + 10
    while time.time() < deadline and not stop_event.is_set():
        time.sleep(0.05)
    stop_event.set()
    for t in threads + [watcher]:
        t.join(timeout=2)

    if violation_box:
        ts, viol, snap = violation_box[0]
        detail = "\n".join(f"  - {v}" for v in viol)
        entries_dump = "\n".join(
            f"    {k!r}: prev={e.prev!r} next={e.next!r} expiry={e.expiry}"
            for k, e in sorted(snap.entries.items())
        )
        pytest.fail(
            f"\nUnexpected pool-corruption violation captured at t={ts:.6f}\n"
            f"head={snap.head!r} tail={snap.tail!r}\n"
            f"Violations:\n{detail}\n"
            f"Pool entries at violation time:\n{entries_dump}\n\n"
            f"Last {TRACE_TAIL} allocator calls (across all worker threads):\n"
            f"{_format_trace_tail(trace, TRACE_TAIL)}\n"
        )
