# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Snapshot

`redis-allocator` is a Python library that implements distributed resource allocation, locking, and task queuing on top of a **single Redis instance**. Lua scripting is the cornerstone of every atomicity guarantee in this codebase, which is why Redis Cluster is explicitly out of scope (see README "Roadmap" → "Out of Scope").

- Target Python: **3.10+** (CI runs py310 and py311 via tox).
- Public surface is re-exported from `redis_allocator/__init__.py`; everything else is implementation.
- Tests use `fakeredis[lua]` — the `[lua]` extra is mandatory because most code paths execute server-side Lua (`register_script`).

## Common Commands

```bash
# Dev install (test + docs extras)
pip install -e ".[dev]"

# Run the default fast unit suite (excludes `concurrency` and `stress` markers)
pytest

# Single test or test file
pytest tests/test_allocator.py
pytest tests/test_allocator.py::TestRedisAllocator::test_malloc_basic

# Coverage
pytest --cov=redis_allocator

# Run the multi-thread race reproducers (fakeredis, ~1s, no external deps)
pytest -m concurrency

# Run the random-sequence fuzz tests (single-thread; may hit lupa quirks on Windows)
pytest -m fuzz

# Run performance contract tests (constant-time complexity for malloc/free/extend)
pytest -m benchmark

# Run the real-Redis benchmark side-by-side (Docker container at localhost:6399):
python tests/_perf_real_redis.py

# Run the Docker-backed stress driver (NOT a pytest test — it's a script):
#   docker run --rm -d --name allocator-stress -p 6399:6379 redis:7-alpine
#   python tests/stress_pool_corruption.py
#   docker rm -f allocator-stress

# Lint (project-wide rule: max-line-length=150)
flake8 redis_allocator tests --max-line-length=150

# Full matrix (py310, py311, flake8) — same as CI
tox

# Build Sphinx docs
cd docs && make html

# Bump version (commitizen-managed; updates redis_allocator/_version.py and tags)
cz bump
```

**Marker policy** (configured in `pyproject.toml` `[tool.pytest.ini_options]`):
- `concurrency` — multi-thread fakeredis tests verifying invariants hold under load; fast (~25s) but excluded by default to keep `pytest` fast and side-effect free.
- `fuzz` — randomised single-thread sequences; can trigger lupa (Lua-in-Python) access violations on Windows that crash the whole pytest run.
- `benchmark` — performance contract tests asserting malloc/free/extend stay constant-time as pool grows.
- `stress` — long-running tests requiring an external Redis (Docker). The driver under `tests/stress_pool_corruption.py` is a standalone script, not a pytest test, so it isn't auto-collected; the marker is reserved for any future pytest-driven stress test.
- Default `addopts = "-m 'not concurrency and not fuzz and not stress and not benchmark'"` keeps all four off unless explicitly opted in.

**Snapshot atomicity (important for any new test)** — `tests/_bughunt_helpers.snapshot()` reads head, tail, all hash entries, and per-key lock state in a **single Lua EVAL**. This is required: an earlier non-atomic version produced false-positive invariant violations under concurrent load (head/entries fetched separately, EVAL slipping in between). When adding new state-introspection helpers for tests, follow the same atomic-EVAL pattern.

## Architecture

### Three modules, one shared abstraction

| Module | Public types | Role |
|---|---|---|
| `lock.py` | `BaseLock`/`RedisLock`, `BaseLockPool`/`RedisLockPool`, `ThreadLock`/`ThreadLockPool`, `LockStatus` | Foundational distributed lock + lock pool. In-process `Thread*` variants share the same ABCs for testability. |
| `allocator.py` | `RedisAllocator`, `RedisAllocatableClass`, `RedisAllocatorObject`, `RedisAllocatorUpdater`, `RedisAllocatorPolicy`/`DefaultRedisAllocatorPolicy`, `RedisThreadHealthCheckPool` | Resource pool with shared/exclusive modes, soft binding, GC, pluggable policy. |
| `task_queue.py` | `RedisTaskQueue`, `RedisTask`, `TaskExecutePolicy` | Distributed task queue with `BLPOP` polling; task state is serialized (see module for the serializer in use). |

**Critical relationship:** `RedisAllocator` *inherits from* `RedisLockPool` (not composes). Allocation = acquire lock from the pool; free = release lock and rejoin free list. `RedisThreadHealthCheckPool` also inherits `RedisLockPool`. "Lock pool" is the load-bearing primitive — most architectural questions resolve to "what does the lock pool do here?".

### Pool data layout (free list in Redis)

The allocator's free pool is a **doubly-linked list materialized inside a single Redis hash**:

- `<prefix>|<suffix>|pool|head` — string key holding the head pointer
- `<prefix>|<suffix>|pool|tail` — string key holding the tail pointer
- `<prefix>|<suffix>|pool` — hash mapping each pool key to a value of the form `prev||next||expiry`
- `<prefix>|<suffix>:<key>` — per-resource lock key (only present in non-shared mode while allocated)
- `<prefix>|<suffix>-cache:bind:<name>` — soft-binding cache (independent timeout)

When changing pool semantics, all four key shapes move together.

### Lua scripts share a common base

`RedisAllocator._lua_required_string` (`allocator.py:575`) is a `cached_property` returning a Lua **helper function library** (linked-list ops, key naming). Every other Lua script in `allocator.py` (7 of them at the time of writing) is constructed as:

```python
self.redis.register_script(f'''
    {self._lua_required_string}
    -- script-specific logic
''')
```

**If you change the linked-list invariants or key-naming scheme, edit `_lua_required_string` first** — otherwise the per-operation scripts will silently disagree. This is the central architectural contract of the allocator.

### Shared vs exclusive allocation

`RedisAllocator(..., shared=False)` is the default and locks each allocated key. `shared=True` rotates the key from head → tail of the free list without locking, allowing concurrent allocation. Almost all behavioral tests are forced to validate **both modes** (see "Testing patterns" below).

### Policy & updater hooks

`RedisAllocatorPolicy` (and its `DefaultRedisAllocatorPolicy`) controls when GC fires, the expiry duration, and how the pool's key set is refreshed from an external source via a `RedisAllocatorUpdater` (the updater's `fetch(param)` returns a fresh batch of keys). This is the extension point for "where do my pool keys come from" and "how aggressively do I reclaim".

## Testing patterns

- `tests/conftest.py` declares the canonical fixtures. **`redis_allocator` and `allocator_with_policy` are parametrized with `params=[False, True]`** — every test that consumes them runs **twice**, once per allocation mode. Add tests to those fixtures by default; only opt out (custom fixture) when behavior genuinely diverges between modes.
- `test_object` is similarly parametrized over `[None, "test_object"]` to cover both "no soft-binding" and "with soft-binding" code paths.
- Use `fakeredis.FakeRedis(decode_responses=True)` (already wrapped as `redis_client` fixture). Real Redis is not required — and not used in CI.
- Always add tests when adding code (per `.cursor/rules/feat.mdc` and `CONTRIBUTING.md`).

## Project conventions

From `.cursor/rules/feat.mdc` and `.windsurfrules` (both authoritative):

- **Code is English** even when the conversation is in Chinese.
- **Google-style docstrings.**
- **Make the minimal necessary change.** Do not refactor or add features when fixing a bug unless explicitly asked.
- **Conventional Commits** (`feat:`, `fix:`, `docs:`, …) — enforced by commitizen, which also drives `cz bump`.
- Line length 150 (consistently set across `.flake8`, `.pylintrc`, `tox.ini`).

## Release & versioning

- Single source of truth for the published version is **`redis_allocator/_version.py`**, written by `setup.py` and bumped by commitizen (`pyproject.toml` → `[tool.commitizen.version_files]`).
- ⚠️ `redis_allocator/__init__.py` contains a hardcoded `__version__ = '0.0.1'` that is **not kept in sync** with `_version.py`. Treat `_version.py` as canonical; do not trust the `__init__.py` constant.
- Pushing a tag matching `*` to `main` triggers `.github/workflows/publish.yml`, which runs tox, builds the wheel, and publishes to PyPI plus a private mirror.

## Things that look like bugs but are intentional

- The lock pool exposes both `lock()` (try-acquire, returns bool) and `rlock()` (reentrant — succeeds if the same value already holds the lock). They are not duplicates.
- `TaskExecutePolicy.Auto` decides local vs remote based on whether a listener is currently registered; tests sometimes mock this — read `task_queue.py` before changing the heuristic.
- Soft-binding entries persist past `free()` by design (own timeout, default 3600s). A bound resource being unavailable falls back to normal allocation rather than blocking.
- **`malloc_key(timeout=0)` and `timeout<0` create a non-expiring (permanent) lock.** Contract: only explicit `free_keys` releases it. GC never reclaims it (because expiry is also -1). Useful for ownership that must survive process restarts; misuse causes resource leaks. Don't "fix" this without changing the contract first.
- **`cache_timeout=0` on `malloc()` / `update_soft_bind()` creates a permanent soft binding.** Sticks until explicitly overwritten (next `update_soft_bind` for the same name) or removed (`unbind_soft_bind`). Symmetric to the lock-timeout contract above.
- **`assign()` does not delete the lock keys of items it evicts from the pool.** This is deliberate to avoid write amplification on every `assign` call. Locks expire on their own TTL; if the same key is later re-extended, its existing lock protects it (subsequent `pop_from_head` skips locked items). Caveat: if an evicted key never returns, its lock is an orphan — callers who care must `DEL <prefix>|<suffix>:<key>` themselves.
