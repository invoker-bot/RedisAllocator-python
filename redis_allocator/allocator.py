"""Redis-based distributed memory allocation system.

This module provides the core functionality of the RedisAllocator system, 
allowing for distributed memory allocation with support for garbage collection,
thread health checking, and priority-based allocation mechanisms.
"""
import logging
import weakref
from abc import ABC, abstractmethod
from functools import cached_property
from threading import current_thread
from typing import (Optional, TypeVar, Generic,
                    Sequence, Iterable)
from redis import StrictRedis as Redis
from .lock import RedisLockPool, Timeout

logger = logging.getLogger(__name__)


class RedisAllocatableClass(ABC):
    @abstractmethod
    def set_config(self, key: str, params: dict):
        """Set the configuration for the object.

        Args:
            key: The key to set the configuration for.
            params: The parameters to set the configuration for.
        """
        pass

    def close(self):
        """close the object."""
        pass

    def name(self) -> Optional[str]:
        """Get the cache name of the object."""
        return None


U = TypeVar('U', bound=RedisAllocatableClass)


class RedisThreadHealthCheckPool(RedisLockPool):
    """A class that provides a simple interface for managing the health status of a thread.

    This class enables tracking the health status of threads in a distributed environment
    using Redis locks.
    """

    def __init__(self, redis: Redis, identity: str, timeout: int):
        """Initialize a RedisThreadHealthCheckPool instance.

        Args:
            redis: The Redis client used for interacting with Redis.
            identity: The identity prefix for the health checker.
            timeout: The timeout for health checks in seconds.
            tasks: A list of thread identifiers to track.
        """
        super().__init__(redis, identity, "thread-health-check-pool")
        self.timeout = timeout
        self.initialize()

    @property
    def current_thread_id(self) -> str:
        """Get the current thread ID.

        Returns:
            The current thread ID as a string.
        """
        return str(current_thread().ident)

    def initialize(self):
        """Initialize the health status."""
        self.update()
        self.extend([self.current_thread_id])

    def update(self):  # pylint: disable=arguments-differ
        """Update the health status."""
        super().update(self.current_thread_id, timeout=self.timeout)

    def finalize(self):
        """Finalize the health status."""
        self.shrink([self.current_thread_id])
        self.unlock(self.current_thread_id)


class RedisAllocatorObject(Generic[U]):
    """Represents an object allocated through RedisAllocator.

    This class provides an interface for working with allocated objects
    including locking and unlocking mechanisms for thread-safe operations.

    """
    _allocator: 'RedisAllocator'  # Reference to the allocator that created this object
    key: str                      # Redis key for this allocated object
    params: Optional[dict]        # Parameters associated with this object
    obj: Optional[U]              # The actual object being allocated

    def __init__(self, allocator: 'RedisAllocator', key: str, obj: Optional[U] = None, params: Optional[dict] = None):
        """Initialize a RedisAllocatorObject instance.

        Args:
            allocator: The RedisAllocator that created this object
            key: The Redis key for this allocated object
            obj: The actual object being allocated
            params: Additional parameters passed by local program
        """
        self._allocator = allocator
        self.key = key
        self.obj = obj
        self.params = params
        if self.obj is not None:
            self.obj.set_config(key, params)

    def update(self, timeout: Timeout = 120):
        """Lock this object for exclusive access.

        Args:
            timeout: How long the lock should be valid (in seconds)
        """
        if timeout > 0:
            self._allocator.update(self.key, timeout=timeout)
        else:
            self._allocator.free(self)

    def close(self):
        """Kill the object."""
        if self.obj is not None:
            self.obj.close()

    def __del__(self):
        """Delete the object."""
        self.close()


class RedisAllocator(RedisLockPool, Generic[U]):
    """A Redis-based distributed memory allocation system.
    
    This class implements a memory allocation interface using Redis as the backend store.
    It manages a pool of resources that can be allocated, freed, and garbage collected.
    The implementation uses a doubly-linked list structure stored in Redis hashes to
    track available and allocated resources.
    
    Generic type U must implement the RedisContextableObject protocol, allowing
    allocated objects to be used as context managers.
    """

    def __init__(self, redis: Redis, prefix: str, suffix='allocator', eps=1e-6,
                 shared=False):
        """Initialize a RedisAllocator instance.
        
        Args:
            redis: Redis client for communication with Redis server
            prefix: Prefix for all Redis keys used by this allocator
            suffix: Suffix for Redis keys to uniquely identify this allocator
            eps: Small value for floating point comparisons
            shared: Whether resources can be shared across multiple consumers
        """
        super().__init__(redis, prefix, suffix=suffix, eps=eps)
        self.shared = shared
        self.soft_bind_timeout = 3600  # Default timeout for soft bindings (1 hour)
        self.objects: weakref.WeakValueDictionary[str, RedisAllocatorObject] = weakref.WeakValueDictionary()

    def object_key(self, key: str, obj: U):
        """Get the key for an object."""
        if not self.shared:
            return key
        return f'{key}:{obj}'

    def _pool_pointer_str(self, head: bool = True):
        """Get the Redis key for the head or tail pointer of the allocation pool.
        
        Args:
            head: If True, get the head pointer key; otherwise, get the tail pointer key
            
        Returns:
            String representation of the Redis key for the pointer
        """
        pointer_type = 'head' if head else 'tail'
        return f'{self._pool_str()}|{pointer_type}'

    def _gc_cursor_str(self):
        """Get the Redis key for the garbage collection cursor.
        
        Returns:
            String representation of the Redis key for the GC cursor
        """
        return f'{self._pool_str()}|gc_cursor'

    @property
    def _lua_required_string(self):
        """LUA script containing helper functions for Redis operations.
        
        This script defines various LUA functions for manipulating the doubly-linked list
        structure that represents the allocation pool:
        - pool_pointer_str: Get Redis keys for head/tail pointers
        - gc_cursor_str: Get Redis key for GC cursor
        - push_to_tail: Add an item to the tail of the linked list
        - pop_from_head: Remove and return the item at the head of the linked list
        - delete_item: Remove an item from anywhere in the linked list
        """
        return f'''
        {super()._lua_required_string}
        local function pool_pointer_str(head)
            local pointer_type = 'head'
            if not head then
                pointer_type = 'tail'
            end
            return '{self._pool_str()}|' .. pointer_type
        end
        local function gc_cursor_str()
            return '{self._pool_str()}|gc_cursor'
        end
        local poolItemsKey = pool_str()
        local headKey      = pool_pointer_str(true)
        local tailKey      = pool_pointer_str(false)
        local function push_to_tail(itemName)
            local tail         = redis.call("GET", tailKey)
            if not tail then
                tail = ""
            end
            redis.call("HSET", poolItemsKey, itemName, tostring(tail) .. "||")
            if tail == "" then
                redis.call("SET", headKey, itemName)
            else
                local tailVal = redis.call("HGET", poolItemsKey, tail)
                local prev, _ = string.match(tailVal, "(.*)||(.*)")
                redis.call("HSET", poolItemsKey, tail, tostring(prev) .. "||" .. tostring(itemName))
            end
            redis.call("SET", tailKey, itemName)
        end
        local function pop_from_head()
            local head = redis.call("GET", headKey)
            if head == nil or head == "" then
                return nil
            end
            local headVal = redis.call("HGET", poolItemsKey, head)
            if headVal == nil then
                redis.call("SET", headKey, "")
                redis.call("SET", tailKey, "")
                return nil
            else
                assert(headVal ~= "#ALLOCATED", "head is allocated")
            end
            local _, next = string.match(headVal, "(.*)||(.*)")
            if next == "" then
                redis.call("SET", headKey, "")
                redis.call("SET", tailKey, "")
            else
                local nextVal = redis.call("HGET", poolItemsKey, next)
                local prev, _ = string.match(nextVal, "(.*)||(.*)")
                redis.call("HSET", poolItemsKey, next, tostring("") .. "||" .. tostring(prev))
                redis.call("SET", headKey, next)
            end
            return head
        end
        local function delete_item(itemName)
            local val = redis.call("HGET", poolItemsKey, itemName)
            if val ~= '#ALLOCATED' then
                local prev, next = string.match(val, "(.*)||(.*)")
                if prev ~= "" then
                    local prevVal = redis.call("HGET", poolItemsKey, prev)
                    if prevVal then
                        local p, _ = string.match(prevVal, "(.*)||(.*)")
                        redis.call("HSET", poolItemsKey, prev, tostring(p) .. "||" .. tostring(next))
                    end
                else
                    redis.call("SET", headKey, next or "")
                end
                if next ~= "" then
                    local nxtVal = redis.call("HGET", poolItemsKey, next)
                    if nxtVal then
                        local _, n = string.match(nxtVal, "(.*)||(.*)")
                        redis.call("HSET", poolItemsKey, next, tostring(prev) .. "||" .. tostring(n))
                    end
                else
                    redis.call("SET", tailKey, prev or "")
                end
            end
            redis.call("HDEL", poolItemsKey, itemName)
        end
        '''

    @property
    def _extend_lua_string(self):
        """LUA script for extending the allocation pool with new resources.
        
        This script adds new items to the pool if they don't already exist.
        New items are added to the tail of the linked list.
        """
        return f'''{self._lua_required_string}
        for _, itemName in ipairs(ARGV) do
            if redis.call("HEXISTS", poolItemsKey, itemName) == 1 then
                -- todo: gc check
            else
                push_to_tail(itemName)
            end
        end
        '''

    @cached_property
    def _extend_script(self):
        """Cached Redis script for extending the allocation pool."""
        return self.redis.register_script(self._extend_lua_string)

    def extend(self, keys: Optional[Sequence[str]] = None):
        """Add new resources to the allocation pool.
        
        Args:
            keys: Sequence of resource identifiers to add to the pool
        """
        if keys is not None and len(keys) > 0:
            self._extend_script(args=keys)

    @property
    def _shrink_lua_string(self):
        """LUA script for removing resources from the allocation pool.
        
        This script removes specified items from the pool by deleting them
        from the linked list structure.
        """
        return f'''{self._lua_required_string}
        for _, itemName in ipairs(ARGV) do
            delete_item(itemName)
        end
        '''

    @cached_property
    def _shrink_script(self):
        """Cached Redis script for shrinking the allocation pool."""
        return self.redis.register_script(self._shrink_lua_string)

    def shrink(self, keys: Optional[Sequence[str]] = None):
        """Remove resources from the allocation pool.
        
        Args:
            keys: Sequence of resource identifiers to remove from the pool
        """
        if keys is not None and len(keys) > 0:
            self._shrink_script(args=keys)

    @property
    def _assign_lua_string(self):
        """LUA script for completely replacing the resources in the allocation pool.
        
        This script clears the existing pool and replaces it with a new set of resources.
        Items not in the new set are removed, and items in the new set but not in the
        existing pool are added to the tail of the linked list.
        """
        return f'''{self._lua_required_string}
        local wantSet  = {{}}
        for _, k in ipairs(ARGV) do
            wantSet[k] = true
        end
        local allItems = redis.call("HKEYS", poolItemsKey)
        for _, itemName in ipairs(allItems) do
            if not wantSet[itemName] then
                delete_item(itemName)
            else
                wantSet[itemName] = nil
            end
        end
        for k, v in pairs(wantSet) do
            if v then
                push_to_tail(k)
            end
        end
        '''

    @cached_property
    def _assign_lua_script(self):
        """Cached Redis script for assigning resources to the allocation pool."""
        return self.redis.register_script(self._assign_lua_string)

    def assign(self, keys: Optional[Sequence[str]] = None):
        """Completely replace the resources in the allocation pool.
        
        Args:
            keys: Sequence of resource identifiers to assign to the pool,
                 replacing any existing resources
        """
        if keys is not None and len(keys) > 0:
            self._assign_lua_script(args=keys)
        else:
            self.clear()

    def keys(self) -> Iterable[str]:
        """Get all resource identifiers in the allocation pool.
        
        Returns:
            Iterable of resource identifiers in the pool
        """
        return self.redis.hkeys(self._pool_str())

    def __contains__(self, key):
        """Check if a resource identifier is in the allocation pool.
        
        Args:
            key: Resource identifier to check
            
        Returns:
            True if the resource is in the pool, False otherwise
        """
        return self.redis.hexists(self._pool_str(), key)

    @property
    def _cache_str(self):
        """Get the Redis key for the allocator's cache.
        
        Returns:
            String representation of the Redis key for the cache
        """
        return f'{self.prefix}|{self.suffix}-cache'

    def clear(self):
        """Clear all resources from the allocation pool and cache."""
        super().clear()
        self.redis.delete(self._cache_str)

    def _soft_bind_name(self, name: str) -> str:
        """Get the Redis key for a soft binding.
        
        Args:
            name: Name of the soft binding
            
        Returns:
            String representation of the Redis key for the soft binding
        """
        return f"{self._cache_str}:bind:{name}"

    def update_soft_bind(self, name: str, key: str):
        """Update a soft binding between a name and a resource.
        
        Args:
            name: Name to bind
            key: Resource identifier to bind to the name
        """
        self.update(self._soft_bind_name(name), key, timeout=self.soft_bind_timeout)

    def unbind_soft_bind(self, name: str):
        """Remove a soft binding.
        
        Args:
            name: Name of the soft binding to remove
        """
        self.unlock(self._soft_bind_name(name))

    @property
    def _malloc_lua_script(self):
        """LUA script for allocating a resource from the pool.
        
        This script allocates a resource by popping an item from the head
        of the linked list and marking it as allocated. If the allocator
        is not shared, the script also sets a lock on the allocated resource.
        """
        return f'''
        {self._lua_required_string}
        local shared = {1 if self.shared else 0}
        local timeout = tonumber(ARGV[1])
        local itemName = pop_from_head()
        if itemName ~= nil then
            if redis.call("EXISTS", key_str(itemName)) == 1 then
                itemName = nil
            else
                if not shared then
                    if timeout ~= nil then
                        redis.call("SET", key_str(itemName), "1", "EX", timeout)
                    else
                        redis.call("SET", key_str(itemName), "1")
                    end
                end
            end
        end
        return itemName
        '''

    @cached_property
    def _malloc_script(self):
        """Cached Redis script for allocating a resource."""
        return self.redis.register_script(self._malloc_lua_script)

    def malloc_key(self, timeout: Timeout = 120) -> Optional[str]:
        """Allocate a resource key from the pool.
        
        Args:
            timeout: How long the allocation should be valid (in seconds)
            
        Returns:
            Resource identifier if allocation was successful, None otherwise
        """
        return self._malloc_script(keys=[self._to_seconds(timeout)])

    def malloc(self, timeout: Timeout = 120, obj: Optional[U] = None, params: Optional[dict] = None) -> Optional[RedisAllocatorObject[U]]:
        """Allocate a resource from the pool and wrap it in a RedisAllocatorObject.
        
        Args:
            timeout: How long the allocation should be valid (in seconds)
            obj: The object to wrap in the RedisAllocatorObject
            params: Additional parameters to associate with the allocated object
            
        Returns:
            RedisAllocatorObject wrapping the allocated resource if successful, None otherwise
        """
        key = self.malloc_key(timeout)
        if key is None:
            return None
        return RedisAllocatorObject(self, key, obj, params)

    @property
    def _free_lua_script(self):
        """LUA script for freeing allocated resources.
        
        This script frees allocated resources by removing their locks
        and pushing them back to the tail of the linked list.
        """
        return f'''
        {self._lua_required_string}
        for _, k in ipairs(KEYS) do
            redis.call('DEL', key_str(k))
            push_to_tail(k)
        end
        '''

    @cached_property
    def _free_script(self):
        """Cached Redis script for freeing allocated resources."""
        return self.redis.register_script(self._free_lua_script)

    def free_keys(self, *keys: str):
        """Free allocated resources.
        
        Args:
            *keys: Resource identifiers to free
        """
        self._free_script(keys, args=keys)

    def free(self, obj: RedisAllocatorObject[U]):
        """Free an allocated object."""
        self.free_keys(obj.key)

    @cached_property
    def _gc_script(self):
        """Cached Redis script for garbage collection."""
        return self.redis.register_script(self._gc_lua_script)

    @property
    def _gc_lua_script(self):
        """LUA script for garbage collection of the allocation pool.
        
        This script scans through the pool and performs two types of cleanup:
        1. Resources marked as allocated but not actually locked are pushed back
           to the available pool
        2. Resources not marked as allocated but actually locked are marked as allocated
        
        This ensures consistency between the allocation metadata and the actual locks.
        """
        return f'''
        {self._lua_required_string}
        local n = tonumber(ARGV[1])
        local cursorKey = gc_cursor_str()
        local oldCursor = redis.call("GET", cursorKey)
        if not oldCursor or oldCursor == "" then
            oldCursor = "0"
        end
        local scanResult = redis.call("HSCAN", poolItemsKey, oldCursor, "COUNT", n)
        local newCursor  = scanResult[1]
        local kvList     = scanResult[2]
        local tail       = redis.call("GET", tailKey)
        if not tail then
            tail = ""
        end
        for i = 1, #kvList, 2 do
            local itemName = kvList[i]
            local val      = kvList[i + 1]
            if val == "#ALLOCATED" then
                local locked = (redis.call("EXISTS", key_str(itemName)) == 1)
                if not locked then
                    push_to_tail(itemName)
                end
            else
                local locked = (redis.call("EXISTS", key_str(itemName)) == 1)
                if locked then
                    delete_item(itemName)
                    redis.call("HSET", poolItemsKey, itemName, "#ALLOCATED")
                end
            end
        end
        '''

    def gc(self, count: int = 10):
        """Perform garbage collection on the allocation pool.
        
        This method scans through the pool and ensures consistency between
        the allocation metadata and the actual locks.
        
        Args:
            count: Number of items to check in this garbage collection pass
        """
        # Ensure count is positive
        assert count > 0, "count should be positive"
        self._gc_script(args=[count])
