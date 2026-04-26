# Allocator Bug-Hunt Report — 2026-04-26 (final, all rounds)

Three investigation rounds. **Every concrete bug is now fixed**, with real-Redis
multi-process stress (60s + 16 workers + 0.4s kill interval) confirming
**0 invariant violations**.

## Final test status

```
default pytest:  181 passed, 5 skipped, 15 deselected   (zombie test now passes)
-m concurrency:  2 passed
-m fuzz:         11 passed
-m benchmark:    2 passed (real O(1) on both real Redis and patched fakeredis)
docker stress:   60s/90s/16-worker/0.4s-kill: OK no violations observed
```

```bash
pytest                                       # default (~7s)
pytest -m concurrency                        # multi-thread invariant tests
pytest -m benchmark                          # O(1) contracts
python tests/_perf_real_redis.py             # cross-check perf on real Redis
python tests/stress_pool_corruption.py       # docker stress (set DURATION/WORKERS via env)
flake8 redis_allocator tests --max-line-length=150
```

---

## Bugs fixed by category

### 🔴 Pool-corrupting bugs (the user-reported "pool 完全损坏")

| # | Bug | Fix location | Symptom |
|---|---|---|---|
| 1 | `pop_from_head` locked-skip / expired branches did not update `headNext.prev` | `allocator.py` — added `_advance_head_after_removal()` helper called in both branches | Stale prev refs caused `set_item_allocated` on a neighbor to write half-allocated `#ALLOCATED || real_key || …` state, then cascading Lua asserts (`prev value should not be nil`, `next item should also be allocated`, `head value should not be nil`) crashed every later `malloc` |
| 2 | `_free_script` resurrected keys evicted from the pool by `assign` | `_free_script` HEXISTS guard before `push_to_tail` | Zombie keys re-appeared in pool, breaking the "assign completely replaces" contract |
| 3 | `_extend_script` & `_assign_script` did not clear residual lock when adding a new free node | DEL `key_str(itemName)` before `push_to_tail` in both scripts' "new key" branch | INV-4 (free + locked overlap): a fresh entry entered the free list while an orphan lock from a previous incarnation survived (combined with by-design #4) |
| 4 | `_malloc_script` soft-binding path `SET lock` *before* `set_item_allocated`, leaving INV-4 if `set_item_allocated` raised | Reordered: `set_item_allocated` first, lock second — EVAL aborts cleanly with no orphan lock | INV-4 from a partial-EVAL state |

### 🟡 Smaller bugs

| # | Bug | Fix |
|---|---|---|
| 5 | `shrink([unknown_key])` raised Lua assert (`val should not be nil`) instead of being a silent no-op | `_shrink_script` HEXISTS guard before `set_item_allocated` |
| 6 / 7 / 8 / 9 / 10 | Keys equal to `""`, `"#ALLOCATED"`, containing `||`, NUL bytes, or non-`str` types silently corrupted the on-disk encoding | New `_validate_keys` raises `ValueError` from `extend`/`assign` for any of those |

### 🟢 GC supplementary cleanup

`check_item_health`'s `if locked then set_item_allocated(...)` branch was
commented out. Re-enabled — acts as a safety net so any future race that
produces INV-4 self-heals on the next GC pass instead of persisting.

## By-design behaviors (kept intentional)

These were originally hypothesised as bugs but confirmed by the user as
intentional contracts; each is now codified by an asserting test
(`test_doc_*_by_design`) and a CLAUDE.md entry under
"Things that look like bugs but are intentional":

- `malloc_key(timeout<=0)` → permanent lock (caller takes ownership).
- `cache_timeout=0` → permanent soft binding.
- `assign()` keeps lock keys of evicted items (write amplification trade-off).
  Pairs with the new fix #3: extend/assign now clear residual locks **only
  when the same name is re-added as a fresh entry**, so the by-design
  behavior still applies for keys that are *evicted and not re-added*.

## Test infrastructure improvements

| Improvement | Why it mattered |
|---|---|
| **Atomic snapshot** in `tests/_bughunt_helpers.snapshot()` (single Lua EVAL) | The pre-existing watcher took multiple Redis commands to read state; under concurrent load, EVALs from workers slipped between those reads, producing apparent "violations" that disappeared on a fresh look. Atomic EVAL eliminated all false positives and let the real bugs (#1 above) surface as Lua asserts instead of being lost in noise. |
| **fakeredis HSET O(1) monkey-patch** (`conftest.py`) | fakeredis 2.35's `HashCommandsMixin._hset` calls `len(h.keys())` twice; `Hash.keys()` builds a fresh list every call, making each HSET O(n). The patch swaps in `len(h)` (O(1)). Without it, the benchmark could not assert the allocator's true O(1) on fakeredis. (Worth filing upstream.) |
| **Concurrency / fuzz / benchmark / stress markers** in `pyproject.toml` | Default `pytest` only runs the fast unit suite; expensive tests are explicit opt-in. |
| **Real-Redis benchmark cross-check** (`tests/_perf_real_redis.py`) | Validates that the fakeredis patch produces the same O(1) as real Redis, so the in-CI assertion is meaningful. |

## What ended up *not* being a bug

The original concurrent watcher reported INV-1, INV-1b, INV-2b violations
that looked like persistent corruption. After the atomic-snapshot rewrite,
**most disappeared** — they were artifacts of the non-atomic snapshot.
The remaining real persistent violations (INV-3 half-allocated states,
INV-4 free+locked overlap, cascading Lua asserts) were the bugs listed in
the table above. A useful reminder that any state-introspection tool used
to test concurrent code must itself be atomic with respect to mutations.

## Files touched (third round)

- `redis_allocator/allocator.py`
  - `pop_from_head`: added `_advance_head_after_removal` helper, used in both locked-skip and expired branches.
  - `_malloc_script` soft-binding: reordered `set_item_allocated` before `SET lock`.
  - `_extend_script`: DEL residual lock before `push_to_tail` of new key.
  - `_assign_script`: DEL residual lock before `push_to_tail` of new key.
  - `check_item_health`: enabled `if locked then set_item_allocated(...)` branch.
  - `_free_script`: HEXISTS guard added (round 2).
  - `_shrink_script`: HEXISTS guard added (round 2).
  - `_validate_keys`: input validation (round 1).
- `tests/_bughunt_helpers.py`: atomic snapshot via Lua EVAL.
- `tests/conftest.py`: fakeredis HSET O(1) monkey-patch.
- `tests/test_allocator_benchmark.py`: O(1) contract tests.
- `tests/_perf_real_redis.py`: real-Redis cross-check.
- `tests/test_allocator_concurrency.py`: invariant-preservation regression test.
- `tests/test_allocator_forensics.py`: forensic invariant test with method-call trace.
- `tests/test_allocator_fuzz.py`: random-sequence fuzz tests.
- `tests/stress_pool_corruption.py`: docker-backed stress driver.
- `pyproject.toml`: pytest markers + opt-in policy.
- `CLAUDE.md`: marker docs + "by-design" section.
