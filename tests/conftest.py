"""Fixtures for tests."""

import pytest
import fakeredis
from redis.client import Redis
from redis_allocator.lock import RedisLock, RedisLockPool, ThreadLock, ThreadLockPool
from redis_allocator.allocator import (
    RedisAllocator, RedisThreadHealthCheckPool, RedisAllocatableClass,
    RedisAllocatorUpdater, DefaultRedisAllocatorPolicy
)

# ---------------------------------------------------------------------------
# fakeredis upstream HSET-O(n) workaround — TEMPORARY BRIDGE.
#
# fakeredis ``HashCommandsMixin._hset`` calls ``len(h.keys())`` twice. Because
# ``Hash.keys()`` constructs a fresh ``[asbytes(k) for k in ...]`` list every
# call, each HSET is O(n) in the existing hash size — even though
# ``Hash.__len__`` itself is O(1). Real Redis HSET is hard O(1).
#
# The fix is upstream: https://github.com/cunla/fakeredis-py/pull/473
# (merged 2026-04-26 as commit 1e1173b on master). However, no release tag
# yet contains the fix — latest PyPI release is fakeredis 2.35.1, which
# still has the bug.
#
# This monkey-patch is therefore a bridge until fakeredis cuts a release
# with the fix.
#
# ============================================================
# CLEANUP CHECKLIST — do this in ONE commit when fakeredis releases the fix:
#   1. Delete this entire block (the ``_install_...`` function and its call).
#   2. Bump ``setup.py`` test dep to ``fakeredis[lua] >= <release>`` where
#      <release> is the first version containing PR #473 (e.g. 2.36.0 or
#      2.37.0 — check the release notes / git tag).
#   3. Update CLAUDE.md "Project Snapshot" — remove the workaround mention.
#   4. Verify ``pytest -m benchmark`` still passes on a fresh install.
# Reference:
#   - PR: https://github.com/cunla/fakeredis-py/pull/473
#   - Local fix branch: D:\Projects\GitHub\fakeredis-py:fix-hset-on-large-hash
# ============================================================


def _install_fakeredis_hset_o1_patch() -> None:
    """Replace HashCommandsMixin._hset with an O(1) variant (idempotent).

    Semantics are identical to the upstream version: both ``len(h.keys())``
    and ``len(h)`` invoke ``Hash._expire_keys()`` first and return the same
    field count. The patch only changes algorithmic complexity from O(n) to
    O(1) per HSET, which lets ``pytest -m benchmark`` enforce the
    allocator's true constant-time contract instead of measuring fakeredis
    bridge overhead.

    The function is a no-op once fakeredis is updated past PR #473 because
    ``len(h.keys())`` will already be ``len(h)`` upstream — re-installing
    the same logic is harmless.
    """
    try:
        from fakeredis.commands_mixins import hash_mixin as _hm
    except ImportError:
        return
    if getattr(_hm.HashCommandsMixin, "_hset_patched_o1", False):
        return
    _orig = _hm.HashCommandsMixin._hset

    def _hset_o1(self, key, *args):
        h = key.value
        previous_keys_count = len(h)
        h.update(dict(zip(*[iter(args)] * 2)), clear_expiration=True)
        created = len(h) - previous_keys_count
        key.updated()
        return created

    _hm.HashCommandsMixin._hset = _hset_o1
    _hm.HashCommandsMixin._hset_patched_o1 = True
    _hm.HashCommandsMixin._hset_original = _orig


_install_fakeredis_hset_o1_patch()


@pytest.fixture
def redis_client():
    """Create a fakeredis client for testing."""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_client_raw():
    """Create a fakeredis client with decode_responses=False for testing."""
    return fakeredis.FakeRedis(decode_responses=False)


@pytest.fixture
def redis_lock(redis_client: Redis):
    """Create a RedisLock for testing."""
    return RedisLock(redis_client, 'test-lock')


@pytest.fixture
def redis_lock_pool(redis_client: Redis):
    """Create a RedisLockPool for testing."""
    pool = RedisLockPool(redis_client, 'test-pool')
    yield pool
    pool.clear()


@pytest.fixture
def thread_lock():
    """Create a ThreadLock for testing."""
    return ThreadLock()


@pytest.fixture
def thread_lock_pool():
    """Create a ThreadLockPool for testing."""
    return ThreadLockPool()


# Test helper classes
class _TestObject(RedisAllocatableClass):
    """Test implementation of RedisAllocatableClass for testing."""

    def __init__(self, name=None):
        self.config_key = None
        self.config_params = None
        self.closed = False
        self._name = name

    def set_config(self, key, params):
        """Set configuration parameters."""
        self.config_key = key
        self.config_params = params

    def open(self):
        """Open the object."""
        self.closed = False
        return self

    def close(self):
        """Mark the object as closed."""
        self.closed = True

    @property
    def name(self):
        """Return a name for soft binding."""
        return self._name


class _TestUpdater(RedisAllocatorUpdater):
    """Test implementation of RedisAllocatorUpdater."""

    def __init__(self, updates):
        super().__init__(updates)
        self.call_count = 0

    def fetch(self, param):
        """Fetch keys based on the param."""
        self.call_count += 1
        return param


# Additional fixtures
@pytest.fixture(params=[None, "test_object"])
def test_object(request: pytest.FixtureRequest) -> _TestObject:
    """Create a test object implementing RedisAllocatableClass."""
    return _TestObject(request.param)


@pytest.fixture(params=[False, True])
def redis_allocator(redis_client: Redis, request: pytest.FixtureRequest) -> RedisAllocator:
    """Create a RedisAllocator instance for testing."""
    alloc = RedisAllocator(
        redis_client,
        'test',
        'alloc-lock',
        shared=request.param
    )
    # Set up initial keys
    alloc.extend(['key1', 'key2', 'key3'])
    return alloc


@pytest.fixture
def health_checker(redis_client: Redis) -> RedisThreadHealthCheckPool:
    """Create a RedisThreadHealthCheckPool instance for testing."""
    return RedisThreadHealthCheckPool(
        redis_client,
        'test',
        timeout=60
    )


@pytest.fixture
def test_updater() -> _TestUpdater:
    """Create a test updater."""
    return _TestUpdater([["key1", "key2"], ["key4", "key5", "key6"], ["key7", "key8", "key9"]])


@pytest.fixture(params=[False, True])
def allocator_with_policy(redis_client: Redis, test_updater: _TestUpdater, request: pytest.FixtureRequest) -> RedisAllocator:
    """Create a RedisAllocator with a default policy."""
    policy = DefaultRedisAllocatorPolicy(
        gc_count=2,
        update_interval=60,
        expiry_duration=300,
        updater=test_updater
    )

    alloc = RedisAllocator(
        redis_client,
        'test-policy',
        'alloc-lock',
        shared=request.param,
        policy=policy
    )

    # Set up initial keys
    alloc.extend(['key1', 'key2', 'key3'])
    return alloc
