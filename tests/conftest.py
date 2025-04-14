"""Fixtures for tests."""

import pytest
import fakeredis
from redis.client import Redis
from redis_allocator.lock import RedisLock, RedisLockPool, ThreadLock, ThreadLockPool
from redis_allocator.allocator import (
    RedisAllocator, RedisThreadHealthCheckPool, RedisAllocatableClass,
    RedisAllocatorUpdater, DefaultRedisAllocatorPolicy
)


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
    pool = ThreadLockPool()
    yield pool
    pool.clear()


# Test helper classes
class _TestObject(RedisAllocatableClass):
    """Test implementation of RedisAllocatableClass for testing."""

    def __init__(self):
        self.config_key = None
        self.config_params = None
        self.closed = False

    def set_config(self, key, params):
        """Set configuration parameters."""
        self.config_key = key
        self.config_params = params

    def close(self):
        """Mark the object as closed."""
        self.closed = True

    def name(self):
        """Return a name for soft binding."""
        return "test_object"


class _TestNamedObject(RedisAllocatableClass):
    """Test implementation of RedisAllocatableClass with a name."""

    def __init__(self, name):
        self.config_key = None
        self.config_params = None
        self.closed = False
        self._name = name

    def set_config(self, key, params):
        """Set configuration parameters."""
        self.config_key = key
        self.config_params = params

    def close(self):
        """Mark the object as closed."""
        self.closed = True

    @property
    def name(self):
        """Return the object name for soft binding."""
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
@pytest.fixture
def test_object() -> _TestObject:
    """Create a test object implementing RedisAllocatableClass."""
    return _TestObject()


@pytest.fixture
def redis_allocator(redis_client: Redis) -> RedisAllocator:
    """Create a RedisAllocator instance for testing."""
    alloc = RedisAllocator(
        redis_client,
        'test',
        'alloc-lock',
        shared=False
    )
    # Set up initial keys
    alloc.extend(['key1', 'key2', 'key3'])
    return alloc


@pytest.fixture
def shared_allocator(redis_client: Redis) -> RedisAllocator:
    """Create a shared RedisAllocator instance for testing."""
    alloc = RedisAllocator(
        redis_client,
        'test',
        'shared-alloc',
        shared=True
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
def named_object() -> _TestNamedObject:
    """Create a test object with a name."""
    return _TestNamedObject("test_named_object")


@pytest.fixture
def test_updater() -> _TestUpdater:
    """Create a test updater."""
    return _TestUpdater([["key1", "key2"], ["key3"], ["key4", "key5", "key6"]])


@pytest.fixture
def allocator_with_policy(redis_client: Redis, test_updater: _TestUpdater) -> RedisAllocator:
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
        shared=False,
        policy=policy
    )

    # Set up initial keys
    alloc.extend(['key1', 'key2', 'key3'])
    return alloc
