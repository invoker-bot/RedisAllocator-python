# flake8: noqa: F401
"""Tests for the Redis-based distributed memory allocation system.

This module tests the functionality of:
1. RedisThreadHealthCheckPool - For thread health monitoring
2. RedisAllocator - For distributed resource allocation
3. RedisAllocatorObject - For managing allocated resources
"""
import time
import datetime
import threading
# from itertools import cycle
from operator import xor
from pytest_mock import MockFixture
from redis import Redis
from freezegun import freeze_time
from redis_allocator.allocator import (
    RedisAllocator, RedisThreadHealthCheckPool, RedisAllocatorObject,
)
from tests.conftest import _TestObject

class TestRedisThreadHealthCheckPool:
    """Tests for the RedisThreadHealthCheckPool class."""

    def test_thread_health_check(self, redis_client: Redis):
        """Test the thread health check mechanism in a multi-thread environment.

        This test creates actual threads:
        - Some threads are "healthy" (regularly update their status)
        - Some threads are "unhealthy" (stop updating their status)

        We verify that the health check correctly identifies the healthy vs unhealthy threads.
        """

        # Set up a health checker with a short timeout to make testing faster
        health_timeout = 60  # 3 seconds timeout for faster testing

        # Start time for our simulation
        start_time = datetime.datetime(2023, 1, 1, 12, 0, 0)

        with freeze_time(start_time) as frozen_time:
            checker = RedisThreadHealthCheckPool(redis_client, 'test-health', timeout=health_timeout)

            # Track thread IDs for verification
            thread_ids = {}
            threads = []
            stop_event = threading.Event()

            # Thread that keeps updating (healthy)
            def healthy_thread():
                checker.initialize()
                thread_id = str(threading.current_thread().ident)
                thread_ids[threading.current_thread().name] = thread_id
                while not stop_event.is_set():
                    checker.update()
                    # We don't actually sleep in the threads
                    # They'll continue running while we control time externally
                    if stop_event.wait(0.01):  # Small wait to prevent CPU spinning
                        break

            # Thread that stops updating (becomes unhealthy)
            def unhealthy_thread():
                checker.initialize()
                thread_id = str(threading.current_thread().ident)
                thread_ids[threading.current_thread().name] = thread_id
                # Only update once, then stop (simulating a dead thread)

            # Create and start threads
            for i in range(3):
                t = threading.Thread(target=healthy_thread, name=f"healthy-{i}", daemon=True)
                t.start()
                threads.append(t)

            for i in range(2):
                t = threading.Thread(target=unhealthy_thread, name=f"unhealthy-{i}", daemon=True)
                t.start()
                threads.append(t)

            # Wait for all threads to register
            # Instead of time.sleep(1), advance time using freeze_time
            frozen_time.tick(1.0)

            # Get initial thread status - all should be healthy initially
            registered_threads = list(checker.keys())
            for thread_name, thread_id in thread_ids.items():
                assert thread_id in registered_threads, f"Thread {thread_name} should be registered"
                assert checker.is_locked(thread_id), f"Thread {thread_id} should be locked/healthy"

            # Wait for unhealthy threads to expire
            # Advance time past the health_timeout
            frozen_time.tick(health_timeout + 1)
            time.sleep(0.1)
            # Now verify healthy vs unhealthy status
            for thread_name, thread_id in thread_ids.items():
                if thread_name.startswith("healthy"):
                    assert checker.is_locked(thread_id), f"Thread {thread_name} should still be locked/healthy"
                else:
                    assert not checker.is_locked(thread_id), f"Thread {thread_name} should be unlocked/unhealthy"

            # Clean up
            stop_event.set()

    def test_thread_recovery(self, redis_client: Redis):
        """Test that a thread can recover after being marked as unhealthy.

        This simulates a scenario where a thread stops updating for a while (becomes unhealthy),
        but then recovers and starts updating again (becomes healthy).
        """

        # Start time for our simulation
        start_time = datetime.datetime(2023, 1, 1, 12, 0, 0)

        with freeze_time(start_time) as frozen_time:
            # Set up a health checker with a short timeout
            health_timeout = 2  # 2 seconds timeout for faster testing
            checker = RedisThreadHealthCheckPool(redis_client, 'test-recovery', timeout=health_timeout)

            # Control variables
            pause_updates = threading.Event()
            resume_updates = threading.Event()
            stop_thread = threading.Event()
            thread_id = None

            # Thread function that will pause and resume updates
            def recovery_thread():
                nonlocal thread_id
                checker.initialize()
                thread_id = str(threading.current_thread().ident)

                # Update until told to pause
                while not pause_updates.is_set() and not stop_thread.is_set():
                    checker.update()
                    stop_thread.wait(0.01)  # Small wait to prevent CPU spinning

                # Wait until told to resume
                resume_updates.wait()

                if not stop_thread.is_set():
                    # Recover by re-initializing
                    checker.initialize()

                    # Continue updating
                    while not stop_thread.is_set():
                        checker.update()
                        stop_thread.wait(0.01)

            # Start the thread
            thread = threading.Thread(target=recovery_thread)
            thread.daemon = True
            thread.start()

            # Wait for thread to initialize
            frozen_time.tick(0.5)

            # Verify thread is initially healthy
            assert thread_id is not None, "Thread ID should be set"
            assert checker.is_locked(thread_id), "Thread should be healthy after initialization"

            # Pause updates to let the thread become unhealthy
            pause_updates.set()

            # Wait for thread to become unhealthy by advancing time
            frozen_time.tick(health_timeout + 1)

            # Verify thread is now unhealthy
            assert not checker.is_locked(thread_id), "Thread should be unhealthy after timeout"

            # Tell thread to resume updates
            resume_updates.set()

            # Wait for thread to recover
            frozen_time.tick(1.0)
            time.sleep(0.1)

            # Verify thread is healthy again
            assert checker.is_locked(thread_id), "Thread should be healthy after recovery"

            # Clean up
            stop_thread.set()
            thread.join(timeout=1)


class TestRedisAllocatorObject:
    """Tests for the RedisAllocatorObject class."""

    def test_initialization(self, redis_allocator: RedisAllocator, test_object: _TestObject):
        """Test that initialization correctly sets up the object."""
        params = {"param1": "value1", "param2": "value2"}
        obj = RedisAllocatorObject(redis_allocator, "test_key", test_object, params)

        assert obj.allocator == redis_allocator
        assert obj.key == "test_key"
        assert obj.obj == test_object
        assert obj.params == params
        assert test_object.config_key == "test_key"
        assert test_object.config_params == params

    def test_update(self, redis_allocator: RedisAllocator, test_object: _TestObject, mocker: MockFixture):
        """Test the update method."""
        obj = RedisAllocatorObject(redis_allocator, "test_key", test_object, {})
        mock_alloc_update = mocker.patch.object(redis_allocator, 'update')
        mock_alloc_free = mocker.patch.object(redis_allocator, 'free')
        obj.update(60)
        mock_alloc_update.assert_called_once_with("test_key", timeout=60)
        mock_alloc_free.assert_not_called()
        obj.update(0)
        mock_alloc_update.assert_called_once()
        mock_alloc_free.assert_called_once()

    def test_close(self, redis_allocator: RedisAllocator, test_object: _TestObject):
        """Test the close method."""
        obj = RedisAllocatorObject(redis_allocator, "test_key", test_object, {})
        obj.open()
        assert not test_object.closed
        obj.close()
        assert test_object.closed
        obj.close()  # Should not raise
        obj.refresh()
        assert not test_object.closed


class TestRedisAllocator:
    """Tests for the RedisAllocator class."""

    def get_redis_pool_state(self, redis_allocator: RedisAllocator, redis_client: Redis):
        """Get the current state of the Redis pool."""
        head_key = redis_client.get(redis_allocator._pool_pointer_str(True))
        tail_key = redis_client.get(redis_allocator._pool_pointer_str(False))
        pool_state = redis_client.hgetall(redis_allocator._pool_str())
        locked_status = dict(redis_allocator.items_locked_status())
        return {
            "pool_state": pool_state,
            "head_key": head_key,
            "tail_key": tail_key,
            "locked_status": locked_status
        }

    def time_to_expire(self, redis_client: Redis, timeout: int):
        if timeout > 0:
            return int(redis_client.time()[0]) + timeout
        return -1

    def generate_pool_state(self, redis_client: Redis, free_keys: list[str | tuple[str, int]], allocated_keys: list[str | tuple[str, int]]):
        free_keys = [keys if isinstance(keys, tuple) else (keys, -1) for keys in free_keys]
        allocated_keys = [keys if isinstance(keys, tuple) else (keys, -1) for keys in allocated_keys]

        state = {
            "pool_state": {},
            "head_key": free_keys[0][0] if len(free_keys) > 0 else '',
            "tail_key": free_keys[-1][0] if len(free_keys) > 0 else '',
            "locked_status": {},
        }
        for prev_key, current_key, next_key in zip([('', -1)] + free_keys[:-1], free_keys, free_keys[1:] + [('', -1)]):
            state["pool_state"][current_key[0]] = f'{prev_key[0]}||{next_key[0]}||{self.time_to_expire(redis_client, current_key[1])}'
            state["locked_status"][current_key[0]] = False
        for key in allocated_keys:
            state["pool_state"][key[0]] = f'#ALLOCATED||#ALLOCATED||{self.time_to_expire(redis_client, key[1])}'
            state["locked_status"][key[0]] = True
        return state

    def test_initialization(self, redis_allocator: RedisAllocator, redis_client: Redis):
        """Test the initialization of Redisredis_allocator."""
        assert self.get_redis_pool_state(redis_allocator, redis_client) == self.generate_pool_state(
            redis_client,
            ['key1', 'key2', 'key3'],
            []
        )

    def test_allocation_and_freeing(self, redis_allocator: RedisAllocator, redis_client: Redis):
        """Test basic allocation/freeing using Lua scripts and verify Redis state."""
        shared = redis_allocator.shared
        test_keys = ["key1", "key2", "key3"]
        redis_allocator.extend(test_keys)
        self.test_initialization(redis_allocator, redis_client)

        for key in test_keys:
            assert key in redis_allocator
            assert not redis_allocator.is_locked(key)
            assert not redis_client.exists(redis_allocator._key_str(key))

        # print(self.get_redis_pool_state(redis_allocator, redis_client))
        allocated_key = redis_allocator.malloc_key(timeout=30)
        assert allocated_key == "key1"
        assert xor(shared, redis_allocator.is_locked(allocated_key))
        assert xor(shared, redis_client.exists(redis_allocator._key_str(allocated_key)))
        assert redis_client.ttl(redis_allocator._key_str(allocated_key)) <= 30
        assert self.get_redis_pool_state(redis_allocator, redis_client) == self.generate_pool_state(
            redis_client,
            (['key2', 'key3', 'key1'] if shared else ['key2', 'key3']),
            ([] if shared else ['key1'])
        )

        allocated_key = redis_allocator.malloc_key(timeout=30)
        assert allocated_key == "key2"
        assert xor(shared, redis_allocator.is_locked(allocated_key))
        assert xor(shared, redis_client.exists(redis_allocator._key_str(allocated_key)))
        assert redis_client.ttl(redis_allocator._key_str(allocated_key)) <= 30
        assert self.get_redis_pool_state(redis_allocator, redis_client) == self.generate_pool_state(
            redis_client,
            (['key3', 'key1', 'key2'] if shared else ['key3']),
            ([] if shared else ['key1', 'key2'])
        )

        redis_allocator.free_keys("key1")
        assert self.get_redis_pool_state(redis_allocator, redis_client) == self.generate_pool_state(
            redis_client,
            (['key3', 'key1', 'key2'] if shared else ['key3', 'key1']),
            ([] if shared else ['key2'])
        )

        {
            "locked_status": {
                'key1': False,
                'key2': not shared,
                'key3': False
            },
            "pool_state": {
                'key1': f'key3||{("key2" if shared else "")}||-1',
                'key2': ('key1||||-1' if shared else '#ALLOCATED||#ALLOCATED||-1'),
                'key3': '||key1||-1'
            },
            "head_key": 'key3',
            "tail_key": ('key2' if shared else 'key1')
        }

        redis_allocator.free_keys("key2")
        assert self.get_redis_pool_state(redis_allocator, redis_client) == self.generate_pool_state(
            redis_client,
            ['key3', 'key1', 'key2'],
            []
        )

    def test_allocation_with_object(self, redis_allocator: RedisAllocator, redis_client: Redis,
                                    test_object: _TestObject):
        """Test allocation with object wrapper using Lua scripts and verify Redis state."""
        shared = redis_allocator.shared
        params = {"param1": "value1"}
        # Determine potential bind key (though it shouldn't be created)

        bind_key = redis_allocator._soft_bind_name(test_object.name) if test_object.name else None

        # --- Allocation ---
        alloc_obj = redis_allocator.malloc(timeout=30, obj=test_object, params=params)

        # Verify object and Redis state after allocation
        assert alloc_obj is not None
        assert isinstance(alloc_obj, RedisAllocatorObject)
        assert alloc_obj.key == "key1"
        assert alloc_obj.obj == test_object
        assert alloc_obj.params == params
        assert test_object.config_key == alloc_obj.key
        assert test_object.config_params == params
        assert xor(shared, redis_client.exists(redis_allocator._key_str(alloc_obj.key)))
        assert self.get_redis_pool_state(redis_allocator, redis_client) == self.generate_pool_state(
            redis_client,
            (['key2', 'key3', 'key1'] if shared else ['key2', 'key3']),
            ([] if shared else ['key1'])
        )

        if bind_key is not None:
            assert redis_client.exists(bind_key)
        redis_allocator.free(alloc_obj)

        # Verify state after freeing
        assert not redis_allocator.is_locked(alloc_obj.key)
        assert redis_client.exists(redis_allocator._key_str(alloc_obj.key)) == 0
        assert alloc_obj.key in redis_allocator
        assert self.get_redis_pool_state(redis_allocator, redis_client) == self.generate_pool_state(
            redis_client,
            ['key2', 'key3', 'key1'],
            []
        )

    def test_gc_functionality(self, allocator_with_policy: RedisAllocator, redis_client: Redis):
        """Test GC scenarios by interacting directly with Redis via Lua scripts."""
        shared = allocator_with_policy.shared
        with freeze_time("2024-01-01") as time:
            alloc_key1 = allocator_with_policy.malloc_key(timeout=1)
            assert alloc_key1 == "key1"
            alloc_key2 = allocator_with_policy.malloc_key(timeout=1)
            assert alloc_key2 == "key2"
            alloc_key3 = allocator_with_policy.malloc_key(timeout=1)
            assert alloc_key3 == "key3"
            assert self.get_redis_pool_state(allocator_with_policy, redis_client) == self.generate_pool_state(
                redis_client,
                (['key1', 'key2', 'key3'] if shared else []),
                ([] if shared else ['key1', 'key2', 'key3'])
            )
            alloc_key4 = allocator_with_policy.malloc_key(timeout=1)
            if shared:
                assert alloc_key4 == "key1"
            else:
                assert alloc_key4 is None
                assert self.get_redis_pool_state(allocator_with_policy, redis_client) == self.generate_pool_state(
                    redis_client,
                    (['key2', 'key3', 'key1'] if shared else []),
                    ([] if shared else ['key1', 'key2', 'key3'])
                )
            time.tick(1.1)
            allocator_with_policy.gc()
            assert self.get_redis_pool_state(allocator_with_policy, redis_client) == self.generate_pool_state(
                redis_client,
                (['key2', 'key3', 'key1'] if shared else ['key1', 'key2', 'key3']),
                []
            )
            allocator_with_policy.extend(["key2"], timeout=2)
            assert self.get_redis_pool_state(allocator_with_policy, redis_client) == self.generate_pool_state(
                redis_client,
                ([('key2', 2), 'key3', 'key1'] if shared else ['key1', ('key2', 2), 'key3']),
                [])
            alloc_key5 = allocator_with_policy.malloc_key(timeout=1)
            assert alloc_key5 == ("key2" if shared else "key1")
            time.tick(3)
            allocator_with_policy.gc()
            assert self.get_redis_pool_state(allocator_with_policy, redis_client) == self.generate_pool_state(
                redis_client,
                ['key3', 'key1'],
                [])
            assert allocator_with_policy.policy.updater.index == 0
            assert len(allocator_with_policy) == 2
            obj = allocator_with_policy.malloc()
            assert len(allocator_with_policy) == 3
            assert obj.key == "key3"
            assert allocator_with_policy.policy.updater.index == 1
            assert self.get_redis_pool_state(allocator_with_policy, redis_client) == self.generate_pool_state(
                redis_client,
                [('key1', 300), ('key2', 300), 'key3'] if shared else [('key1', 300), ('key2', 300)],
                [] if shared else ['key3']
            )
            time.tick(600)
            allocator_with_policy.gc()
            print(self.get_redis_pool_state(allocator_with_policy, redis_client))
            redis_client.register_script("print('-------------------------------')")()
            allocator_with_policy.gc()  # some times should be called twice to remove the expired items
            redis_client.register_script("print('-------------------------------')")()
            print(self.get_redis_pool_state(allocator_with_policy, redis_client))
            allocator_with_policy.policy.refresh_pool(allocator_with_policy)
            assert len(allocator_with_policy) == 4
            allocator_with_policy.gc()
            assert self.get_redis_pool_state(allocator_with_policy, redis_client) == self.generate_pool_state(
                redis_client,
                ["key3", ("key4", 300), ("key5", 300), ("key6", 300)],
                []
            )



    def test_soft_binding(self, redis_allocator: RedisAllocator, redis_client: Redis, test_object: _TestObject):
        """Test soft binding mechanism with direct Redis interaction."""
        shared = redis_allocator.shared
        with freeze_time("2024-01-01") as time:
            # Extend the pool
            allocation1 = redis_allocator.malloc(timeout=30, obj=test_object, cache_timeout=10)
            assert allocation1.key == "key1"
            assert xor(shared, redis_allocator.is_locked("key1"))
            bind_str = redis_allocator._soft_bind_name(test_object.name) if test_object.name else None
            if bind_str is not None:
                assert redis_client.get(bind_str) == "key1"
                assert 1 < redis_client.ttl(bind_str) <= 10
            redis_allocator.free(allocation1)

            assert not redis_allocator.is_locked("key1")
            allocation2 = redis_allocator.malloc(timeout=30, obj=test_object, cache_timeout=10)
            assert allocation2.key == ("key1" if bind_str else "key2")
            if shared:
                if bind_str:
                    state = ['key2', 'key3', 'key1'], []
                else:
                    state = ['key3', 'key1', 'key2'], []
            else:
                if bind_str:
                    state = ['key2', 'key3'], ['key1']
                else:
                    state = ['key3', 'key1'], ['key2']
            assert self.get_redis_pool_state(redis_allocator, redis_client) == self.generate_pool_state(
                redis_client,
                *state
            )
            time.tick(31)
            redis_allocator.gc()
            assert self.get_redis_pool_state(redis_allocator, redis_client) == self.generate_pool_state(
                redis_client,
                (['key2', 'key3', 'key1'] if bind_str else ['key3', 'key1', 'key2']),
                []
            )
            allocation3 = redis_allocator.malloc_key(timeout=30, name="unbind", cache_timeout=10)
            assert allocation3 == ("key2" if bind_str else "key3")
            assert xor(shared, redis_allocator.is_locked(allocation3))
            if bind_str:
                if shared:
                    state = ['key3', 'key1', 'key2'], []
                else:
                    state = ['key3', 'key1'], ['key2']
            else:
                if shared:
                    state = ['key1', 'key2', 'key3'], []
                else:
                    state = ['key1', 'key2'], ['key3']
            assert self.get_redis_pool_state(redis_allocator, redis_client) == self.generate_pool_state(redis_client, *state)


    # def test_allocator_health_check(self, redis_allocator: RedisAllocator, mocker: MockFixture):
    #     ''''''
    #     with freeze_time("2024-01-01") as time:
    #         for i in range(100):
    #             obj = _TestObject(name = f"test_object_{i}")
    #             mocker.patch.object(obj, 'is_healthy', side_effect = cycle([True, False]))
    #             
    #             redis_allocator.malloc(obj = obj, timeout = 30)
    #             time.tick(30)
    #             redis_allocator.gc()
    #             unh, h = redis_allocator.health_check()
    #             assert len(redis_allocator.get_free_list()) == h
    #         # time.tick(100)
    #         # redis_allocator.gc()
    #         # assert not redis_allocator.is_locked("key1")
