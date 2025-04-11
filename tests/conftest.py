"""Fixtures for tests."""

import pytest
import fakeredis
from redis.client import Redis
from redis_allocator.lock import RedisLock, RedisLockPool, ThreadLock, ThreadLockPool


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
