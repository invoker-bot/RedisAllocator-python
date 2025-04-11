# flake8: noqa: F401
"""Tests for the Redis-based distributed memory allocation system.

This module tests the functionality of:
1. RedisThreadHealthCheckPool - For thread health monitoring
2. RedisAllocator - For distributed resource allocation
3. RedisAllocatorObject - For managing allocated resources
"""
import pytest
from unittest.mock import MagicMock, patch, call
from redis import RedisError
from redis_allocator.allocator import RedisAllocator, RedisThreadHealthCheckPool, RedisAllocatorObject, RedisAllocatableClass, RedisLockPool


# Use the _TestObject naming to avoid pytest trying to collect it as a test class
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


@pytest.fixture
def test_object():
    """Create a test object implementing RedisAllocatableClass."""
    return _TestObject()


@pytest.fixture
def allocator(redis_client):
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
def shared_allocator(redis_client):
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
def health_checker(redis_client):
    """Create a RedisThreadHealthCheckPool instance for testing."""
    return RedisThreadHealthCheckPool(
        redis_client,
        'test',
        timeout=60
    )


class TestRedisThreadHealthCheckPool:
    """Tests for the RedisThreadHealthCheckPool class."""

    def test_initialization(self, health_checker, redis_client):
        """Test that initialization correctly registers the thread and sets up monitoring."""
        # Initialization should register the current thread
        assert health_checker.current_thread_id is not None
        # Initialization calls update and extend, no need to check Redis calls directly
        # since we're testing the object's behavior, not implementation details
        assert hasattr(health_checker, 'timeout')

    def test_update(self, health_checker, redis_client):
        """Test that update refreshes the thread's health status."""
        # Override the parent class's update method to verify our object behavior
        with patch.object(RedisLockPool, 'update') as mock_update:
            # Call update
            health_checker.update()
            
            # Should call the parent's update method with thread ID and timeout
            mock_update.assert_called_once_with(health_checker.current_thread_id, timeout=health_checker.timeout)

    def test_finalize(self, health_checker, redis_client):
        """Test that finalize cleans up thread resources."""
        # Override the parent class's methods to verify our object behavior
        with patch.object(RedisLockPool, 'shrink') as mock_shrink:
            with patch.object(RedisLockPool, 'unlock') as mock_unlock:
                # Call finalize
                health_checker.finalize()
                
                # Should call shrink with thread ID
                mock_shrink.assert_called_once_with([health_checker.current_thread_id])
                # Should call unlock with thread ID
                mock_unlock.assert_called_once_with(health_checker.current_thread_id)

    def test_custom_timeout(self, redis_client):
        """Test initialization with a custom timeout value."""
        custom_timeout = 120
        checker = RedisThreadHealthCheckPool(redis_client, 'test', timeout=custom_timeout)
        assert checker.timeout == custom_timeout

    def test_multiple_initialize_calls(self, health_checker):
        """Test calling initialize multiple times."""
        with patch.object(RedisLockPool, 'update') as mock_update:
            with patch.object(RedisLockPool, 'extend') as mock_extend:
                # Call initialize again
                health_checker.initialize()
                health_checker.initialize()
                
                # Should have called update and extend each time
                assert mock_update.call_count == 2
                assert mock_extend.call_count == 2


class TestRedisAllocatorObject:
    """Tests for the RedisAllocatorObject class."""
    
    def test_initialization(self, allocator, test_object):
        """Test that initialization correctly sets up the object."""
        # Create a test params dict
        params = {"param1": "value1", "param2": "value2"}
        
        # Create a RedisAllocatorObject
        obj = RedisAllocatorObject(allocator, "test_key", test_object, params)
        
        # Verify properties
        assert obj._allocator == allocator
        assert obj.key == "test_key"
        assert obj.obj == test_object
        assert obj.params == params
        
        # Verify set_config was called on the wrapped object
        assert test_object.config_key == "test_key"
        assert test_object.config_params == params
    
    def test_initialization_with_defaults(self, allocator):
        """Test initialization with default None values."""
        # Create a RedisAllocatorObject with default None values
        obj = RedisAllocatorObject(allocator, "test_key")
        
        # Verify properties
        assert obj._allocator == allocator
        assert obj.key == "test_key"
        assert obj.obj is None
        assert obj.params is None
    
    def test_update(self, allocator, test_object):
        """Test the update method (renamed from lock)."""
        # Create a RedisAllocatorObject
        obj = RedisAllocatorObject(allocator, "test_key", test_object, {})
        
        # Reset mock
        allocator.update = MagicMock()
        
        # Call update with positive timeout
        obj.update(60)
        
        # Verify update was called
        allocator.update.assert_called_once_with("test_key", timeout=60)
    
    def test_update_with_zero_timeout(self, allocator, test_object):
        """Test update with zero timeout, which should free the object."""
        # Create a RedisAllocatorObject
        obj = RedisAllocatorObject(allocator, "test_key", test_object, {})
        
        # Reset mocks
        allocator.update = MagicMock()
        allocator.free = MagicMock()
        
        # Call update with zero timeout
        obj.update(0)
        
        # Verify free was called instead of update
        allocator.update.assert_not_called()
        allocator.free.assert_called_once_with(obj)
    
    def test_close(self, allocator, test_object):
        """Test the close method."""
        # Create a RedisAllocatorObject
        obj = RedisAllocatorObject(allocator, "test_key", test_object, {})
        
        # Call close
        obj.close()
        
        # Verify close was called on the wrapped object
        assert test_object.closed
    
    def test_close_with_none_object(self, allocator):
        """Test the close method with None object."""
        # Create a RedisAllocatorObject with None object
        obj = RedisAllocatorObject(allocator, "test_key")
        
        # Call close should not raise any exception
        obj.close()
    
    def test_del(self, allocator, test_object):
        """Test the __del__ method."""
        # Create a RedisAllocatorObject
        obj = RedisAllocatorObject(allocator, "test_key", test_object, {})
        
        # Patch close method to verify it gets called
        obj.close = MagicMock()
        
        # Simulate __del__ being called
        obj.__del__()
        
        # Verify close was called
        obj.close.assert_called_once()


class TestRedisAllocator:
    """Tests for the RedisAllocator class."""
    
    def test_initialization(self, redis_client):
        """Test the initialization of RedisAllocator."""
        allocator = RedisAllocator(redis_client, 'test', 'alloc-lock')
        
        # Should have an empty WeakValueDictionary for objects
        assert len(allocator.objects) == 0
        # Should be initialized with default values
        assert allocator.shared is False
        # Should have default soft_bind_timeout
        assert allocator.soft_bind_timeout == 3600
    
    def test_initialization_with_custom_values(self, redis_client):
        """Test initialization with custom values."""
        eps = 1e-8
        allocator = RedisAllocator(
            redis_client, 
            'custom_prefix', 
            suffix='custom_suffix',
            eps=eps,
            shared=True
        )
        
        # Should have custom values
        assert allocator.prefix == 'custom_prefix'
        assert allocator.suffix == 'custom_suffix'
        assert allocator.eps == eps
        assert allocator.shared is True
    
    def test_object_key_non_shared(self, allocator, test_object):
        """Test the object_key method in non-shared mode."""
        # In non-shared mode, should return the key as is
        allocator.shared = False
        result = allocator.object_key("test_key", test_object)
        assert result == "test_key"
    
    def test_object_key_shared(self, allocator, test_object):
        """Test the object_key method in shared mode."""
        # In shared mode, should return key:obj
        allocator.shared = True
        result = allocator.object_key("test_key", test_object)
        assert result == f"test_key:{test_object}"
    
    def test_object_key_with_none(self, allocator):
        """Test the object_key method with None object."""
        # With None object, should still work
        allocator.shared = True
        result = allocator.object_key("test_key", None)
        assert result == "test_key:None"
        
        allocator.shared = False
        result = allocator.object_key("test_key", None)
        assert result == "test_key"
    
    def test_extend(self, allocator, redis_client):
        """Test the extend method."""
        # Clear any existing data
        redis_client.flushall()
        
        # Call extend
        allocator.extend(["key4", "key5"])
        
        # Verify keys were added
        assert "key4" in allocator
        assert "key5" in allocator
    
    def test_extend_empty(self, allocator, redis_client):
        """Test extend with empty keys."""
        # Clear any existing data
        redis_client.flushall()
        
        # Call extend with empty list
        allocator.extend([])
        allocator.extend(None)
        
        # No keys should be added
        assert len(list(allocator.keys())) == 0
    
    def test_shrink(self, allocator, redis_client):
        """Test the shrink method."""
        # Clear any existing data
        redis_client.flushall()
        
        # Add some keys first
        allocator.extend(["key1", "key2", "key3"])
        
        # Call shrink
        allocator.shrink(["key1", "key2"])
        
        # Verify keys were removed
        assert "key1" not in allocator
        assert "key2" not in allocator
        assert "key3" in allocator
    
    def test_shrink_empty(self, allocator, redis_client):
        """Test shrink with empty keys."""
        # Clear any existing data
        redis_client.flushall()
        
        # Add some keys first
        allocator.extend(["key1", "key2"])
        
        # Call shrink with empty list
        allocator.shrink([])
        allocator.shrink(None)
        
        # Keys should remain unchanged
        assert "key1" in allocator
        assert "key2" in allocator
    
    def test_assign(self, allocator, redis_client):
        """Test the assign method."""
        # Clear any existing data
        redis_client.flushall()
        
        # Add some initial keys
        allocator.extend(["key1", "key2"])
        
        # Call assign with new keys
        allocator.assign(["key3", "key4"])
        
        # Verify old keys are gone and new keys are present
        assert "key1" not in allocator
        assert "key2" not in allocator
        assert "key3" in allocator
        assert "key4" in allocator
        
        # Call assign with None
        allocator.assign(None)
        
        # All keys should be gone
        assert len(list(allocator.keys())) == 0
    
    def test_assign_empty(self, allocator, redis_client):
        """Test assign with empty keys."""
        # Clear any existing data
        redis_client.flushall()
        
        # Add some initial keys
        allocator.extend(["key1", "key2"])
        
        # Call assign with empty list
        allocator.assign([])
        
        # All keys should be gone
        assert len(list(allocator.keys())) == 0
    
    def test_clear(self, allocator, redis_client):
        """Test the clear method."""
        # Clear any existing data
        redis_client.flushall()
        
        # Add some keys
        allocator.extend(["key1", "key2"])
        
        # Call clear
        allocator.clear()
        
        # All keys should be gone
        assert len(list(allocator.keys())) == 0
    
    def test_redis_error_in_clear(self, allocator, redis_client):
        """Test handling Redis errors in clear."""
        # Clear any existing data
        redis_client.flushall()
        
        # Add some keys
        allocator.extend(["key1", "key2"])
        
        # Mock Redis error
        redis_client.delete = lambda *args: (_ for _ in ()).throw(RedisError("Test error"))
        
        # Call clear should raise RedisError
        with pytest.raises(RedisError):
            allocator.clear()
    
    def test_keys(self, allocator, redis_client):
        """Test the keys method."""
        # Clear any existing data
        redis_client.flushall()
        
        # Add some keys
        allocator.extend(["key1", "key2", "key3"])
        
        # Get keys
        result = list(allocator.keys())
        
        # Verify we got all keys
        assert set(result) == {"key1", "key2", "key3"}
    
    def test_redis_error_in_keys(self, allocator, redis_client):
        """Test handling Redis errors in keys."""
        # Clear any existing data
        redis_client.flushall()
        
        # Add some keys
        allocator.extend(["key1", "key2"])
        
        # Mock Redis error
        redis_client.hkeys = lambda *args: (_ for _ in ()).throw(RedisError("Test error"))
        
        # Getting keys should raise RedisError
        with pytest.raises(RedisError):
            list(allocator.keys())
    
    def test_contains(self, allocator, redis_client):
        """Test the __contains__ method."""
        # Clear any existing data
        redis_client.flushall()
        
        # Add some keys
        allocator.extend(["key1", "key2"])
        
        # Check containment
        assert "key1" in allocator
        assert "key2" in allocator
        assert "key3" not in allocator
    
    def test_redis_error_in_contains(self, allocator, redis_client):
        """Test handling Redis errors in __contains__."""
        # Clear any existing data
        redis_client.flushall()
        
        # Add some keys
        allocator.extend(["key1", "key2"])
        
        # Mock Redis error
        redis_client.hexists = lambda *args: (_ for _ in ()).throw(RedisError("Test error"))
        
        # Checking containment should raise RedisError
        with pytest.raises(RedisError):
            "key1" in allocator
    
    def test_update_soft_bind(self, allocator, redis_client):
        """Test the update_soft_bind method."""
        # Set up mock
        allocator.update = MagicMock()
        
        # Call update_soft_bind
        allocator.update_soft_bind("test_name", "test_key")
        
        # Verify update was called with the right parameters
        allocator.update.assert_called_once_with(
            allocator._soft_bind_name("test_name"), 
            "test_key", 
            timeout=allocator.soft_bind_timeout
        )
    
    def test_unbind_soft_bind(self, allocator, redis_client):
        """Test the unbind_soft_bind method."""
        # Set up mock
        allocator.unlock = MagicMock()
        
        # Call unbind_soft_bind
        allocator.unbind_soft_bind("test_name")
        
        # Verify unlock was called with the right parameter
        allocator.unlock.assert_called_once_with(allocator._soft_bind_name("test_name"))
    
    def test_soft_bind_with_empty_name(self, allocator):
        """Test soft bind methods with empty name."""
        # Set up mocks
        allocator.update = MagicMock()
        allocator.unlock = MagicMock()
        
        # Call methods with empty name
        allocator.update_soft_bind("", "test_key")
        allocator.unbind_soft_bind("")
        
        # Should still call the underlying methods with empty string
        allocator.update.assert_called_once()
        allocator.unlock.assert_called_once()
        
        # The soft bind name should be generated even with empty string
        assert allocator._soft_bind_name("") != ""
    
    def test_shared_vs_non_shared_allocation(self, allocator, shared_allocator):
        """Test difference between shared and non-shared allocation."""
        # Set up mocks for both allocators
        allocator._malloc_script = MagicMock(return_value="key1")
        shared_allocator._malloc_script = MagicMock(return_value="key1")
        
        # Check the malloc_lua_script property for both allocators
        non_shared_script = allocator._malloc_lua_script
        shared_script = shared_allocator._malloc_lua_script
        
        # The scripts should be different, with shared=0 in non-shared and shared=1 in shared
        assert "local shared = 0" in non_shared_script
        assert "local shared = 1" in shared_script
        
        # Both can allocate the same key, but behavior should differ
        assert allocator.malloc_key() == "key1"
        assert shared_allocator.malloc_key() == "key1"
