"""Redis-based distributed memory allocation system.

This module provides the core functionality of the RedisAllocator system,
allowing for distributed memory allocation with support for garbage collection,
thread health checking, and priority-based allocation mechanisms.

Key features:
1. Shared vs non-shared allocation modes:
   - In shared mode, allocating an item simply removes it from the free list and puts it back to the tail
   - In non-shared mode, allocation locks the item to prevent others from accessing it
2. Garbage collection for stale/unhealthy items:
   - Items that are locked (unhealthy) but in the free list are removed
   - Items that are not in the free list but haven't been updated within their timeout are freed
3. Soft binding mechanism:
   - Maps object names to allocated keys for consistent allocation
   - Prioritizes previously allocated keys when the same named object requests allocation
4. Support for an updater to refresh the pool's keys periodically
5. Policy-based control of allocation behavior through RedisAllocatorPolicy
"""
import logging
import weakref
from abc import ABC, abstractmethod
from typing import Any
from functools import cached_property
from threading import current_thread
from typing import (Optional, TypeVar, Generic,
                    Sequence, Iterable)
from redis import StrictRedis as Redis
from .lock import RedisLockPool, Timeout

logger = logging.getLogger(__name__)


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


class RedisAllocatableClass(ABC):
    """A class that can be allocated through RedisAllocator.

    You should inherit from this class and implement the set_config method.
    """

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

    def is_healthy(self):
        return True

    @property
    def name(self) -> Optional[str]:
        """Get the cache name of the object, if is none no soft binding will be used."""
        return None


U = TypeVar('U', bound=RedisAllocatableClass)


class RedisAllocatorObject(Generic[U]):
    """Represents an object allocated through RedisAllocator.

    This class provides an interface for working with allocated objects
    including locking and unlocking mechanisms for thread-safe operations.
    """
    allocator: 'RedisAllocator'  # Reference to the allocator that created this object
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
        self.allocator = allocator
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
            self.allocator.update(self.key, timeout=timeout)
        else:
            self.allocator.free(self)

    def close(self):
        """Kill the object."""
        if self.obj is not None:
            self.obj.close()

    # def refresh(self):
    #     """Refresh the object."""

    def __del__(self):
        """Delete the object."""
        self.close()


class RedisAllocatorUpdater:
    """A class that updates the allocator keys."""

    def __init__(self, params: Sequence[Any]):
        """Initialize the allocator updater."""
        assert len(params) > 0, "params should not be empty"
        self.params = params
        self.index = 0

    @abstractmethod
    def fetch(self, param: Any) -> Sequence[str]:
        """Fetch the keys from params."""
        pass

    def __call__(self):
        """Update the allocator key."""
        current_param = self.params[self.index]
        self.index = (self.index + 1) % len(self.params)
        keys = self.fetch(current_param)
        return keys

    def __len__(self):
        """Get the length of the allocator updater."""
        return len(self.params)


class RedisAllocatorPolicy(ABC):
    """Abstract base class for Redis allocator policies.

    This class defines the interface for allocation policies that can be used
    with RedisAllocator to control allocation behavior.
    """

    def initialize(self, allocator: 'RedisAllocator'):
        """Initialize the policy with an allocator instance.

        Args:
            allocator: The RedisAllocator instance to use with this policy
        """
        pass

    @abstractmethod
    def malloc(self, allocator: 'RedisAllocator', timeout: Timeout = 120,
               obj: Optional[Any] = None, params: Optional[dict] = None) -> Optional[RedisAllocatorObject]:
        """Allocate a resource according to the policy.

        Args:
            allocator: The RedisAllocator instance
            timeout: How long the allocation should be valid (in seconds)
            obj: The object to associate with the allocation
            params: Additional parameters for the allocation

        Returns:
            RedisAllocatorObject if allocation was successful, None otherwise
        """
        pass

    @abstractmethod
    def refresh_pool(self, allocator: 'RedisAllocator'):
        """Refresh the allocation pool.

        This method is called periodically to update the pool with new resources.

        Args:
            allocator: The RedisAllocator instance
        """
        pass


class DefaultRedisAllocatorPolicy(RedisAllocatorPolicy):
    """Default implementation of RedisAllocatorPolicy.

    This policy provides the following features:
    1. Garbage collection before allocation: Automatically performs garbage collection
       operations before allocating resources to ensure stale resources are reclaimed.

    2. Soft binding prioritization: Prioritizes allocation of previously bound keys
       for named objects, creating a consistent mapping between object names and keys.
       If a soft binding exists but the bound key is no longer in the pool, the binding is
       ignored and a new key is allocated.

    3. Periodic pool updates: Uses an optional updater to refresh the pool's keys at
       configurable intervals. Only one process/thread (the one that acquires the update lock)
       will perform the update.

    4. Configurable expiry times: Allows setting default expiry durations for pool items,
       ensuring automatic cleanup of stale resources even without explicit garbage collection.

    The policy controls when garbage collection happens, when the pool is refreshed with new keys,
    and how allocation prioritizes resources.
    """

    def __init__(self, gc_count: int = 5, update_interval: int = 300,
                 expiry_duration: int = -1, updater: Optional[RedisAllocatorUpdater] = None):
        """Initialize the default allocation policy.

        Args:
            gc_count: Number of GC operations to perform before allocation
            update_interval: Interval in seconds between pool updates
            expiry_duration: Default timeout for pool items (-1 means no timeout)
            updater: Optional updater for refreshing the pool's keys
        """
        self.gc_count = gc_count
        self.update_interval: float = update_interval
        self.expiry_duration: float = expiry_duration
        self.updater = updater
        self._allocator: Optional['RedisAllocator'] = None
        self._update_lock_key: Optional[str] = None

    def initialize(self, allocator: 'RedisAllocator'):
        """Initialize the policy with an allocator instance.

        Args:
            allocator: The RedisAllocator instance to use with this policy
        """
        self._allocator = weakref.ref(allocator)
        self._update_lock_key = f"{allocator._pool_str()}|policy_update_lock"

    def malloc(self, allocator: 'RedisAllocator', timeout: Timeout = 120,
               obj: Optional[Any] = None, params: Optional[dict] = None) -> Optional[RedisAllocatorObject]:
        """Allocate a resource according to the policy.

        This implementation:
        1. Performs GC operations before allocation
        2. Checks for soft binding based on object name
        3. Falls back to regular allocation if no soft binding exists

        Args:
            allocator: The RedisAllocator instance
            timeout: How long the allocation should be valid (in seconds)
            obj: The object to associate with the allocation
            params: Additional parameters for the allocation

        Returns:
            RedisAllocatorObject if allocation was successful, None otherwise
        """
        # Try to refresh the pool if necessary
        self._try_refresh_pool(allocator)

        # Perform GC operations before allocation
        allocator.gc(self.gc_count)

        # Fall back to regular allocation
        key = allocator.malloc_key(timeout, obj.name if obj else None)
        return RedisAllocatorObject(allocator, key, obj, params)

    def _try_refresh_pool(self, allocator: 'RedisAllocator'):
        """Try to refresh the pool if necessary and if we can acquire the lock.

        Args:
            allocator: The RedisAllocator instance
        """
        if self.updater is None:
            return
        if allocator.lock(self._update_lock_key, timeout=self.update_interval):
            # If we got here, we acquired the lock, so we can update the pool
            self.refresh_pool(allocator)

    def refresh_pool(self, allocator: 'RedisAllocator'):
        """Refresh the allocation pool using the updater.

        Args:
            allocator: The RedisAllocator instance
        """
        if self.updater is None:
            return

        keys = self.updater()

        if len(keys) == 0:
            logger.warning("No keys to update to the pool")
            return

        # Update the pool based on the number of keys
        if len(self.updater) == 1:
            allocator.assign(keys, timeout=self.expiry_duration)
        else:
            allocator.extend(keys, timeout=self.expiry_duration)


class RedisAllocator(RedisLockPool, Generic[U]):
    """A Redis-based distributed memory allocation system.

    This class implements a memory allocation interface using Redis as the backend store.
    It manages a pool of resources that can be allocated, freed, and garbage collected.
    The implementation uses a doubly-linked list structure stored in Redis hashes to
    track available and allocated resources.

    Allocation Modes:
    - Shared mode (shared=True): Resources can be shared across multiple clients. When allocated,
      items are removed from the free list head and placed back on the tail without locking.
      This allows multiple clients to access the same resource concurrently.
    - Non-shared mode (shared=False): Resources are locked when allocated, preventing other
      clients from accessing them until they are freed or their lock times out.

    Soft Binding:
    Objects implementing RedisAllocatableClass with a non-None name property can use soft binding,
    which creates a mapping between the object name and an allocated key. Subsequent allocation
    requests with the same named object will try to reuse the previously allocated key,
    providing consistent resource allocation for the same logical entity.

    Garbage Collection:
    The allocator includes a garbage collection mechanism that:
    1. Removes items from the free list that are locked (unhealthy)
    2. Returns items to the free list if their locks have expired
    3. Removes items with explicit expiry times that have elapsed

    Allocation Policies:
    The allocator supports customizable allocation policies through the RedisAllocatorPolicy
    interface, allowing for custom allocation strategies, automatic pool updates, and
    garbage collection scheduling.

    Generic type U must implement the RedisContextableObject protocol, allowing
    allocated objects to be used as context managers.
    """

    def __init__(self, redis: Redis, prefix: str, suffix='allocator', eps=1e-6,
                 shared=False, policy: Optional[RedisAllocatorPolicy] = None):
        """Initialize a RedisAllocator instance.

        Args:
            redis: Redis client for communication with Redis server
            prefix: Prefix for all Redis keys used by this allocator
            suffix: Suffix for Redis keys to uniquely identify this allocator
            eps: Small value for floating point comparisons
            shared: Whether resources can be shared across multiple consumers
            policy: Optional allocation policy to control allocation behavior
        """
        super().__init__(redis, prefix, suffix=suffix, eps=eps)
        self.shared = shared
        self.soft_bind_timeout = 3600  # Default timeout for soft bindings (1 hour)
        self.objects: weakref.WeakValueDictionary[str, RedisAllocatorObject] = weakref.WeakValueDictionary()
        self.policy = policy or DefaultRedisAllocatorPolicy()
        self.policy.initialize(self)

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

    @property
    def _lua_required_string(self):
        """LUA script containing helper functions for Redis operations.

        This script defines various LUA functions for manipulating the doubly-linked list
        structure that represents the allocation pool:
        - key_str(key: str) -> str: Get Redis key for a given key
        - pool_str() -> str: Get Redis key for the pool
        - pool_pointer_str(head: bool) -> str: Get Redis keys for head/tail pointers
        - cache_str() -> str: Get Redis key for the cache
        - timeout_to_expiry(timeout: int) -> int: Convert a timeout to an expiry
        - is_expiry_invalid(expiry: int) -> bool: Check if an expiry is invalid
        - is_expired(value: str) -> bool: Check if a value is expired
        - split_pool_value(value: str) -> tuple: Split a pool value into prev, next, and expiry
        - join_pool_value(prev: str, next: str, expiry: int) -> str: Join a pool value into a string
        - push_to_tail(itemName: str, expiry: int) -> None: Add an item to the tail of the linked list
        - pop_from_head() -> [str, int]: Remove and return the item at the head of the linked list
        - set_item_allocated(itemName: str) -> None: Set an item as allocated
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
        local function cache_str()
            return '{self._cache_str}'
        end
        local function soft_bind_name(name)
            if name == "" then
                return ""
            end
            return cache_str() .. ':bind:' .. name
        end
        local function split_pool_value(value)
            if value == nil then
                return "", "", -1
            end
            value = tostring(value)
            local prev, next, expiry = string.match(value, "(.*)||(.*)||(.*)")
            return prev, next, tonumber(expiry)
        end
        local function join_pool_value(prev, next, expiry)
            if expiry == nil then
                expiry = -1
            end
            return tostring(prev) .. "||" .. tostring(next) .. "||" .. tostring(expiry)
        end
        local function timeout_to_expiry(timeout)
            if timeout == nil or timeout <= 0 then
                return -1
            end
            return os.time() + timeout
        end
        local function is_expiry_invalid(expiry)
            return expiry ~= nil and expiry > 0 and expiry <= os.time()
        end
        local function is_expired(value)
            local _, _, expiry = split_pool_value(value)
            return is_expiry_invalid(expiry)
        end
        local poolItemsKey = pool_str()
        local headKey      = pool_pointer_str(true)
        local tailKey      = pool_pointer_str(false)
        local function push_to_tail(itemName, expiry)  -- push the item to the free list
            local tail = redis.call("GET", tailKey)
            if not tail then
                tail = ""
            end
            redis.call("HSET", poolItemsKey, itemName, join_pool_value(tail, "", expiry))
            if tail == "" then  -- the free list is empty chain
                redis.call("SET", headKey, itemName)
            else
                local tailVal = redis.call("HGET", poolItemsKey, tail)
                local prev, next, expiry = split_pool_value(tailVal)
                assert(next == "", "tail is not the last item in the free list")
                redis.call("HSET", poolItemsKey, tail, join_pool_value(prev, itemName, expiry))
            end
            redis.call("SET", tailKey, itemName)  -- set the tail point to the new item
        end
        local function pop_from_head()  -- pop the item from the free list
            local head = redis.call("GET", headKey)
            if head == nil or head == "" then  -- the free list is empty
                return nil, -1
            end
            local headVal = redis.call("HGET", poolItemsKey, head)
            assert(headVal ~= nil, "head should not nil")
            local headPrev, headNext, headExpiry = split_pool_value(headVal)
            -- Check if the head item has expired or is locked
            if is_expiry_invalid(headExpiry) then  -- the item has expired
                redis.call("HDEL", poolItemsKey, head)
                return pop_from_head()
            end
            if redis.call("EXISTS", key_str(head)) then  -- the item is locked
                return pop_from_head()
            end
            local prev, next, expiry = split_pool_value(headVal)
            if next == "" then  -- the item is the last in the free list
                redis.call("SET", headKey, "")
                redis.call("SET", tailKey, "")
            else
                local nextVal = redis.call("HGET", poolItemsKey, next)
                local nextPrev, nextNext, nextExpiry = split_pool_value(nextVal)
                redis.call("HSET", poolItemsKey, next, join_pool_value("", nextNext, nextExpiry))
                redis.call("SET", headKey, next)
            end
            return head, headExpiry
        end
        local function set_item_allocated(itemName)
            local val = redis.call("HGET", poolItemsKey, itemName)
            if val ~= nil then
                local prev, next, expiry = split_pool_value(val)
                if prev ~= "#ALLOCATED" then
                    if is_expiry_invalid(expiry) then
                        redis.call("HDEL", poolItemsKey, itemName)
                    end
                    if prev ~= "" then
                        local prevVal = redis.call("HGET", poolItemsKey, prev)
                        if prevVal then
                            local prevPrev, prevNext, prevExpiry = split_pool_value(prevVal)
                            redis.call("HSET", poolItemsKey, prev, join_pool_value(prevPrev, next, prevExpiry))
                        end
                    else
                        redis.call("SET", headKey, next or "")
                    end
                    if next ~= "" then
                        local nextVal = redis.call("HGET", poolItemsKey, next)
                        if nextVal then
                            local nextPrev, nextNext, nextExpiry = split_pool_value(nextVal)
                            redis.call("HSET", poolItemsKey, next, join_pool_value(prev, nextNext, nextExpiry))
                        end
                    else
                        redis.call("SET", tailKey, prev or "")
                    end
                    redis.call("SET", key_str(itemName), join_pool_value("#ALLOCATED", "#ALLOCATED", expiry))
                end
            end
        end
        local function check_item_health(itemName, value)
            if value == nil then
                value = redis.call("HGET", pool_str(), itemName)
            end
            if value then
                local prev, next, expiry = split_pool_value(value)
                if prev == "#ALLOCATED" then
                    local locked = redis.call("EXISTS", key_str(itemName))
                    if not locked then
                        push_to_tail(itemName, expiry)
                    end
                else
                    -- Check if the item has expired
                    if is_expiry_invalid(expiry) then
                        set_item_allocated(itemName)
                        redis.call("HDEL", poolItemsKey, itemName)
                    else
                        local locked = redis.call("EXISTS", key_str(itemName))
                        if locked then
                            set_item_allocated(itemName)
                        end
                    end
                end
            end
        end
        '''

    @cached_property
    def _extend_script(self):
        """Cached Redis script for extending the allocation pool."""
        return self.redis.register_script(f'''{self._lua_required_string}\n
        local timeout = tonumber(ARGV[1] or -1)
        local expiry = timeout_to_expiry(timeout)
        for i=2, #ARGV do
            local itemName = ARGV[i]
            local val = redis.call("HGET", poolItemsKey, itemName)
            if val == nil then
                push_to_tail(itemName, expiry)
            else -- refresh the expiry timeout
                local prev, next, _ = split_pool_value(val)
                redis.call("HSET", poolItemsKey, itemName, join_pool_value(prev, next, expiry))
            end
        end''')

    def extend(self, keys: Optional[Sequence[str]] = None, timeout: int = -1):
        """Add new resources to the allocation pool.

        Args:
            keys: Sequence of resource identifiers to add to the pool
            timeout: Optional timeout in seconds for the pool items (-1 means no timeout)
        """
        if keys is not None and len(keys) > 0:
            # Ensure timeout is integer for Lua script
            int_timeout = timeout if timeout is not None else -1
            self._extend_script(args=[int_timeout] + list(keys))

    @cached_property
    def _shrink_script(self):
        """Cached Redis script for shrinking the allocation pool."""
        return self.redis.register_script(f'''{self._lua_required_string}
        for i=1, #ARGV do
            local itemName = ARGV[i]
            set_item_allocated(itemName)
            redis.call("HDEL", poolItemsKey, itemName)
        end''')

    def shrink(self, keys: Optional[Sequence[str]] = None):
        """Remove resources from the allocation pool.

        Args:
            keys: Sequence of resource identifiers to remove from the pool
        """
        if keys is not None and len(keys) > 0:
            self._shrink_script(args=keys)

    @cached_property
    def _assign_script(self):
        """Cached Redis script for assigning resources to the allocation pool."""
        return self.redis.register_script(f'''{self._lua_required_string}
        local timeout = tonumber(ARGV[1] or -1)
        local expiry = timeout_to_expiry(timeout)
        local assignSet  = {{}}
        for i=2, #ARGV do
            local k = ARGV[i]
            assignSet[k] = true
        end
        local allItems = redis.call("HKEYS", poolItemsKey)
        for _, itemName in ipairs(allItems) do
            if not assignSet[itemName] then
                set_item_allocated(itemName)
                redis.call("HDEL", poolItemsKey, itemName)
            else
                assignSet[itemName] = nil
            end
        end
        for k, v in pairs(assignSet) do
            if v then
                push_to_tail(k, expiry)
            end
        end
        ''')

    def assign(self, keys: Optional[Sequence[str]] = None, timeout: int = -1):
        """Completely replace the resources in the allocation pool.

        Args:
            keys: Sequence of resource identifiers to assign to the pool,
                 replacing any existing resources
            timeout: Optional timeout in seconds for the pool items (-1 means no timeout)
        """
        if keys is not None and len(keys) > 0:
            self._assign_script(args=[timeout] + list(keys))
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

        Soft bindings create a persistent mapping between named objects and allocated keys,
        allowing the same key to be consistently allocated to the same named object.
        This is useful for maintaining affinity between objects and their resources.

        Args:
            name: Name to bind
            key: Resource identifier to bind to the name
        """
        self.update(self._soft_bind_name(name), key, timeout=self.soft_bind_timeout)

    def unbind_soft_bind(self, name: str):
        """Remove a soft binding.

        This removes the persistent mapping between a named object and its allocated key,
        allowing the key to be freely allocated to any requestor.

        Args:
            name: Name of the soft binding to remove
        """
        self.unlock(self._soft_bind_name(name))

    @cached_property
    def _malloc_script(self):
        """Cached Redis script for allocating a resource."""
        return self.redis.register_script(f'''
        {self._lua_required_string}
        local shared = {1 if self.shared else 0}
        local timeout = tonumber(ARGV[1])
        local cacheName = soft_bind_name(ARGV[2])
        local cacheTimeout = tonumber(ARGV[3])
        local function refresh_cache(cacheKey)
            if cacheName then
                if cacheTimeout ~= nil and cacheTimeout > 0 then
                    redis.call("SET", cacheName, cacheKey, "EX", cacheTimeout)
                else
                    redis.call("SET", cacheName, cacheKey)
                end
            end
        end
        if name == "" then
            local cachedKey = redis.call("GET", cacheName)
            if cachedKey then
                if redis.call("EXISTS", key_str(cachedKey)) then
                    redis.call("DEL", cacheName)
                else
                    refresh_cache(cachedKey)
                    return cachedKey
                end
            end
        end
        local itemName, expiry = pop_from_head()
        if itemName ~= nil then
            if not shared then
                if timeout ~= nil and timeout > 0 then
                    redis.call("SET", key_str(itemName), "1", "EX", timeout)
                else
                    redis.call("SET", key_str(itemName), "1")
                end
            else
                push_to_tail(itemName, expiry)
            end
        end
        if itemName then
            refresh_cache(itemName)
        end
        return itemName
        ''')

    def malloc_key(self, timeout: Timeout = 120, name: Optional[str] = None) -> Optional[str]:
        """Allocate a resource key from the pool.

        The behavior depends on the allocator's shared mode:
        - In non-shared mode (default): Locks the allocated key for exclusive access
        - In shared mode: Simply removes the key from the free list without locking it

        Args:
            timeout: How long the allocation should be valid (in seconds)
            obj: Optional object to use for additional context

        Returns:
            Resource identifier if allocation was successful, None otherwise
        """
        if name is None:
            name = ""
        return self._malloc_script(args=[self._to_seconds(timeout), name])

    def malloc(self, timeout: Timeout = 120, obj: Optional[U] = None, params: Optional[dict] = None) -> Optional[RedisAllocatorObject[U]]:
        """Allocate a resource from the pool and wrap it in a RedisAllocatorObject.

        If a policy is configured, it will be used to control the allocation behavior.
        Otherwise, the basic allocation mechanism will be used.

        Args:
            timeout: How long the allocation should be valid (in seconds)
            obj: The object to wrap in the RedisAllocatorObject
            params: Additional parameters to associate with the allocated object

        Returns:
            RedisAllocatorObject wrapping the allocated resource if successful, None otherwise
        """
        if self.policy:
            return self.policy.malloc(self, timeout, obj, params)
        name = obj.name if obj else None
        key = self.malloc_key(timeout, name)
        return RedisAllocatorObject(self, key, obj, params)

    @cached_property
    def _free_script(self):
        """Cached Redis script for freeing allocated resources."""
        return self.redis.register_script(f'''
        {self._lua_required_string}
        local timeout = tonumber(ARGV[1] or -1)
        local expiry = timeout_to_expiry(timeout)
        for i=2, #ARGV do
            local k = ARGV[i]
            redis.call('DEL', key_str(k))
            push_to_tail(k, expiry)
        end
        ''')

    def free_keys(self, *keys: str, timeout: int = -1):
        """Free allocated resources.

        Args:
            *keys: Resource identifiers to free
            timeout: Optional timeout in seconds for the pool items (-1 means no timeout)
        """
        if keys:
            self._free_script(args=[timeout] + list(keys))

    def free(self, obj: RedisAllocatorObject[U], timeout: int = -1):
        """Free an allocated object.

        Args:
            obj: The allocated object to free
            timeout: Optional timeout in seconds for the pool item (-1 means no timeout)
        """
        self.free_keys(obj.key, timeout=timeout)

    def _gc_cursor_str(self):
        """Get the Redis key for the garbage collection cursor.

        Returns:
            String representation of the Redis key for the GC cursor
        """
        return f'{self._pool_str()}|gc_cursor'

    @cached_property
    def _gc_script(self):
        """Cached Redis script for garbage collection."""
        return self.redis.register_script(f'''
        {self._lua_required_string}
        local cursorKey = '{self._gc_cursor_str()}'
        local function get_cursor()
            local oldCursor = redis.call("GET", cursorKey)
            if not oldCursor or oldCursor == "" then
                return "0"
            else
                return oldCursor
            end
        end
        local function set_cursor(cursor)
            redis.call("SET", cursorKey, cursor)
        end
        local n = tonumber(ARGV[1])
        local scanResult = redis.call("HSCAN", pool_str(), get_cursor(), "COUNT", n)
        local newCursor  = scanResult[1]
        local kvList     = scanResult[2]
        for i = 1, #kvList, 2 do
            local itemName = kvList[i]
            local val      = kvList[i + 1]
            check_item_health(itemName, val)
        end
        set_cursor(newCursor)
        ''')

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
