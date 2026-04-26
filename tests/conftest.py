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
# fakeredis upstream bug workaround.
#
# fakeredis 2.35.x ``HashCommandsMixin._hset`` calls ``len(h.keys())`` twice
# per HSET. ``Hash.keys()`` (in fakeredis/model/_hash.py) constructs a fresh
# ``[asbytes(k) for k in ...]`` list every call — making each HSET O(n) in
# the hash's existing size, even though Hash.__len__ itself is O(1).
#
# Real Redis HSET is hard O(1). This monkey-patch replaces the inner helper
# with the equivalent O(1) version so that fakeredis-based benchmarks reflect
# the allocator's true complexity rather than fakeredis's instrumentation
# overhead. The semantics are identical: ``len(h.keys())`` and ``len(h)`` both
# return the number of fields, both call ``_expire_keys()`` first, and the
# returned ``created`` count is identical.
#
# Upstream issue worth filing.
# ---------------------------------------------------------------------------


def _install_fakeredis_hset_o1_patch() -> None:
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
