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


class _TestRedisAllocator(RedisAllocator):

    @property
    def _lua_required_string(self):
        return f'''
        os.time = function() return tonumber(redis.call("TIME")[1]) end
        {super()._lua_required_string}
        '''


@pytest.fixture(params=[False, True])
def allocator_with_policy(redis_client: Redis, test_updater: _TestUpdater, request: pytest.FixtureRequest) -> RedisAllocator:
    """Create a RedisAllocator with a default policy."""
    policy = DefaultRedisAllocatorPolicy(
        gc_count=2,
        update_interval=60,
        expiry_duration=300,
        updater=test_updater
    )

    alloc = _TestRedisAllocator(
        redis_client,
        'test-policy',
        'alloc-lock',
        shared=request.param,
        policy=policy
    )

    # Set up initial keys
    alloc.extend(['key1', 'key2', 'key3'])
    return alloc
