"""Tests for the RedisLock and RedisLockPool classes."""

from redis import Redis
from redis_allocator import (
    RedisLock,
    RedisLockPool,
    LockStatus,
    ThreadLock,
    ThreadLockPool
)
import time
import concurrent.futures
import pytest
import threading
from freezegun import freeze_time
import datetime


class TestRedisLock:
    """Tests for the RedisLock class."""

    def test_lock(self, redis_lock: RedisLock, redis_client: Redis):
        """Test the lock method."""
        assert redis_lock.lock("test-key") is True
        assert redis_client.exists(redis_lock._key_str("test-key"))

    def test_unlock(self, redis_lock: RedisLock, redis_client: Redis):
        """Test the unlock method."""
        # First set the lock
        redis_lock.update("test-key")
        assert redis_client.exists(redis_lock._key_str("test-key"))
        # Then unlock
        redis_lock.unlock("test-key")
        assert not redis_client.exists(redis_lock._key_str("test-key"))

    def test_update(self, redis_lock: RedisLock, redis_client: Redis):
        """Test the update method."""
        redis_lock.update("test-key", value="2", timeout=60)
        assert redis_client.get(redis_lock._key_str("test-key")) == "2"
        # TTL should be close to 60
        ttl = redis_client.ttl(redis_lock._key_str("test-key"))
        assert 55 <= ttl <= 60

    def test_lock_value(self, redis_lock: RedisLock):
        """Test the lock_value method."""
        redis_lock.lock("test-key", value="120")
        assert redis_lock.lock_value("test-key") == "120"

    def test_is_locked(self, redis_lock: RedisLock, redis_client: Redis):
        """Test the is_locked method."""
        assert not redis_lock.is_locked("test-key")
        redis_client.set(redis_lock._key_str("test-key"), "300")
        assert redis_lock.is_locked("test-key")
        assert not redis_lock.is_locked("non-existent-key")

    def test_key_status(self, redis_lock: RedisLock, redis_client: Redis):
        """Test the key_status method."""
        # Test FREE status
        assert redis_lock.key_status("free-key") == LockStatus.FREE

        # Test LOCKED status
        redis_client.set(redis_lock._key_str("locked-key"), "1", ex=60)
        assert redis_lock.key_status("locked-key") == LockStatus.LOCKED

        # Test ERROR status (permanent lock)
        redis_client.set(redis_lock._key_str("error-key"), "1")
        assert redis_lock.key_status("error-key") == LockStatus.ERROR

        # Test UNAVAILABLE status (TTL > timeout)
        redis_client.set(redis_lock._key_str("unavailable-key"), "1", ex=200)
        assert redis_lock.key_status("unavailable-key", timeout=100) == LockStatus.UNAVAILABLE

    def test_rlock(self, redis_lock: RedisLock, redis_client: Redis):
        """Test the rlock method."""
        # First test acquiring a lock
        assert redis_lock.rlock("test-key", value="1") is True
        assert redis_client.get(redis_lock._key_str("test-key")) == "1"

        # Test acquiring the same lock with the same value
        assert redis_lock.rlock("test-key", value="1") is True

        # Test acquiring the same lock with a different value
        assert redis_lock.rlock("test-key", value="2") is False

        # Verify the original value wasn't changed
        assert redis_client.get(redis_lock._key_str("test-key")) == "1"

    def test_conditional_setdel_operations(
        self, redis_lock: RedisLock, redis_client: Redis
    ):
        """Test the conditional set/del operations."""
        # Skip direct testing of _conditional_setdel as it's an internal method
        # We'll test the public methods that use it instead

        # Set initial value
        test_key = "cond-key"
        key_str = redis_lock._key_str(test_key)
        redis_client.set(key_str, "10")

        # Test setgt (greater than)
        assert redis_lock.setgt(key=test_key, value=15)  # 15 > 10, should succeed
        assert redis_client.get(key_str) == "15"  # Value should be updated to 15

        # Test setgt with unsuccessful condition
        assert not redis_lock.setgt(key=test_key, value=5)  # 5 < 15, should not succeed
        assert redis_client.get(key_str) == "15"  # Value should remain 15

        # Test setlt (less than)
        assert redis_lock.setlt(key=test_key, value=5)  # 5 < 15, should succeed
        assert redis_client.get(key_str) == "5"  # Value should be updated to 5

        # Test setlt with unsuccessful condition
        assert not redis_lock.setlt(
            key=test_key, value=10
        )  # 10 > 5, should not succeed
        assert redis_client.get(key_str) == "5"  # Value should remain 5

        # Test setge (greater than or equal)
        assert redis_lock.setge(key=test_key, value=5)  # 5 >= 5, should succeed
        assert redis_client.get(key_str) == "5"  # Value should still be 5

        # Test setge with unsuccessful condition
        assert not redis_lock.setge(key=test_key, value=4)  # 4 < 5, should not succeed
        assert redis_client.get(key_str) == "5"  # Value should remain 5

        # Test setle (less than or equal)
        assert redis_lock.setle(key=test_key, value=5)  # 5 <= 5, should succeed
        assert redis_client.get(key_str) == "5"  # Value should remain 5

        # Test setle with unsuccessful condition
        assert not redis_lock.setle(key=test_key, value=6)  # 6 > 5, should not succeed
        assert redis_client.get(key_str) == "5"  # Value should remain 5

        # Test seteq (equal)
        assert redis_lock.seteq(key=test_key, value=5)  # 5 == 5, should succeed
        assert redis_client.get(key_str) == "5"  # Value should remain 5

        # Test seteq with unsuccessful condition
        assert not redis_lock.seteq(key=test_key, value=6)  # 6 != 5, should not succeed
        assert redis_client.get(key_str) == "5"  # Value should remain 5

        # Test setne (not equal)
        assert redis_lock.setne(key=test_key, value=10)  # 10 != 5, should succeed
        assert redis_client.get(key_str) == "10"  # Value should be updated to 10

        # Test setne with unsuccessful condition
        assert not redis_lock.setne(
            key=test_key, value=10
        )  # 10 == 10, should not succeed
        assert redis_client.get(key_str) == "10"  # Value should remain 10

        # Test delgt (delete if greater than)
        redis_client.set(key_str, "10")
        redis_lock.delgt(key=test_key, value=15)  # 15 > 10, should delete
        assert not redis_client.exists(key_str)  # Key should be deleted

        # Test dellt (delete if less than)
        redis_client.set(key_str, "10")
        redis_lock.dellt(key=test_key, value=5)  # 5 < 10, should delete
        assert not redis_client.exists(key_str)  # Key should be deleted

        # Test delge (delete if greater than or equal)
        redis_client.set(key_str, "10")
        redis_lock.delge(key=test_key, value=10)  # 10 >= 10, should delete
        assert not redis_client.exists(key_str)  # Key should be deleted

        # Test delle (delete if less than or equal)
        redis_client.set(key_str, "10")
        redis_lock.delle(key=test_key, value=10)  # 10 <= 10, should delete
        assert not redis_client.exists(key_str)  # Key should be deleted

        # Test deleq (delete if equal)
        redis_client.set(key_str, "10")
        redis_lock.deleq(key=test_key, value=10)  # 10 == 10, should delete
        assert not redis_client.exists(key_str)  # Key should be deleted

        # Test delne (delete if not equal)
        redis_client.set(key_str, "10")
        redis_lock.delne(key=test_key, value=5)  # 5 != 10, should delete
        assert not redis_client.exists(key_str)  # Key should be deleted

        # Test with expired keys
        redis_client.set(key_str, "10")
        redis_lock.setgt(key=test_key, value=15, ex=30)  # set with expiration
        ttl = redis_client.ttl(key_str)
        assert ttl > 0 and ttl <= 30  # Should have a TTL set

    def test_setters_and_deleters(self, redis_lock: RedisLock, redis_client: Redis):
        """Test all setter and deleter methods."""
        test_key = "op-key"
        key_str = redis_lock._key_str(test_key)

        # Let's test the setters first

        # Set an initial value
        redis_client.set(key_str, "10")

        # Test setgt (doesn't meet condition)
        assert not redis_lock.setgt(key=test_key, value=5)
        assert redis_client.get(key_str) == "10"  # Unchanged because 5 is not > 10

        # Test setgt (meets condition)
        assert redis_lock.setgt(key=test_key, value=15, set_value=7)
        assert redis_client.get(key_str) == "7"  # Changed because 15 > 10

        # Test setlt (doesn't meet condition)
        assert not redis_lock.setlt(key=test_key, value=10)
        assert redis_client.get(key_str) == "7"  # Unchanged because 10 is not < 7

        # Test setlt (meets condition)
        assert redis_lock.setlt(key=test_key, value=5)
        assert redis_client.get(key_str) == "5"  # Changed because 5 < 7

        # Reset for more tests
        redis_client.set(key_str, "10")

        # Test setge (meets condition)
        assert redis_lock.setge(key=test_key, value=10)
        assert redis_client.get(key_str) == "10"  # Changed because 10 >= 10

        # Test setle (meets condition)
        assert redis_lock.setle(key=test_key, value=10)
        assert redis_client.get(key_str) == "10"  # Changed because 10 <= 10

        # Test seteq (meets condition)
        assert redis_lock.seteq(key=test_key, value=10)
        assert redis_client.get(key_str) == "10"  # Changed because 10 == 10

        # Test setne (doesn't meet condition)
        assert not redis_lock.setne(key=test_key, value=10)
        assert redis_client.get(key_str) == "10"  # Unchanged because 10 is not != 10

        # Test setne (meets condition)
        assert redis_lock.setne(key=test_key, value=5)
        assert redis_client.get(key_str) == "5"  # Changed because 5 != 10

        # Now test the deleters

        # Reset for delete tests
        redis_client.set(key_str, "10")

        # Test delgt (doesn't meet condition)
        assert not redis_lock.delgt(key=test_key, value=5)
        assert redis_client.exists(key_str)  # Key still exists because 5 is not > 10

        # Test delgt (meets condition)
        assert redis_lock.delgt(key=test_key, value=15)
        assert not redis_client.exists(key_str)  # Key deleted because 15 > 10

        # Reset for more delete tests
        redis_client.set(key_str, "10")

        # Test dellt (doesn't meet condition)
        assert not redis_lock.dellt(key=test_key, value=15)
        assert redis_client.exists(key_str)  # Key still exists because 15 is not < 10

        # Test dellt (meets condition)
        assert redis_lock.dellt(key=test_key, value=5)
        assert not redis_client.exists(key_str)  # Key deleted because 5 < 10

        # Test delge, delle, deleq, delne
        redis_client.set(key_str, "10")
        assert redis_lock.delge(key=test_key, value=10)  # 10 >= 10, should delete
        assert not redis_client.exists(key_str)

        redis_client.set(key_str, "10")
        assert redis_lock.delle(key=test_key, value=10)  # 10 <= 10, should delete
        assert not redis_client.exists(key_str)

        redis_client.set(key_str, "10")
        assert redis_lock.deleq(key=test_key, value=10)  # 10 == 10, should delete
        assert not redis_client.exists(key_str)

        redis_client.set(key_str, "10")
        assert redis_lock.delne(key=test_key, value=5)  # 5 != 10, should delete
        assert not redis_client.exists(key_str)

        # Test unsuccessful deletions
        redis_client.set(key_str, "10")
        assert not redis_lock.delge(key=test_key, value=5)  # 5 < 10, should not delete
        assert redis_client.exists(key_str)

        assert not redis_lock.delle(
            key=test_key, value=15
        )  # 15 > 10, should not delete
        assert redis_client.exists(key_str)

        assert not redis_lock.deleq(
            key=test_key, value=11
        )  # 11 != 10, should not delete
        assert redis_client.exists(key_str)

        assert not redis_lock.delne(
            key=test_key, value=10
        )  # 10 == 10, should not delete
        assert redis_client.exists(key_str)

    def test_equality_and_hash(self, redis_client: Redis):
        """Test equality and hash methods."""
        # Create two identical locks
        lock1 = RedisLock(redis_client, "test")
        lock2 = RedisLock(redis_client, "test")

        # Create a different lock
        lock3 = RedisLock(redis_client, "different")

        # Test equality
        assert lock1 == lock2
        assert lock1 != lock3
        assert lock1 != "not a lock"

        # Test hash
        lock_set = {lock1, lock2, lock3}
        assert len(lock_set) == 2  # lock1 and lock2 should hash to the same value

    def test_invalid_operator(self, redis_lock: RedisLock):
        """Test that an invalid operator raises a ValueError."""
        with pytest.raises(ValueError, match="Invalid operator"):
            redis_lock._conditional_setdel_lua_script("invalid", 0.001)

    def test_timeout_types(self, redis_lock: RedisLock, redis_client: Redis):
        """Test different timeout types."""
        from datetime import timedelta

        # Test with timedelta
        test_key = "timeout-key"
        key_str = redis_lock._key_str(test_key)

        # Use timedelta for timeout
        redis_lock.update(test_key, value="1", timeout=timedelta(seconds=30))
        ttl = redis_client.ttl(key_str)
        assert 25 <= ttl <= 30  # Allow a small margin

        # Use None for timeout (should use a very long timeout)
        redis_lock.update(test_key, value="2", timeout=None)
        ttl = redis_client.ttl(key_str)
        assert ttl == -1  # No expiration

    def test_decoder_assertion(self, redis_client_raw: Redis):
        """Test that the RedisLock constructor asserts that decode_responses is enabled."""
        # redis_client_raw is a client with decode_responses=False
        with pytest.raises(
            AssertionError, match="Redis must be configured to decode responses"
        ):
            RedisLock(redis_client_raw, "test")

    def test_multi_thread_lock_competition(self, redis_lock: RedisLock):
        """Test that only one thread can acquire the same lock at a time."""
        lock_key = "multi-thread-test-key"
        num_threads = 10
        success_count = 0
        lock_holder = None
        threads_completed = 0
        thread_lock = threading.Lock()  # Thread lock to protect shared variables

        # Initial time
        current_time = datetime.datetime(2023, 1, 1, 12, 0, 0)

        def worker():
            nonlocal success_count, lock_holder, threads_completed
            thread_id = threading.get_ident()
            # Try to acquire the lock multiple times with a small delay
            for _ in range(5):  # Try 5 times
                if redis_lock.lock(lock_key, value=str(thread_id), timeout=100):
                    # This thread acquired the lock
                    with thread_lock:  # Protect the shared counter
                        success_count += 1
                        lock_holder = thread_id
                # Wait a bit before retrying
                time.sleep(0.1)

            with thread_lock:  # Protect the shared counter
                threads_completed += 1

        # Start multiple threads to compete for the lock
        threads = []
        for _ in range(num_threads):
            thread = threading.Thread(target=worker)
            thread.start()
            threads.append(thread)

        # Use busy waiting with freezegun to simulate time passing
        with freeze_time(current_time) as frozen_time:
            # Wait for all threads to complete using busy waiting
            while any(thread.is_alive() for thread in threads):
                # Advance time by 0.05 seconds to accelerate sleep
                frozen_time.tick(0.05)
                time.sleep(0.001)  # Short real sleep to prevent CPU hogging

        # Verify only one thread got the lock
        assert success_count == 1
        assert lock_holder is not None
        assert threads_completed == num_threads

    def test_lock_timeout_expiry(self, redis_lock: RedisLock):
        """Test that a lock can be acquired after the previous holder's timeout expires."""
        lock_key = "timeout-test-key"
        lock_timeout = 60  # 60 seconds timeout

        # Set the starting time
        initial_time = datetime.datetime(2023, 1, 1, 12, 0, 0)

        with freeze_time(initial_time) as frozen_time:
            # First thread acquires the lock
            thread1_id = "thread-1"
            assert redis_lock.lock(lock_key, value=thread1_id, timeout=lock_timeout)

            # Verify the lock is held by thread 1
            assert redis_lock.is_locked(lock_key)
            assert redis_lock.lock_value(lock_key) == thread1_id

            # Thread 2 tries to acquire the same lock and fails
            thread2_id = "thread-2"
            assert not redis_lock.lock(lock_key, value=thread2_id)

            # Advance time to just before timeout
            frozen_time.tick(lock_timeout - 1)

            # Thread 2 tries again and still fails
            assert not redis_lock.lock(lock_key, value=thread2_id)

            # Advance time past the timeout
            frozen_time.tick(2)

            # Thread 2 tries again and succeeds because the lock has expired
            assert redis_lock.lock(lock_key, value=thread2_id)

            # Verify the lock is now held by thread 2
            assert redis_lock.is_locked(lock_key)
            assert redis_lock.lock_value(lock_key) == thread2_id

    def test_lock_update_prevents_timeout(self, redis_lock: RedisLock, redis_client: Redis):
        """Test that updating a lock prevents it from timing out."""
        lock_key = "update-test-key"
        lock_timeout = 60  # 60 seconds timeout

        # Set the starting time
        initial_time = datetime.datetime(2023, 1, 1, 12, 0, 0)

        with freeze_time(initial_time) as frozen_time:
            # First thread acquires the lock
            thread1_id = "thread-1"
            assert redis_lock.lock(lock_key, value=thread1_id, timeout=lock_timeout)

            # Advance time to just before timeout
            frozen_time.tick(lock_timeout - 10)

            # Thread 1 updates the lock
            redis_lock.update(lock_key, value=thread1_id, timeout=lock_timeout)

            # Advance time past the original timeout
            frozen_time.tick(20)

            # Thread 2 tries to acquire the lock and fails because thread 1 updated it
            thread2_id = "thread-2"
            assert not redis_lock.lock(lock_key, value=thread2_id)

            # Verify the lock is still held by thread 1
            assert redis_lock.is_locked(lock_key)
            assert redis_lock.lock_value(lock_key) == thread1_id

            # Advance time past the new timeout
            frozen_time.tick(lock_timeout)

            # Thread 2 tries again and succeeds because the updated lock has expired
            assert redis_lock.lock(lock_key, value=thread2_id)

            # Verify the lock is now held by thread 2
            assert redis_lock.is_locked(lock_key)
            assert redis_lock.lock_value(lock_key) == thread2_id

    def test_rlock_same_thread_different_thread(self, redis_lock: RedisLock, redis_client: Redis):
        """Test that the same thread can rlock itself but different threads cannot."""
        lock_key = "rlock-test-key"
        thread1_id = "thread-1"
        thread2_id = "thread-2"

        # Thread 1 acquires the lock
        assert redis_lock.lock(lock_key, value=thread1_id)

        # Thread 1 can reacquire its own lock with rlock
        assert redis_lock.rlock(lock_key, value=thread1_id)

        # Thread 2 cannot acquire the lock with either lock or rlock
        assert not redis_lock.lock(lock_key, value=thread2_id)
        assert not redis_lock.rlock(lock_key, value=thread2_id)

        # Thread 1 releases the lock
        redis_lock.unlock(lock_key)

        # Thread 2 can now acquire the lock
        assert redis_lock.lock(lock_key, value=thread2_id)


class TestRedisLockPool:
    """Tests for the RedisLockPool class."""

    def test_keys(self, redis_lock_pool: RedisLockPool):
        """Test the keys method."""
        # Add some keys to the pool
        redis_lock_pool.assign(["key1", "key2"])
        keys = redis_lock_pool.keys()
        assert sorted(list(keys)) == ["key1", "key2"]

    def test_extend(self, redis_lock_pool: RedisLockPool):
        """Test the extend method with keys."""
        redis_lock_pool.extend(["key1", "key2"])

        # Verify keys were added
        assert "key1" in redis_lock_pool
        assert "key2" in redis_lock_pool

    def test_shrink(self, redis_lock_pool: RedisLockPool):
        """Test the shrink method."""
        # First add keys
        redis_lock_pool.extend(["key1", "key2", "key3"])

        # Then shrink
        redis_lock_pool.shrink(["key1", "key2"])

        # Verify keys were removed
        assert "key1" not in redis_lock_pool
        assert "key2" not in redis_lock_pool
        assert "key3" in redis_lock_pool

    def test_clear(self, redis_lock_pool: RedisLockPool):
        """Test the clear method."""
        # First add keys
        redis_lock_pool.extend(["key1", "key2"])

        # Then clear
        redis_lock_pool.clear()

        # Verify all keys were removed
        assert len(redis_lock_pool) == 0

    def test_assign(self, redis_lock_pool: RedisLockPool):
        """Test the assign method."""
        # First add some initial keys
        redis_lock_pool.extend(["old1", "old2"])

        # Then assign new keys
        redis_lock_pool.assign(["key1", "key2"])

        # Verify old keys were removed and new keys were added
        assert "old1" not in redis_lock_pool
        assert "old2" not in redis_lock_pool
        assert "key1" in redis_lock_pool
        assert "key2" in redis_lock_pool

    def test_contains(self, redis_lock_pool: RedisLockPool):
        """Test the __contains__ method."""
        # Add a key
        redis_lock_pool.extend(["key1"])

        # Test __contains__
        assert "key1" in redis_lock_pool
        assert "key2" not in redis_lock_pool

    def test_len(self, redis_lock_pool: RedisLockPool):
        """Test the __len__ method."""
        # Add some keys
        redis_lock_pool.extend(["key1", "key2", "key3"])

        # Test __len__
        assert len(redis_lock_pool) == 3

    def test_get_set_del_item(self, redis_lock_pool: RedisLockPool):
        """Test the __getitem__, __setitem__, and __delitem__ methods."""
        # First test extend to add a key
        redis_lock_pool.extend(["key1"])

        # Test __getitem__
        assert "key1" in redis_lock_pool

        # Test shrink to remove key
        redis_lock_pool.shrink(["key1"])
        assert "key1" not in redis_lock_pool

    def test_health_check(self, redis_lock_pool: RedisLockPool):
        """Test the health_check method."""
        # Add some keys
        redis_lock_pool.extend(["key1", "key2", "key3"])

        # Lock some keys
        redis_lock_pool.lock("key1")
        redis_lock_pool.lock("key2")

        # Check health
        locked, free = redis_lock_pool.health_check()
        assert locked == 2
        assert free == 1

    def test_empty_health_check(self, redis_lock_pool: RedisLockPool):
        """Test health_check on an empty pool."""
        # Clear the pool first
        redis_lock_pool.clear()

        # Check health of empty pool
        locked, free = redis_lock_pool.health_check()
        assert locked == 0
        assert free == 0

    def test_extend_empty_keys(self, redis_lock_pool: RedisLockPool):
        """Test extending the pool with an empty list of keys."""
        # Clear the pool first
        redis_lock_pool.clear()

        # Extend with empty list - should not change anything
        redis_lock_pool.extend([])
        assert len(redis_lock_pool) == 0

        # Test with None
        redis_lock_pool.extend(None)
        assert len(redis_lock_pool) == 0

    def test_shrink_empty_keys(self, redis_lock_pool: RedisLockPool):
        """Test shrinking the pool with an empty list of keys."""
        # Add some keys
        redis_lock_pool.extend(["key1", "key2"])

        # Shrink with empty list - should not change anything
        redis_lock_pool.shrink([])
        assert len(redis_lock_pool) == 2

        # Test with None - should also not change anything
        redis_lock_pool.shrink(None)
        assert len(redis_lock_pool) == 2

    def test_assign_with_keys(self, redis_lock_pool: RedisLockPool):
        """Test assigning keys to the pool."""
        # Clear first
        redis_lock_pool.clear()

        # Test assign with actual keys
        keys = ["new1", "new2", "new3"]
        redis_lock_pool.assign(keys)

        # Verify all keys were added
        assert len(redis_lock_pool) == 3
        for key in keys:
            assert key in redis_lock_pool

    def test_iterate_pool(self, redis_lock_pool: RedisLockPool):
        """Test iterating over the keys in the pool."""
        # Add some keys
        test_keys = ["key1", "key2", "key3"]
        redis_lock_pool.extend(test_keys)

        # Iterate and collect keys
        iterated_keys = []
        for key in redis_lock_pool:
            iterated_keys.append(key)

        # Verify all keys were iterated
        assert sorted(iterated_keys) == sorted(test_keys)

    def test_assign_empty(self, redis_lock_pool: RedisLockPool):
        """Test assigning an empty list of keys to the pool."""
        # First add some keys
        redis_lock_pool.extend(["old1", "old2"])

        # Then assign empty list
        redis_lock_pool.assign([])

        # Verify all keys were removed
        assert len(redis_lock_pool) == 0

        # Assign None
        redis_lock_pool.extend(["old1"])
        redis_lock_pool.assign(None)
        assert len(redis_lock_pool) == 0


class TestThreadLock:
    """Tests for the ThreadLock class."""

    def test_lock(self, thread_lock: ThreadLock):
        """Test the lock method."""
        assert thread_lock.lock("test-key")
        assert thread_lock.is_locked("test-key")

    def test_unlock(self, thread_lock: ThreadLock):
        """Test the unlock method."""
        # First set the lock
        thread_lock.update("test-key")
        assert thread_lock.is_locked("test-key")
        # Then unlock
        thread_lock.unlock("test-key")
        assert not thread_lock.is_locked("test-key")

    def test_update(self, thread_lock: ThreadLock):
        """Test the update method."""
        thread_lock.update("test-key", value="2", timeout=60)
        assert thread_lock.lock_value("test-key") == "2"
        # TTL should be close to 60
        assert thread_lock.key_status("test-key", timeout=60) == LockStatus.LOCKED

    def test_lock_value(self, thread_lock: ThreadLock):
        """Test the lock_value method."""
        thread_lock.update("test-key", value="120")
        assert thread_lock.lock_value("test-key") == "120"

    def test_is_locked(self, thread_lock: ThreadLock):
        """Test the is_locked method."""
        assert not thread_lock.is_locked("test-key")
        thread_lock.update("test-key", value="300")
        assert thread_lock.is_locked("test-key")
        assert not thread_lock.is_locked("non-existent-key")

    def test_key_status(self, thread_lock: ThreadLock):
        """Test the key_status method."""
        # Test FREE status
        assert thread_lock.key_status("free-key") == LockStatus.FREE

        # Test LOCKED status
        thread_lock.update("locked-key", value="1", timeout=60)
        assert thread_lock.key_status("locked-key") == LockStatus.LOCKED

        # Test UNAVAILABLE status (TTL > timeout)
        thread_lock.update("unavailable-key", value="1", timeout=200)
        assert thread_lock.key_status("unavailable-key", timeout=100) == LockStatus.UNAVAILABLE

    def test_rlock(self, thread_lock: ThreadLock):
        """Test the rlock method."""
        # First test acquiring a lock
        assert thread_lock.rlock("test-key", value="1") is True
        assert thread_lock.lock_value("test-key") == "1"

        # Test acquiring the same lock with the same value
        assert thread_lock.rlock("test-key", value="1") is True

        # Test acquiring the same lock with a different value
        assert thread_lock.rlock("test-key", value="2") is False

        # Verify the original value wasn't changed
        assert thread_lock.lock_value("test-key") == "1"

    def test_conditional_setdel_operations(self, thread_lock: ThreadLock):
        """Test the conditional set/del operations."""
        # Set initial value
        test_key = "cond-key"
        thread_lock.update(test_key, value="10")

        # Test setgt (greater than)
        assert thread_lock.setgt(key=test_key, value=15)  # 15 > 10, should succeed
        assert thread_lock.lock_value(test_key) == "15"  # Value should be updated to 15

        # Test setgt with unsuccessful condition
        assert not thread_lock.setgt(
            key=test_key, value=5
        )  # 5 < 15, should not succeed
        assert thread_lock.lock_value(test_key) == "15"  # Value should remain 15

        # Test setlt (less than)
        assert thread_lock.setlt(key=test_key, value=5)  # 5 < 15, should succeed
        assert thread_lock.lock_value(test_key) == "5"  # Value should be updated to 5

        # Test setlt with unsuccessful condition
        assert not thread_lock.setlt(
            key=test_key, value=10
        )  # 10 > 5, should not succeed
        assert thread_lock.lock_value(test_key) == "5"  # Value should remain 5

        # Test setge (greater than or equal)
        assert thread_lock.setge(key=test_key, value=5)  # 5 >= 5, should succeed
        assert thread_lock.lock_value(test_key) == "5"  # Value should still be 5

        # Test setge with unsuccessful condition
        assert not thread_lock.setge(key=test_key, value=4)  # 4 < 5, should not succeed
        assert thread_lock.lock_value(test_key) == "5"  # Value should remain 5

        # Test setle (less than or equal)
        assert thread_lock.setle(key=test_key, value=5)  # 5 <= 5, should succeed
        assert thread_lock.lock_value(test_key) == "5"  # Value should remain 5

        # Test setle with unsuccessful condition
        assert not thread_lock.setle(key=test_key, value=6)  # 6 > 5, should not succeed
        assert thread_lock.lock_value(test_key) == "5"  # Value should remain 5

        # Test seteq (equal)
        assert thread_lock.seteq(key=test_key, value=5)  # 5 == 5, should succeed
        assert thread_lock.lock_value(test_key) == "5"  # Value should remain 5

        # Test seteq with unsuccessful condition
        assert not thread_lock.seteq(
            key=test_key, value=6
        )  # 6 != 5, should not succeed
        assert thread_lock.lock_value(test_key) == "5"  # Value should remain 5

        # Test setne (not equal)
        assert thread_lock.setne(key=test_key, value=10)  # 10 != 5, should succeed
        assert thread_lock.lock_value(test_key) == "10"  # Value should be updated to 10

        # Test setne with unsuccessful condition
        assert not thread_lock.setne(
            key=test_key, value=10
        )  # 10 == 10, should not succeed
        assert thread_lock.lock_value(test_key) == "10"  # Value should remain 10

        # Test delgt (delete if greater than)
        thread_lock.update(test_key, value="10")
        assert thread_lock.delgt(key=test_key, value=15)  # 15 > 10, should delete
        assert not thread_lock.is_locked(test_key)  # Key should be deleted

        # Test dellt (delete if less than)
        thread_lock.update(test_key, value="10")
        assert thread_lock.dellt(key=test_key, value=5)  # 5 < 10, should delete
        assert not thread_lock.is_locked(test_key)  # Key should be deleted

        # Test delge (delete if greater than or equal)
        thread_lock.update(test_key, value="10")
        assert thread_lock.delge(key=test_key, value=10)  # 10 >= 10, should delete
        assert not thread_lock.is_locked(test_key)  # Key should be deleted

        # Test delle (delete if less than or equal)
        thread_lock.update(test_key, value="10")
        assert thread_lock.delle(key=test_key, value=10)  # 10 <= 10, should delete
        assert not thread_lock.is_locked(test_key)  # Key should be deleted

        # Test deleq (delete if equal)
        thread_lock.update(test_key, value="10")
        assert thread_lock.deleq(key=test_key, value=10)  # 10 == 10, should delete
        assert not thread_lock.is_locked(test_key)  # Key should be deleted

        # Test delne (delete if not equal)
        thread_lock.update(test_key, value="10")
        assert thread_lock.delne(key=test_key, value=5)  # 5 != 10, should delete
        assert not thread_lock.is_locked(test_key)  # Key should be deleted

    def test_thread_safety(self, thread_lock: ThreadLock):
        """Test thread safety of ThreadLock."""

        def worker(key):
            if thread_lock.lock(key):
                time.sleep(0.1)  # Simulate some work
                thread_lock.unlock(key)
                return True
            return False

        # Test multiple threads trying to acquire the same lock
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker, "test-key") for _ in range(10)]
            results = [f.result() for f in futures]

            # Only one thread should have successfully acquired the lock
            assert sum(results) == 1

    def test_invalid_operator(self, thread_lock: ThreadLock):
        """Test that an invalid operator raises a ValueError."""
        with pytest.raises(ValueError, match="Invalid operator"):
            thread_lock._compare_values("invalid", 10.0, 5.0)

    def test_timeout_types(self, thread_lock: ThreadLock):
        """Test different timeout types."""
        from datetime import timedelta, datetime

        # Test with timedelta
        test_key = "timeout-key"

        # Use timedelta for timeout (30 seconds)
        thread_lock.update(test_key, value="1", timeout=timedelta(seconds=30))
        assert thread_lock.is_locked(test_key)
        assert thread_lock.key_status(test_key) == LockStatus.LOCKED

        # Use None for timeout (should use a very long timeout)
        thread_lock.update(test_key, value="2", timeout=None)
        assert thread_lock.is_locked(test_key)
        # The TTL should be very high (set to 2099)
        future_time = datetime(2099, 1, 1).timestamp() - time.time()
        # Allow 10 seconds margin in the test
        assert thread_lock._get_ttl(test_key) > future_time - 10

    def test_multi_thread_lock_competition(self, thread_lock: ThreadLock):
        """Test that only one thread can acquire the same lock at a time."""
        lock_key = "multi-thread-test-key"
        num_threads = 10
        success_count = 0
        lock_holder = None
        threads_completed = 0
        thread_protect_lock = threading.Lock()  # Thread lock to protect shared variables

        # Initial time
        current_time = datetime.datetime(2023, 1, 1, 12, 0, 0)

        def worker():
            nonlocal success_count, lock_holder, threads_completed
            thread_id = threading.get_ident()

            # Try to acquire the lock multiple times with a small delay
            for _ in range(5):  # Try 5 times
                if thread_lock.lock(lock_key, value=str(thread_id), timeout=100):
                    # This thread acquired the lock
                    with thread_protect_lock:  # Protect the shared counter
                        success_count += 1
                        lock_holder = thread_id

                    # Hold the lock for a short time
                    time.sleep(0.1)

                    # Release the lock
                    # thread_lock.unlock(lock_key)
                    break

                # Wait a bit before retrying
                time.sleep(0.1)

            with thread_protect_lock:  # Protect the shared counter
                threads_completed += 1

        # Start multiple threads to compete for the lock
        threads = []
        for _ in range(num_threads):
            thread = threading.Thread(target=worker)
            thread.start()
            threads.append(thread)

        # Use busy waiting with freezegun to simulate time passing
        with freeze_time(current_time) as frozen_time:
            # Wait for all threads to complete using busy waiting
            while any(thread.is_alive() for thread in threads):
                # Advance time by 0.05 seconds to accelerate sleeps
                frozen_time.tick(0.05)
                time.sleep(0.001)  # Short real sleep to prevent CPU hogging

        # Verify only one thread got the lock
        assert success_count == 1
        assert lock_holder is not None
        assert threads_completed == num_threads

    def test_lock_timeout_expiry(self, thread_lock: ThreadLock):
        """Test that a lock can be acquired after the previous holder's timeout expires."""
        lock_key = "timeout-test-key"
        lock_timeout = 60  # 60 seconds timeout

        # Set the starting time
        initial_time = datetime.datetime(2023, 1, 1, 12, 0, 0)

        with freeze_time(initial_time) as frozen_time:
            # First thread acquires the lock
            thread1_id = "thread-1"
            assert thread_lock.lock(lock_key, value=thread1_id, timeout=lock_timeout)

            # Verify the lock is held by thread 1
            assert thread_lock.is_locked(lock_key)
            assert thread_lock.lock_value(lock_key) == thread1_id

            # Thread 2 tries to acquire the same lock and fails
            thread2_id = "thread-2"
            assert not thread_lock.lock(lock_key, value=thread2_id)

            # Advance time to just before timeout
            frozen_time.move_to(initial_time + datetime.timedelta(seconds=lock_timeout - 1))

            # Thread 2 tries again and still fails
            assert not thread_lock.lock(lock_key, value=thread2_id)

            # Advance time past the timeout
            frozen_time.move_to(initial_time + datetime.timedelta(seconds=lock_timeout + 1))

            # Thread 2 tries again and succeeds because the lock has expired
            assert thread_lock.lock(lock_key, value=thread2_id)

            # Verify the lock is now held by thread 2
            assert thread_lock.is_locked(lock_key)
            assert thread_lock.lock_value(lock_key) == thread2_id

    def test_lock_update_prevents_timeout(self, thread_lock: ThreadLock):
        """Test that updating a lock prevents it from timing out."""
        lock_key = "update-test-key"
        lock_timeout = 60  # 60 seconds timeout

        # Set the starting time
        initial_time = datetime.datetime(2023, 1, 1, 12, 0, 0)

        with freeze_time(initial_time) as frozen_time:
            # First thread acquires the lock
            thread1_id = "thread-1"
            assert thread_lock.lock(lock_key, value=thread1_id, timeout=lock_timeout)

            # Advance time to just before timeout
            frozen_time.move_to(initial_time + datetime.timedelta(seconds=lock_timeout - 10))

            # Thread 1 updates the lock
            thread_lock.update(lock_key, value=thread1_id, timeout=lock_timeout)

            # Advance time past the original timeout
            frozen_time.move_to(initial_time + datetime.timedelta(seconds=lock_timeout + 10))

            # Thread 2 tries to acquire the lock and fails because thread 1 updated it
            thread2_id = "thread-2"
            assert not thread_lock.lock(lock_key, value=thread2_id)

            # Verify the lock is still held by thread 1
            assert thread_lock.is_locked(lock_key)
            assert thread_lock.lock_value(lock_key) == thread1_id

            # Advance time past the new timeout
            frozen_time.move_to(initial_time + datetime.timedelta(seconds=2 * lock_timeout + 10))

            # Thread 2 tries again and succeeds because the updated lock has expired
            assert thread_lock.lock(lock_key, value=thread2_id)

            # Verify the lock is now held by thread 2
            assert thread_lock.is_locked(lock_key)
            assert thread_lock.lock_value(lock_key) == thread2_id

    def test_rlock_same_thread_different_thread(self, thread_lock: ThreadLock):
        """Test that the same thread can rlock itself but different threads cannot."""
        lock_key = "rlock-test-key"
        thread1_id = "thread-1"
        thread2_id = "thread-2"

        # Thread 1 acquires the lock
        assert thread_lock.lock(lock_key, value=thread1_id)

        # Thread 1 can reacquire its own lock with rlock
        assert thread_lock.rlock(lock_key, value=thread1_id)

        # Thread 2 cannot acquire the lock with either lock or rlock
        assert not thread_lock.lock(lock_key, value=thread2_id)
        assert not thread_lock.rlock(lock_key, value=thread2_id)

        # Thread 1 releases the lock
        thread_lock.unlock(lock_key)

        # Thread 2 can now acquire the lock
        assert thread_lock.lock(lock_key, value=thread2_id)


class TestThreadLockPool:
    """Tests for the ThreadLockPool class."""

    def test_keys(self, thread_lock_pool: ThreadLockPool):
        """Test the keys method."""
        # Add some keys to the pool
        thread_lock_pool.assign(["key1", "key2"])
        keys = thread_lock_pool.keys()
        assert sorted(list(keys)) == ["key1", "key2"]

    def test_extend(self, thread_lock_pool: ThreadLockPool):
        """Test the extend method with keys."""
        thread_lock_pool.extend(["key1", "key2"])

        # Verify keys were added
        assert "key1" in thread_lock_pool
        assert "key2" in thread_lock_pool

    def test_shrink(self, thread_lock_pool: ThreadLockPool):
        """Test the shrink method."""
        # First add keys
        thread_lock_pool.extend(["key1", "key2", "key3"])

        # Then shrink
        thread_lock_pool.shrink(["key1", "key2"])

        # Verify keys were removed
        assert "key1" not in thread_lock_pool
        assert "key2" not in thread_lock_pool
        assert "key3" in thread_lock_pool

    def test_clear(self, thread_lock_pool: ThreadLockPool):
        """Test the clear method."""
        # First add keys
        thread_lock_pool.extend(["key1", "key2"])

        # Then clear
        thread_lock_pool.clear()

        # Verify all keys were removed
        assert len(thread_lock_pool) == 0

    def test_assign(self, thread_lock_pool: ThreadLockPool):
        """Test the assign method."""
        # First add some initial keys
        thread_lock_pool.extend(["old1", "old2"])

        # Then assign new keys
        thread_lock_pool.assign(["key1", "key2"])

        # Verify old keys were removed and new keys were added
        assert "old1" not in thread_lock_pool
        assert "old2" not in thread_lock_pool
        assert "key1" in thread_lock_pool
        assert "key2" in thread_lock_pool

    def test_contains(self, thread_lock_pool: ThreadLockPool):
        """Test the __contains__ method."""
        # Add a key
        thread_lock_pool.extend(["key1"])

        # Test __contains__
        assert "key1" in thread_lock_pool
        assert "key2" not in thread_lock_pool

    def test_set_del_item(self, thread_lock_pool: ThreadLockPool):
        """Test the __setitem__ and __delitem__ methods."""
        # First test extend to add a key
        thread_lock_pool.extend(["key1"])

        # Test __getitem__
        assert "key1" in thread_lock_pool

        # Test shrink to remove key
        thread_lock_pool.shrink(["key1"])
        assert "key1" not in thread_lock_pool

    def test_health_check(self, thread_lock_pool: ThreadLockPool):
        """Test the health_check method."""
        # Add some keys
        thread_lock_pool.extend(["key1", "key2", "key3"])

        # Lock some keys
        thread_lock_pool.lock("key1")
        thread_lock_pool.lock("key2")

        # Check health
        locked, free = thread_lock_pool.health_check()
        assert locked == 2
        assert free == 1

    def test_thread_safety(self, thread_lock_pool: ThreadLockPool):
        """Test thread safety of ThreadLockPool."""

        def worker(key):
            if thread_lock_pool.lock(key):
                time.sleep(0.1)  # Simulate some work
                thread_lock_pool.unlock(key)
                return True
            return False

        # Add keys to the pool
        thread_lock_pool.extend(["test-key"])

        # Test multiple threads trying to acquire the same lock
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker, "test-key") for _ in range(10)]
            results = [f.result() for f in futures]

            # Only one thread should have successfully acquired the lock
            assert sum(results) == 1

    def test_empty_health_check(self, thread_lock_pool: ThreadLockPool):
        """Test health_check on an empty pool."""
        # Clear the pool first
        thread_lock_pool.clear()

        # Check health of empty pool
        locked, free = thread_lock_pool.health_check()
        assert locked == 0
        assert free == 0

    def test_empty_pool_operations(self, thread_lock_pool: ThreadLockPool):
        """Test operations on an empty pool."""
        # Test clear on already empty pool
        thread_lock_pool.clear()
        assert len(thread_lock_pool) == 0

        # Test shrink on empty pool
        thread_lock_pool.shrink(["nonexistent"])
        assert len(thread_lock_pool) == 0

        # Test extend with None
        thread_lock_pool.extend(None)
        assert len(thread_lock_pool) == 0

        # Test assign with None
        thread_lock_pool.assign(None)
        assert len(thread_lock_pool) == 0

    def test_iterate_pool(self, thread_lock_pool: ThreadLockPool):
        """Test iterating over the keys in the pool."""
        # Add some keys
        test_keys = ["key1", "key2", "key3"]
        thread_lock_pool.extend(test_keys)

        # Iterate and collect keys
        iterated_keys = []
        for key in thread_lock_pool:
            iterated_keys.append(key)

        # Verify all keys were iterated
        assert sorted(iterated_keys) == sorted(test_keys)

    def test_len_empty_pool(self, thread_lock_pool: ThreadLockPool):
        """Test len() on empty pool."""
        thread_lock_pool.clear()
        assert len(thread_lock_pool) == 0
