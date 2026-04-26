"""Random-sequence fuzz tests for the allocator pool structure.

These run against fakeredis (single-threaded, serial EVAL) and so cannot
exhibit *true* multi-process races. But they DO exercise the same property
that races create: the next EVAL starts with whatever state the previous
EVALs (possibly from "other clients") left behind. Any structural corruption
that survives between EVALs will be caught by ``check_invariants`` on every
step.

If a fuzz seed reproduces here, the bug is in the allocator's per-EVAL logic
itself (no concurrency required) and a Docker stress test is unnecessary.
If nothing reproduces here even at high iteration counts, the bug really
does require multi-process concurrency, and the stress test under Docker
becomes the right tool.
"""
from __future__ import annotations

import random
from typing import List

import pytest
from redis import Redis

from redis_allocator.allocator import RedisAllocator
from tests._bughunt_helpers import check_invariants, snapshot


SEEDS = [0, 1, 2, 7, 13, 42, 99, 100, 101, 1234, 9999]
ITERATIONS_PER_SEED = 400


def _do_random_action(
    alloc: RedisAllocator,
    held: List[str],
    rng: random.Random,
) -> tuple[str, object]:
    """Perform one random allocator action; return (action, detail) for logs."""
    # Weighted action distribution: malloc/free dominate, structural ops are rarer.
    action = rng.choices(
        ["malloc", "free", "extend", "shrink", "assign", "gc"],
        weights=[35, 35, 15, 5, 5, 5],
        k=1,
    )[0]

    if action == "malloc":
        k = alloc.malloc_key(timeout=rng.choice([10, 30, 60, 120]))
        if k is not None:
            held.append(k)
        return ("malloc", k)

    if action == "free" and held:
        idx = rng.randrange(len(held))
        k = held.pop(idx)
        alloc.free_keys(k)
        return ("free", k)

    if action == "extend":
        new_keys = [f"x{rng.randrange(10000)}" for _ in range(rng.randint(1, 4))]
        alloc.extend(new_keys, timeout=rng.choice([30, 60, -1]))
        return ("extend", new_keys)

    if action == "shrink" and held:
        # Pick from either held or the broader pool — exercise both code paths.
        target = rng.choice(held) if rng.random() < 0.5 else f"x{rng.randrange(10000)}"
        alloc.shrink([target])
        # If we shrunk a key we held, drop it from our list.
        if target in held:
            held.remove(target)
        return ("shrink", target)

    if action == "assign":
        new_keys = [f"a{rng.randrange(10000)}" for _ in range(rng.randint(1, 5))]
        alloc.assign(new_keys, timeout=rng.choice([30, 60, -1]))
        return ("assign", new_keys)

    if action == "gc":
        alloc.gc(count=rng.randint(1, 10))
        return ("gc", None)

    return ("noop", None)


@pytest.mark.fuzz
@pytest.mark.parametrize("seed", SEEDS)
def test_fuzz_pool_invariants_under_random_ops(redis_client: Redis, seed: int):
    """Run ``ITERATIONS_PER_SEED`` random actions; assert invariants every step.

    The first invariant-violating step is reported with a full action log so
    the failure is reproducible (just rerun with the same seed).
    """
    rng = random.Random(seed)
    alloc = RedisAllocator(redis_client, f"fuzz{seed}", "t", shared=False)
    alloc.extend([f"init{i}" for i in range(rng.randint(2, 6))])

    held: List[str] = []
    log: List[tuple[int, str, object, str]] = []  # (step, action, detail, exc)

    for step in range(ITERATIONS_PER_SEED):
        try:
            action, detail = _do_random_action(alloc, held, rng)
            exc = ""
        except Exception as e:
            action, detail = ("EXC", None)
            exc = f"{type(e).__name__}: {e}"
        log.append((step, action, detail, exc))

        violations = check_invariants(alloc)
        if violations:
            snap = snapshot(alloc)
            recent = log[-20:]
            recent_lines = "\n".join(
                f"  step {s:>4} {a:<8} detail={d!r} exc={x!r}" for s, a, d, x in recent
            )
            entries_dump = "\n".join(
                f"    {k!r}: prev={e.prev!r} next={e.next!r} expiry={e.expiry}"
                for k, e in sorted(snap.entries.items())
            )
            pytest.fail(
                f"Pool corruption at seed={seed} step={step}:\n"
                f"Violations:\n  - " + "\n  - ".join(violations) + "\n"
                f"head={snap.head!r} tail={snap.tail!r}\n"
                f"Recent actions:\n{recent_lines}\n"
                f"Pool entries:\n{entries_dump}\n"
            )
