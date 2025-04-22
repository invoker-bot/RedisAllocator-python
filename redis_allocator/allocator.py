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
import atexit
import logging
import weakref
import contextlib
from abc import ABC, abstractmethod
from typing import Any, Callable
from functools import cached_property
from threading import current_thread
from concurrent.futures import ThreadPoolExecutor
from typing import (Optional, TypeVar, Generic,
                    Sequence, Iterable)
from redis import StrictRedis as Redis
from cachetools import cached, TTLCache
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

    def open(self):
        """Open the object."""
        return self

    def close(self):
        """close the object."""
        pass

    def is_healthy(self):
        return True

    @property
    def name(self) -> Optional[str]:
        """Get the cache name of the object, if is none no soft binding will be used."""
        return None

    @property
    def unique_id(self) -> str:
        """Get the unique ID of the object."""
        return ""


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

    def open(self):
        """Open the object."""
        if self.obj is not None:
            self.obj.open()
        return self

    def close(self):
        """Kill the object."""
        if self.obj is not None:
            self.obj.close()

    def is_healthy(self) -> bool:
        """Check if the object is healthy."""
        if self.obj is not None:
            return self.obj.is_healthy()
        return True

    def set_healthy(self, duration: Timeout = 3600):
        """Set the object as healthy."""
        if self.obj is not None and self.obj.name is not None:
            self.allocator.update_soft_bind(self.obj.name, self.key, duration)
        if self.allocator.shared:
            self.allocator.unlock(self.key)

    def set_unhealthy(self, duration: Timeout = 3600):
        """Set the object as unhealthy."""
        if self.obj is not None and self.obj.name is not None:
            self.allocator.unbind_soft_bind(self.obj.name)
        self.allocator.update(self.key, timeout=duration)

    def refresh(self, timeout: Timeout = 120, cache_timeout: Timeout = 3600):
        """Refresh the object."""
        self.close()
        new_obj = self.allocator.policy.malloc(self.allocator, timeout=timeout,
                                               obj=self.obj, params=self.params,
                                               cache_timeout=cache_timeout)
        if new_obj is not None:
            self.obj = new_obj.obj
            self.key = new_obj.key
            self.params = new_obj.params
            self.open()
        elif self.obj is not None:
            logger.error("Failed to refresh the object %s", self.key)

    def refresh_until_healthy(self, timeout: Timeout = 120, max_attempts: int = 10, lock_duration: Timeout = 3600, cache_timeout: Timeout = 3600):
        """Refresh the object until it is healthy."""
        for _ in range(max_attempts):
            try:
                if self.is_healthy():
                    return
            except Exception as e:
                logger.error(f"Error checking health of {self.key}: {e}")
            self.set_unhealthy(lock_duration)
            self.refresh(timeout, cache_timeout)
        raise RuntimeError("the objects is still unhealthy after %d attempts", max_attempts)

    @property
    def unique_id(self) -> str:
        """Get the unique ID of the object."""
        if self.obj is None:
            return self.key
        return f"{self.key}:{self.obj.unique_id}"

    @property
    def name(self) -> Optional[str]:
        """Get the name of the object."""
        if self.obj is None:
            return None
        return self.obj.name

    # def __del__(self):
    #     """Delete the object."""
    #     self.close()


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


class RedisAllocatorPolicy(ABC, Generic[U]):
    """Abstract base class for Redis allocator policies.

    This class defines the interface for allocation policies that can be used
    with RedisAllocator to control allocation behavior.
    """

    def initialize(self, allocator: 'RedisAllocator[U]'):
        """Initialize the policy with an allocator instance.

        Args:
            allocator: The RedisAllocator instance to use with this policy
        """
        pass

    @abstractmethod
    def malloc(self, allocator: 'RedisAllocator[U]', timeout: Timeout = 120,
               obj: Optional[U] = None, params: Optional[dict] = None,
               cache_timeout: Timeout = 3600) -> Optional[RedisAllocatorObject[U]]:
        """Allocate a resource according to the policy.

        Args:
            allocator: The RedisAllocator instance
            timeout: How long the allocation should be valid (in seconds)
            obj: The object to associate with the allocation
            params: Additional parameters for the allocation
            cache_timeout: Timeout for the soft binding cache entry (seconds).
                       Defaults to 3600.

        Returns:
            RedisAllocatorObject if allocation was successful, None otherwise
        """
        pass

    @abstractmethod
    def refresh_pool(self, allocator: 'RedisAllocator[U]'):
        """Refresh the allocation pool.

        This method is called periodically to update the pool with new resources.

        Args:
            allocator: The RedisAllocator instance
        """
        pass

    def check_health_once(self, r_obj: RedisAllocatorObject[U], duration: int = 3600) -> bool:
        """Check the health of the object."""
        with contextlib.closing(r_obj.open()):
            try:
                if r_obj.is_healthy():
                    if r_obj.allocator.shared:
                        r_obj.allocator.unlock(r_obj.key)  # set the key as free
                    return True
                else:
                    r_obj.set_unhealthy(duration)
                    return False
            except Exception as e:
                logger.error(f"Error checking health of {r_obj.key}: {e}")
                r_obj.set_unhealthy(duration)
                raise

    def check_health(self, allocator: 'RedisAllocator[U]', lock_duration: Timeout = 3600, max_threads: int = 8,
                     obj_fn: Optional[Callable[[str], U]] = None,
                     params_fn: Optional[Callable[[str], dict]] = None) -> tuple[int, int]:
        """Check the health of the allocator.

        Args:
            allocator: The RedisAllocator instance
            lock_duration: The duration of the lock (in seconds)
            max_threads: The maximum number of threads to use

        Returns:
            A tuple containing the number of healthy and unhealthy items in the allocator
        """
        with ThreadPoolExecutor(max_workers=max_threads) as executor:
            inputs = []
            for key in allocator.keys():
                if params_fn is not None:
                    params = params_fn(key)
                else:
                    params = None
                if obj_fn is not None:
                    obj = obj_fn(key)
                else:
                    obj = None
                inputs.append(RedisAllocatorObject(allocator, key, obj, params))
            results = list(executor.map(self.check_health_once, inputs, timeout=lock_duration))
            healthy = sum(results)
            unhealthy = len(results) - healthy
            return healthy, unhealthy


class DefaultRedisAllocatorPolicy(RedisAllocatorPolicy[U]):
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
                 expiry_duration: int = -1, updater: Optional[RedisAllocatorUpdater] = None,
                 auto_close: bool = False):
        """Initialize the default allocation policy.

        Args:
            gc_count: Number of GC operations to perform before allocation
            update_interval: Interval in seconds between pool updates
            expiry_duration: Default timeout for pool items (-1 means no timeout)
            updater: Optional updater for refreshing the pool's keys
            auto_close: If True, the allocator will automatically close the object when it is not unique
        """
        self.gc_count = gc_count
        self.update_interval: float = update_interval
        self.expiry_duration: float = expiry_duration
        self.updater = updater
        self._allocator: Optional[weakref.ReferenceType['RedisAllocator']] = None
        self._update_lock_key: Optional[str] = None
        self.objects: weakref.WeakValueDictionary[str, RedisAllocatorObject] = weakref.WeakValueDictionary()
        self.auto_close = auto_close

    def initialize(self, allocator: 'RedisAllocator[U]'):
        """Initialize the policy with an allocator instance.

        Args:
            allocator: The RedisAllocator instance to use with this policy
        """
        self._allocator = weakref.ref(allocator)
        self._update_lock_key = f"{allocator._pool_str()}|policy_update_lock"
        atexit.register(lambda: self.finalize(self._allocator()))

    def malloc(self, allocator: 'RedisAllocator[U]', timeout: Timeout = 120,
               obj: Optional[U] = None, params: Optional[dict] = None,
               cache_timeout: Timeout = 3600) -> Optional[RedisAllocatorObject[U]]:
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
            cache_timeout: Timeout for the soft binding cache entry (seconds).
                       Defaults to 3600.

        Returns:
            RedisAllocatorObject if allocation was successful, None otherwise
        """
        # Try to refresh the pool if necessary
        self._try_refresh_pool(allocator)

        # Perform GC operations before allocation
        allocator.gc(self.gc_count)

        # Fall back to regular allocation
        # Explicitly call obj.name if obj exists
        obj_name = obj.name if obj and hasattr(obj, 'name') and callable(
            obj.name) else (obj.name if obj and hasattr(obj, 'name') else None)
        key = allocator.malloc_key(timeout, obj_name,
                                   cache_timeout=cache_timeout)
        alloc_obj = RedisAllocatorObject(allocator, key, obj, params)
        if self.auto_close:
            old_obj = self.objects.get(alloc_obj.unique_id, None)
            if old_obj is not None:
                old_obj.close()
        self.objects[alloc_obj.unique_id] = alloc_obj
        return alloc_obj

    @cached(TTLCache(maxsize=64, ttl=5))
    def _try_refresh_pool(self, allocator: 'RedisAllocator[U]'):
        """Try to refresh the pool if necessary and if we can acquire the lock.

        Args:
            allocator: The RedisAllocator instance
        """
        if self.updater is None:
            return
        if allocator.lock(self._update_lock_key, timeout=self.update_interval):
            # If we got here, we acquired the lock, so we can update the pool
            self.refresh_pool(allocator)

    def refresh_pool(self, allocator: 'RedisAllocator[U]'):
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

    def finalize(self, allocator: 'RedisAllocator[U]'):
        """Finalize the policy."""
        for obj in self.objects.values():
            obj.close()


class RedisAllocator(RedisLockPool, Generic[U]):
    """A Redis-based distributed allocation system.

    Manages a pool of resource identifiers (keys) using Redis, allowing distributed
    clients to allocate, free, and manage these resources. It leverages Redis's
    atomic operations via Lua scripts for safe concurrent access.

    The allocator maintains a doubly-linked list in a Redis hash to track available
    (free) resources. Allocated resources are tracked using standard Redis keys
    that act as locks.

    Key Concepts:
    - Allocation Pool: A set of resource identifiers (keys) managed by the allocator.
      Stored in a Redis hash (`<prefix>|<suffix>|pool`) representing a doubly-linked list.
      Head/Tail pointers are stored in separate keys (`<prefix>|<suffix>|pool|head`,
      `<prefix>|<suffix>|pool|tail`).
    - Free List: The subset of keys within the pool that are currently available.
      Represented by the linked list structure within the pool hash.
    - Allocated State: A key is considered allocated if a corresponding lock key exists
      (`<prefix>|<suffix>:<key>`).
    - Shared Mode: If `shared=True`, allocating a key moves it to the tail of the
      free list but does *not* create a lock key. This allows multiple clients to
      "allocate" the same key concurrently, effectively using the list as a rotating pool.
      If `shared=False` (default), allocation creates a lock key, granting exclusive access.
    - Soft Binding: Allows associating a logical name with an allocated key. If an object
      provides a `name`, the allocator tries to reuse the previously bound key for that name.
      Stored in Redis keys like `<prefix>|<suffix>-cache:bind:<name>`.
    - Garbage Collection (GC): Periodically scans the pool to reconcile the free list
      with the lock states. Removes expired/locked items from the free list and returns
      items whose locks have expired back to the free list.
    - Policies: Uses `RedisAllocatorPolicy` (e.g., `DefaultRedisAllocatorPolicy`)
      to customize allocation behavior, GC triggering, and pool updates.

    Generic type U should implement `RedisAllocatableClass`.
    """

    def __init__(self, redis: Redis, prefix: str, suffix='allocator', eps=1e-6,
                 shared=False, policy: Optional[RedisAllocatorPolicy[U]] = None):
        """Initializes the RedisAllocator.

        Args:
            redis: StrictRedis client instance (must decode responses).
            prefix: Prefix for all Redis keys used by this allocator instance.
            suffix: Suffix to uniquely identify this allocator instance's keys.
            eps: Small float tolerance for comparisons (used by underlying lock).
            shared: If True, operates in shared mode (keys are rotated, not locked).
                    If False (default), keys are locked upon allocation.
            policy: Optional allocation policy. Defaults to `DefaultRedisAllocatorPolicy`.
        """
        super().__init__(redis, prefix, suffix=suffix, eps=eps)
        self.shared = shared
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
        """Base Lua script providing common functions for pool manipulation.

        Includes functions inherited from RedisLockPool and adds allocator-specific ones:
        - pool_pointer_str(head: bool): Returns the Redis key for the head/tail pointer.
        - cache_str(): Returns the Redis key for the allocator's general cache.
        - soft_bind_name(name: str): Returns the Redis key for a specific soft binding.
        - split_pool_value(value: str): Parses the 'prev||next||expiry' string stored
                                        in the pool hash for a key.
        - join_pool_value(prev: str, next: str, expiry: int): Creates the value string.
        - timeout_to_expiry(timeout: int): Converts relative seconds to absolute Unix timestamp.
        - is_expiry_invalid(expiry: int): Checks if an absolute expiry time is in the past.
        - is_expired(value: str): Checks if a pool item's expiry is in the past.
        - push_to_tail(itemName: str, expiry: int): Adds/updates an item at the tail of the free list.
        - pop_from_head(): Removes and returns the item from the head of the free list,
                           skipping expired or locked items. Returns (nil, -1) if empty.
        - set_item_allocated(itemName: str): Removes an item from the free list structure.
        - check_item_health(itemName: str, value: str|nil): Core GC logic for a single item.
            - If item is marked #ALLOCATED but has no lock -> push to tail (return to free list).
            - If item is in free list but expired -> remove from pool hash.
            - If item is in free list but locked -> mark as #ALLOCATED (remove from free list).
        """
        return f'''
        {super()._lua_required_string}
        local function time()
            return tonumber(redis.call("TIME")[1])
        end
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
            if name == "" or not name then
                return ""
            end
            return cache_str() .. ':bind:' .. name
        end
        local function split_pool_value(value)
            if not value or value == "" then
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
            return time() + timeout
        end
        local function is_expiry_invalid(expiry)
            return expiry ~= nil and expiry > 0 and expiry <= time()
        end
        local function is_expired(value)
            local _, _, expiry = split_pool_value(value)
            return is_expiry_invalid(expiry)
        end
        local poolItemsKey = pool_str()
        local headKey      = pool_pointer_str(true)
        local tailKey      = pool_pointer_str(false)
        local function current_pointer(pointerKey)
            local pointer = redis.call("GET", pointerKey)
            if not pointer then
                return ""
            end
            return pointer
        end
        local function current_head()
            return current_pointer(headKey)
        end
        local function current_tail()
            return current_pointer(tailKey)
        end
        local function set_current_head(pointer)
            redis.call("SET", headKey, pointer)
        end
        local function set_current_tail(pointer)
            redis.call("SET", tailKey, pointer)
        end

        local function push_to_tail(itemName, expiry)  -- push the item to the free list, item should not be in the free list
            local head = current_head()
            local tail = current_tail()
            ---- this is the debug code
            local itemVal = redis.call("HGET", poolItemsKey, itemName)
            if itemVal then
                local prev, next, expiry = split_pool_value(itemVal)
                assert(prev == "#ALLOCATED" or prev == "", "item should be allocated or free")
                assert(next == "" or next == "#ALLOCATED", "item should be the last item in the free list")
            end
            -- set the item points to the tail
            redis.call("HSET", poolItemsKey, itemName, join_pool_value(tail, "", expiry))
            if tail == "" or head == "" then  -- the free list is empty chain
                -- assert(tail == "" and head == "", "head or tail should not be empty")
                set_current_head(itemName)
            else
                local tailVal = redis.call("HGET", poolItemsKey, tail)
                assert(tailVal, "tail value should not be nil")
                local tailPrev, tailNext, tailExpiry = split_pool_value(tailVal)
                assert(tailNext == "", "tail should be the last item in the free list")
                redis.call("HSET", poolItemsKey, tail, join_pool_value(tailPrev, itemName, tailExpiry))
            end
            set_current_tail(itemName)
        end

        -- Ensure the new head node is well-formed (prev="") and update tail/head
        local function set_item_head_nil(nextItemName)
            if nextItemName == "" then
                -- list becomes empty
                redis.call("SET", headKey, "")
                redis.call("SET", tailKey, "")
                return
            end

            local nextVal = redis.call("HGET", poolItemsKey, nextItemName)
            if not nextVal then
                -- corrupted pointer, clear list
                redis.call("SET", headKey, "")
                redis.call("SET", tailKey, "")
                return
            end

            local _prev, nextNext, nextExpiry = split_pool_value(nextVal)
            if _prev ~= "" then
                redis.call("HSET", poolItemsKey, nextItemName, join_pool_value("", nextNext, nextExpiry))
            end
        end

        local function pop_from_head()  -- pop the item from the free list
            local head = current_head()
            if not head or head == "" then  -- the free list is empty
                return nil, -1
            end
            local headVal = redis.call("HGET", poolItemsKey, head)
            assert(headVal, "head value should not be nil")
            local headPrev, headNext, headExpiry = split_pool_value(headVal)
            -- Check if the head item has expired or is locked
            if is_expiry_invalid(headExpiry) then  -- the item has expired
                redis.call("HDEL", poolItemsKey, head)
                set_current_head(headNext)
                -- set_item_head_nil(headNext)
                return pop_from_head()
            elseif redis.call("EXISTS", key_str(head)) > 0 then  -- the item is locked
                redis.call("HSET", poolItemsKey, head, join_pool_value("#ALLOCATED", "#ALLOCATED", headExpiry))
                set_current_head(headNext)
                -- set_item_head_nil(headNext)
                return pop_from_head()
            elseif headNext == "" then  -- the item is the last in the free list
                redis.call("SET", headKey, "")
                redis.call("SET", tailKey, "")
            else
                local nextVal = redis.call("HGET", poolItemsKey, headNext)
                assert(nextVal, "next value should not be nil")
                local nextPrev, nextNext, nextExpiry = split_pool_value(nextVal)
                redis.call("HSET", poolItemsKey, headNext, join_pool_value("", nextNext, nextExpiry))
                redis.call("SET", headKey, headNext)
            end
            redis.call("HSET", poolItemsKey, head, join_pool_value("#ALLOCATED", "#ALLOCATED", headExpiry))
            return head, headExpiry
        end

        local function set_item_allocated(itemName, val)
            if not val then
                val = redis.call("HGET", pool_str(), itemName)
            end
            assert(val, "val should not be nil")
            local prev, next, expiry = split_pool_value(val)
            if prev ~= "#ALLOCATED" then
                assert(next ~= "#ALLOCATED", "next item should not be allocated")
                if is_expiry_invalid(expiry) then
                    redis.call("HDEL", poolItemsKey, itemName)
                end
                if prev ~= "" then
                    local prevVal = redis.call("HGET", poolItemsKey, prev)
                    assert(prevVal, "prev value should not be nil", prev, prevVal)
                    local prevPrev, prevNext, prevExpiry = split_pool_value(prevVal)
                    redis.call("HSET", poolItemsKey, prev, join_pool_value(prevPrev, next, prevExpiry))
                else
                    redis.call("SET", headKey, next)
                end
                if next ~= "" then
                    local nextVal = redis.call("HGET", poolItemsKey, next)
                    assert(nextVal, "next value should not be nil")
                    local nextPrev, nextNext, nextExpiry = split_pool_value(nextVal)
                    redis.call("HSET", poolItemsKey, next, join_pool_value(prev, nextNext, nextExpiry))
                else
                    redis.call("SET", tailKey, prev)
                end
                redis.call("HSET", poolItemsKey, itemName, join_pool_value("#ALLOCATED", "#ALLOCATED", expiry))
                -- If we removed the current head, update head pointer
                -- local savedHead = redis.call("GET", headKey)
                -- if savedHead == itemName then
                --     redis.call("SET", headKey, next or "")
                -- end
            else
                assert(next == "#ALLOCATED", "next item should also be allocated")
            end
        end

        local function check_item_health(itemName)
            local value = redis.call("HGET", pool_str(), itemName)
            assert(value, "value should not be nil")
            local prev, next, expiry = split_pool_value(value)
            if is_expiry_invalid(expiry) then  -- Check if the item has expired
                set_item_allocated(itemName, value)
                redis.call("HDEL", poolItemsKey, itemName)
                return
            end
            local locked = redis.call("EXISTS", key_str(itemName)) > 0
            if prev == "#ALLOCATED" then
                if not locked then
                    push_to_tail(itemName, expiry)
                end
            else
                if locked then
                    set_item_allocated(itemName, value)
                end
            end
        end

        -- Return an array of item names in the free list order (head -> tail)
        -- Does NOT modify the list. Uses a bounded loop (at most pool size)
        -- to avoid infinite traversal when the list structure is corrupted.
        local function get_free_list()
            local items = {{}}
            local current = redis.call("GET", headKey)
            if not current or current == "" then
                return items  -- empty list
            end
            local max_iters = tonumber(redis.call("HLEN", poolItemsKey))
            if not max_iters or max_iters <= 0 then
                return items
            end
            for i = 1, max_iters do
                if not current or current == "" then
                    break
                end
                table.insert(items, current)
                local val = redis.call("HGET", poolItemsKey, current)
                if not val then
                    break  -- corrupted pointer
                end
                local _prev, nxt, _expiry = split_pool_value(val)
                if nxt == "" or nxt == "#ALLOCATED" then
                    break -- reached tail or allocated marker
                end
                current = nxt
            end
            return items
        end
        '''

    @cached_property
    def _extend_script(self):
        """Cached Lua script to add or update keys in the pool.

        Iterates through provided keys (ARGV[2...]).
        If a key doesn't exist in the pool hash, it's added to the tail of the free list
        using push_to_tail() with the specified expiry (calculated from ARGV[1] timeout).
        If a key *does* exist, its expiry time is updated in the pool hash.
        """
        return self.redis.register_script(f'''
        {self._lua_required_string}
        local timeout = tonumber(ARGV[1] or -1)
        local expiry = timeout_to_expiry(timeout)
        for i=2, #ARGV do
            local itemName = ARGV[i]
            local val = redis.call("HGET", poolItemsKey, itemName)
            if val then  -- only refresh the expiry timeout
                local prev, next, _ = split_pool_value(val)
                val = join_pool_value(prev, next, expiry)
                redis.call("HSET", poolItemsKey, itemName, val)
            else -- refresh the expiry timeout
                push_to_tail(itemName, expiry)
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
        """Cached Lua script to remove keys from the pool.

        Iterates through provided keys (ARGV[1...]).
        For each key:
        1. Calls set_item_allocated() to remove it from the free list structure.
        2. Deletes the key entirely from the pool hash using HDEL.
        """
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
        """Cached Lua script to set the pool to exactly the given keys.

        1. Builds a Lua set (`assignSet`) of the desired keys (ARGV[2...]).
        2. Fetches all current keys from the pool hash (HKEYS).
        3. Iterates through current keys:
           - If a key is *not* in `assignSet`, it's removed from the pool
             (set_item_allocated() then HDEL).
           - If a key *is* in `assignSet`, it's marked as processed by setting
             `assignSet[key] = nil`.
        4. Iterates through the remaining keys in `assignSet` (those not already
           in the pool). These are added to the tail of the free list using
           push_to_tail() with the specified expiry (from ARGV[1] timeout).
        """
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

    def update_soft_bind(self, name: str, key: str, timeout: Timeout = 3600):
        """Update a soft binding between a name and a resource.

        Soft bindings create a persistent mapping between named objects and allocated keys,
        allowing the same key to be consistently allocated to the same named object.
        This is useful for maintaining affinity between objects and their resources.

        Args:
            name: Name to bind
            key: Resource identifier to bind to the name
        """
        self.update(self._soft_bind_name(name), key, timeout=timeout)

    def unbind_soft_bind(self, name: str):
        """Remove a soft binding.

        This removes the persistent mapping between a named object and its allocated key,
        allowing the key to be freely allocated to any requestor.

        Args:
            name: Name of the soft binding to remove
        """
        self.unlock(self._soft_bind_name(name))

    def get_soft_bind(self, name: str) -> Optional[str]:
        """Get the resource identifier bound to a name.

        Args:
            name: Name of the soft binding
        """
        return self.redis.get(self._soft_bind_name(name))

    @cached_property
    def _malloc_script(self):
        """Cached Lua script to allocate a key from the pool.

        Input ARGS: timeout, name (for soft binding), soft_bind_timeout

        1. Soft Binding Check (if name provided):
           - Tries to GET the bound key from the soft bind cache key.
           - If found and the key is *not* currently locked (checked via EXISTS key_str(cachedKey)),
             it refreshes the soft bind expiry and returns the cached key.
           - If found but the key *is* locked, it deletes the stale soft bind entry.
        2. Pop from Head: Calls `pop_from_head()` to get the next available key
           from the free list head. This function internally skips expired/locked items.
        3. Lock/Update (if key found):
           - If `shared=False`: Sets the lock key (`key_str(itemName)`) with the specified timeout.
           - If `shared=True`: Calls `push_to_tail()` to put the item back onto the free list immediately.
        4. Update Soft Bind Cache (if key found and name provided):
           - Sets the soft bind cache key to the allocated `itemName` with its timeout.
        5. Returns the allocated `itemName` or nil if the pool was empty.
        """
        return self.redis.register_script(f'''
        {self._lua_required_string}
        local shared = {1 if self.shared else 0}

        local timeout = tonumber(ARGV[1])
        local name_arg = ARGV[2] -- Original name argument
        local cacheName = soft_bind_name(name_arg) -- Key for soft binding cache
        local cacheTimeout = tonumber(ARGV[3]) -- Timeout for the soft binding cache entry
        local function refresh_cache(cacheKey)
            -- Only refresh if a valid name and timeout were provided
            if name_arg ~= "" then
                if cacheTimeout ~= nil and cacheTimeout > 0 then
                    redis.call("SET", cacheName, cacheKey, "EX", cacheTimeout)
                else -- If timeout is invalid/zero, set without expiry
                    redis.call("SET", cacheName, cacheKey)
                end
            end
        end
        -- Check soft binding only if a name was provided
        if name_arg ~= "" then
            local cachedKey = redis.call("GET", cacheName)
            if cachedKey then
                -- Check if the cached key exists and is currently locked (in non-shared mode)
                if redis.call("HEXISTS", poolItemsKey, cachedKey) <= 0 or redis.call("EXISTS", key_str(cachedKey)) > 0 then
                    -- Cached key is locked, binding is stale, remove it
                    redis.call("DEL", cacheName)
                else
                    -- Cached key is valid (either not locked or in shared mode)
                    refresh_cache(cachedKey) -- Refresh the cache expiry
                    if shared == 0 then
                        redis.call("SET", key_str(cachedKey), "1", "EX", timeout)
                        set_item_allocated(cachedKey)
                    end
                    return cachedKey
                end
            end
        end

        -- No valid soft bind found, proceed with normal allocation
        local itemName, expiry = pop_from_head()
        if itemName ~= nil then
            if shared == 0 then
                -- Non-shared mode: Acquire lock
                if timeout ~= nil and timeout > 0 then
                    redis.call("SET", key_str(itemName), "1", "EX", timeout)
                else
                    redis.call("SET", key_str(itemName), "1") -- Set without expiry if timeout <= 0
                end
            else
                -- Shared mode: Just put it back to the tail
                push_to_tail(itemName, expiry)
            end
        end

        -- If allocation was successful and a name was provided, update the soft bind cache
        if itemName then
            refresh_cache(itemName)
        end
        return itemName
        ''')

    def malloc_key(self, timeout: Timeout = 120, name: Optional[str] = None,
                   cache_timeout: Timeout = 3600) -> Optional[str]:
        """Allocate a resource key from the pool.

        The behavior depends on the allocator's shared mode:
        - In non-shared mode (default): Locks the allocated key for exclusive access
        - In shared mode: Simply removes the key from the free list without locking it

        Args:
            timeout: How long the allocation lock should be valid (in seconds).
            name: Optional name to use for soft binding.
            cache_timeout: Timeout for the soft binding cache entry (seconds).
                           Defaults to 3600. If <= 0, cache entry persists indefinitely.

        Returns:
            Resource identifier if allocation was successful, None otherwise
        """
        if name is None:
            name = ""
        # Convert timeout values to integers for Lua
        lock_timeout_sec = int(self._to_seconds(timeout))
        cache_timeout_sec = int(self._to_seconds(cache_timeout))
        # Convert integers to strings for Lua script arguments
        return self._malloc_script(args=[
            lock_timeout_sec,
            name,
            cache_timeout_sec,
        ])

    def malloc(self, timeout: Timeout = 120, obj: Optional[U] = None, params: Optional[dict] = None,
               cache_timeout: Timeout = 3600) -> Optional[RedisAllocatorObject[U]]:
        """Allocate a resource from the pool and wrap it in a RedisAllocatorObject.

        If a policy is configured, it will be used to control the allocation behavior.
        Otherwise, the basic allocation mechanism will be used.

        Args:
            timeout: How long the allocation lock should be valid (in seconds)
            obj: The object to wrap in the RedisAllocatorObject. If it has a `.name`,
                 soft binding will be attempted.
            params: Additional parameters to associate with the allocated object.
            cache_timeout: Timeout for the soft binding cache entry (seconds).
                           Defaults to 3600. Passed to the policy or `malloc_key`.

        Returns:
            RedisAllocatorObject wrapping the allocated resource if successful, None otherwise
        """
        if self.policy:
            # Pass cache_timeout to the policy's malloc method
            return self.policy.malloc(
                self, timeout, obj, params,
                cache_timeout=cache_timeout
            )
        # No policy, call malloc_key directly
        # Explicitly call obj.name if obj exists
        name = obj.name if obj and hasattr(obj, 'name') else None
        key = self.malloc_key(timeout, name, cache_timeout=cache_timeout)
        return RedisAllocatorObject(
            self, key, obj, params
        )

    @cached_property
    def _free_script(self):
        """Cached Lua script to free allocated keys.

        Iterates through provided keys (ARGV[2...]).
        For each key:
        1. Deletes the corresponding lock key (`key_str(k)`) using DEL.
           If the key existed (DEL returns 1), it proceeds.
        2. Adds the key back to the tail of the free list using `push_to_tail()`
           with the specified expiry (calculated from ARGV[1] timeout).
        """
        return self.redis.register_script(f'''
        {self._lua_required_string}
        local timeout = tonumber(ARGV[1] or -1)
        local expiry = timeout_to_expiry(timeout)
        for i=2, #ARGV do
            local k = ARGV[i]
            local deleted = redis.call('DEL', key_str(k))
            if deleted > 0 then -- Only push back to pool if it was actually locked/deleted
                push_to_tail(k, expiry)
            end
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

    @cached_property
    def _get_free_list_script(self):
        return self.redis.register_script(f'''
        {self._lua_required_string}
        return get_free_list()
        ''')

    def get_free_list(self):
        return self._get_free_list_script()

    def _gc_cursor_str(self):
        """Get the Redis key for the garbage collection cursor.

        Returns:
            String representation of the Redis key for the GC cursor
        """
        return f'{self._pool_str()}|gc_cursor'

    @cached_property
    def _gc_script(self):
        """Cached Lua script for performing garbage collection.

        Uses HSCAN to iterate through the pool hash incrementally.
        Input ARGS: count (max items to scan per call)

        1. Gets the scan cursor from a dedicated key (`_gc_cursor_str()`).
        2. Calls HSCAN on the pool hash (`pool_str()`) starting from the cursor,
           requesting up to `count` items.
        3. Iterates through the key-value pairs returned by HSCAN.
        4. For each item, calls `check_item_health()` to reconcile its state
           (see `_lua_required_string` documentation).
        5. Saves the new cursor returned by HSCAN for the next GC call.
        """
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
        local t = ""
        for i = 1, #kvList, 2 do
            local itemName = kvList[i]
            -- local val      = kvList[i + 1]
            check_item_health(itemName)
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
