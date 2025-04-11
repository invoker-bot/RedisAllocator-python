"""Redis-based locking mechanisms for distributed coordination.

This module provides Redis-based locking classes that enable distributed
coordination and ensure data consistency across distributed processes.
"""
import time
import logging
import threading
from abc import ABC, ABCMeta, abstractmethod
from dataclasses import dataclass
from enum import IntEnum
from collections import defaultdict
from functools import cached_property
from datetime import timedelta, datetime
from typing import Iterable, Optional, Sequence, Tuple, Union, Any
from redis import StrictRedis as Redis

logger = logging.getLogger(__name__)
Timeout = Union[float, timedelta, None]


class LockStatus(IntEnum):
    """Enumeration representing the status of a Redis lock.

    The LockStatus enum defines the possible states of a Redis lock:

    - FREE: The lock is not being used.
    - UNAVAILABLE: The lock is being used by another program, or it has been marked as unavailable for a certain period of time.
    - LOCKED: The lock is being used by the current program.
    - ERROR: The lock is being used permanently, indicating a potential issue with the program.
    """
    FREE = 0x00
    UNAVAILABLE = 0x01
    LOCKED = 0x02
    ERROR = 0x04


class BaseLock(ABC):
    """Abstract base class defining the interface for lock implementations.

    Attributes:
        eps: Epsilon value for floating point comparison.
    """
    eps: float

    def __init__(self, eps: float = 1e-6):
        """Initialize a BaseLock instance.

        Args:
            eps: Epsilon value for floating point comparison.
        """
        self.eps = eps

    @abstractmethod
    def key_status(self, key: str, timeout: int = 120) -> LockStatus:
        """Get the status of a key.

        Args:
            key: The key to check the status of.
            timeout: The lock timeout in seconds.

        Returns:
            The current status of the key.
        """
        pass

    @abstractmethod
    def update(self, key: str, value='1', timeout: Timeout = 120):
        """Lock a key for a specified duration without checking if the key is already locked.

        Args:
            key: The key to lock.
            value: The value to set for the key.
            timeout: The lock timeout in seconds.
        """
        pass

    @abstractmethod
    def lock(self, key: str, value: str = '1', timeout: Timeout = 120) -> bool:
        """Try to lock a key for a specified duration.

        Args:
            key: The key to lock.
            value: The value to set for the key.
            timeout: The lock timeout in seconds.

        Returns:
            True if the ownership of the key is successfully acquired, False otherwise.
        """
        pass

    @abstractmethod
    def is_locked(self, key: str) -> bool:
        """Check if a key is locked.

        Args:
            key: The key to check.

        Returns:
            True if the key is locked, False otherwise.
        """
        pass

    @abstractmethod
    def lock_value(self, key: str) -> Optional[str]:
        """Get the value of a locked key.

        Args:
            key: The key to get the value of.

        Returns:
            The value of the key if the key is locked, None otherwise.
        """
        pass

    @abstractmethod
    def rlock(self, key: str, value: str = '1', timeout=120) -> bool:
        """Try to lock a key for a specified duration.

        When the value is the same as the current value, the function will return True.

        Args:
            key: The key to lock.
            value: The value to set for the key.
            timeout: The lock timeout in seconds.

        Returns:
            True if the ownership of the key is successfully acquired, False otherwise.
        """
        pass

    @abstractmethod
    def unlock(self, key: str) -> bool:
        """Forcefully release a key without checking if the key is locked.

        Args:
            key: The key to release.

        Returns:
            True if the key is successfully released, False if the key is not locked.
        """
        pass

    @abstractmethod
    def _conditional_setdel(self, op: str, key: str, value: float, set_value: Optional[float] = None,
                            ex: Optional[int] = None, isdel: bool = False) -> bool:
        """Conditionally set or del a key's value based on comparison with current value.

        Args:
            op: Comparison operator ('>', '<', '>=', '<=', '==', '!=').
            key: The key to set or delete.
            value: The value to compare with.
            set_value: The value to set, if None, will use value instead.
            ex: Optional expiration time in seconds.
            isdel: Whether to delete the key or set the value if the condition is met.

        Returns:
            Whether the operation was successful.
        """
        pass

    def setgt(self, key: str, value: float, set_value: Optional[float] = None, ex: Optional[int] = None) -> bool:
        """Sets a new value when the comparison value is greater than the current value."""
        return self._conditional_setdel('>', key, value, set_value, ex, False)

    def setlt(self, key: str, value: float, set_value: Optional[float] = None, ex: Optional[int] = None) -> bool:
        """Sets a new value when the comparison value is less than the current value."""
        return self._conditional_setdel('<', key, value, set_value, ex, False)

    def setge(self, key: str, value: float, set_value: Optional[float] = None, ex: Optional[int] = None) -> bool:
        """Sets a new value when the comparison value is greater than or equal to the current value."""
        return self._conditional_setdel('>=', key, value, set_value, ex, False)

    def setle(self, key: str, value: float, set_value: Optional[float] = None, ex: Optional[int] = None) -> bool:
        """Sets a new value when the comparison value is less than or equal to the current value."""
        return self._conditional_setdel('<=', key, value, set_value, ex, False)

    def seteq(self, key: str, value: float, set_value: Optional[float] = None, ex: Optional[int] = None) -> bool:
        """Sets a new value when the comparison value is equal to the current value."""
        return self._conditional_setdel('==', key, value, set_value, ex, False)

    def setne(self, key: str, value: float, set_value: Optional[float] = None, ex: Optional[int] = None) -> bool:
        """Sets a new value when the comparison value is not equal to the current value."""
        return self._conditional_setdel('!=', key, value, set_value, ex, False)

    def delgt(self, key: str, value: float):
        """Deletes a key when the comparison value is greater than the current value."""
        return self._conditional_setdel('>', key, value, None, None, True)

    def dellt(self, key: str, value: float):
        """Deletes a key when the comparison value is less than the current value."""
        return self._conditional_setdel('<', key, value, None, None, True)

    def delge(self, key: str, value: float):
        """Deletes a key when the comparison value is greater than or equal to the current value."""
        return self._conditional_setdel('>=', key, value, None, None, True)

    def delle(self, key: str, value: float):
        """Deletes a key when the comparison value is less than or equal to the current value."""
        return self._conditional_setdel('<=', key, value, None, None, True)

    def deleq(self, key: str, value: float):
        """Deletes a key when the comparison value is equal to the current value."""
        return self._conditional_setdel('==', key, value, None, None, True)

    def delne(self, key: str, value: float):
        """Deletes a key when the comparison value is not equal to the current value."""
        return self._conditional_setdel('!=', key, value, None, None, True)

    def _to_seconds(self, timeout: Timeout):
        """Convert a timeout to seconds."""
        if timeout is None:
            timeout = datetime(2099, 1, 1).timestamp()
        elif isinstance(timeout, timedelta):
            timeout = timeout.total_seconds()
        return timeout


class BaseLockPool(BaseLock, metaclass=ABCMeta):
    """Abstract base class defining the interface for lock pool implementations.

    A lock pool manages a collection of lock keys as a group, providing methods
    to track, add, remove, and check lock status of multiple keys.

    Attributes:
        eps: Epsilon value for floating point comparison.
    """

    @abstractmethod
    def extend(self, keys: Optional[Sequence[str]] = None):
        """Extend the pool with the specified keys."""
        pass

    @abstractmethod
    def shrink(self, keys: Sequence[str]):
        """Shrink the pool by removing the specified keys."""
        pass

    @abstractmethod
    def assign(self, keys: Optional[Sequence[str]] = None):
        """Assign keys to the pool, replacing any existing keys."""
        pass

    @abstractmethod
    def clear(self):
        """Empty the pool."""
        pass

    @abstractmethod
    def keys(self) -> Iterable[str]:
        """Get the keys in the pool."""
        pass

    @abstractmethod
    def _get_key_lock_status(self, keys: Iterable[str]) -> Iterable[bool]:
        """Get the lock status of the specified keys."""
        pass

    def values_lock_status(self) -> Iterable[bool]:
        """Get the lock status of all keys in the pool."""
        return self._get_key_lock_status(self.keys())

    def items_locked_status(self) -> Iterable[Tuple[str, bool]]:
        """Get (key, lock_status) pairs for all keys in the pool."""
        all_keys = list(self.keys())
        return zip(all_keys, self._get_key_lock_status(all_keys))

    def health_check(self) -> Tuple[int, int]:
        """Check the health status of the keys in the pool.

        Returns:
            A tuple of (locked_count, free_count)
        """
        items = list(self.values_lock_status())
        locked = sum(1 for item in items if item)
        free = len(items) - locked
        return locked, free

    def __len__(self):
        """Get the number of keys in the pool."""
        return len(list(self.keys()))

    def __iter__(self):
        """Iterate over the keys in the pool."""
        return iter(self.keys())


class RedisLock(BaseLock):
    """Redis-based lock implementation.

    Provides distributed locking capabilities using Redis as the backend storage.

    Attributes:
        redis: The Redis client instance.
        prefix: Prefix for Redis keys.
        suffix: Suffix for Redis keys.
        eps: Epsilon value for floating point comparison.
    """

    redis: Redis
    prefix: str
    suffix: str

    def __init__(self, redis: Redis, prefix: str, suffix="lock", eps: float = 1e-6):
        """Initialize a RedisLock instance.

        Args:
            redis: Redis client instance.
            prefix: Prefix for Redis keys.
            suffix: Suffix for Redis keys.
            eps: Epsilon value for floating point comparison.
        """
        assert "'" not in prefix and "'" not in suffix, "Prefix and suffix cannot contain single quotes"
        assert redis.get_encoder().decode_responses, "Redis must be configured to decode responses"
        super().__init__(eps=eps)
        self.redis = redis
        self.prefix = prefix
        self.suffix = suffix

    @property
    def _lua_required_string(self):
        return f'''
        local function key_str(key)
            return '{self.prefix}|{self.suffix}:' .. key
        end
        '''

    def _key_str(self, key: str):
        return f'{self.prefix}|{self.suffix}:{key}'

    def key_status(self, key: str, timeout: int = 120) -> LockStatus:
        ttl = self.redis.ttl(self._key_str(key))
        if ttl > timeout:  # If TTL is greater than the required expiration time, it means the usage is incorrect
            return LockStatus.UNAVAILABLE
        elif ttl >= 0:
            return LockStatus.LOCKED
        elif ttl == -1:
            return LockStatus.ERROR  # Permanent lock
        return LockStatus.FREE

    def update(self, key: str, value='1', timeout: Timeout = 120):
        self.redis.set(self._key_str(key), value, ex=timeout)

    def lock(self, key: str, value: str = '1', timeout: Timeout = 120) -> bool:
        key_str = self._key_str(key)
        return self.redis.set(key_str, value, ex=timeout, nx=True)

    def is_locked(self, key: str) -> bool:
        return self.redis.exists(self._key_str(key))

    def lock_value(self, key: str) -> Optional[str]:
        return self.redis.get(self._key_str(key))

    def rlock(self, key: str, value: str = '1', timeout=120) -> bool:
        key_str = self._key_str(key)
        old_value = self.redis.set(key_str, value, ex=timeout, nx=True, get=True)
        return old_value is None or old_value == value

    def unlock(self, key: str) -> bool:
        return bool(self.redis.delete(self._key_str(key)))

    def _conditional_setdel(self, op: str, key: str, value: float, set_value: Optional[float] = None, ex: Optional[int] = None,
                            isdel: bool = False) -> bool:
        # Convert None to a valid value for Redis (using -1 to indicate no expiration)
        key_value = self._key_str(key)
        ex_value = -1 if ex is None else ex
        isdel_value = '1' if isdel else '0'
        if set_value is None:
            set_value = value
        return self._conditional_setdel_script[op](keys=[key_value],
                                                   args=[value, set_value, ex_value, isdel_value])

    @cached_property
    def _conditional_setdel_script(self):
        return {op: self.redis.register_script(self._conditional_setdel_lua_script(op, self.eps)) for op in
                ('>', '<', '>=', '<=', '==', '!=')}

    def _conditional_setdel_lua_script(self, op: str, eps: float = 1e-6) -> str:
        match op:
            case '>':
                condition = 'compare_value > current_value'
            case '<':
                condition = 'compare_value < current_value'
            case '>=':
                condition = f'compare_value >= current_value - {eps}'
            case '<=':
                condition = f'compare_value <= current_value + {eps}'
            case '==':
                condition = f'abs(compare_value - current_value) < {eps}'
            case '!=':
                condition = f'abs(compare_value - current_value) > {eps}'
            case _:
                raise ValueError(f"Invalid operator: {op}")
        return f'''
        {self._lua_required_string}
        local abs = math.abs
        local current_key = KEYS[1]
        local current_value = tonumber(redis.call('GET', current_key))
        local compare_value = tonumber(ARGV[1])
        local new_value = tonumber(ARGV[2])
        local ex = tonumber(ARGV[3])
        local isdel = ARGV[4] ~= '0'
        if current_value == nil or {condition} then
            if isdel then
                redis.call('DEL', current_key)
            else
                if ex ~= nil and ex > 0 then
                    redis.call('SET', current_key, new_value, 'EX', ex)
                else
                    redis.call('SET', current_key, new_value)
                end
            end
            return true
        end
        return false
        '''

    def __eq__(self, value: Any) -> bool:
        if isinstance(value, RedisLock):
            return self.prefix == value.prefix and self.suffix == value.suffix
        return False

    def __hash__(self) -> int:
        return hash((self.prefix, self.suffix))


class RedisLockPool(RedisLock, BaseLockPool):
    """Redis-based lock pool implementation.

    Manages a pool of Redis locks, stored as a Redis set.
    """

    def __init__(self, redis: Redis, prefix: str, suffix='lock-pool', eps: float = 1e-6):
        """Initialize a RedisLockPool instance.

        Args:
            redis: Redis client instance.
            prefix: Prefix for Redis keys.
            suffix: Suffix for Redis keys.
            eps: Epsilon value for floating point comparison.
        """
        super().__init__(redis, prefix, suffix=suffix, eps=eps)
        assert redis.get_encoder().decode_responses, "Redis must be configured to decode responses"

    @property
    def _lua_required_string(self):
        return f'''
        {super()._lua_required_string}
        local function pool_str()
            return '{self._pool_str()}'
        end
        '''

    def _pool_str(self):
        """Returns the Redis key for the pool."""
        return f'{self.prefix}|{self.suffix}|pool'

    def extend(self, keys: Optional[Sequence[str]] = None):
        """Extend the pool with the specified keys."""
        if keys is not None and len(keys) > 0:
            self.redis.sadd(self._pool_str(), *keys)

    def shrink(self, keys: Sequence[str]):
        """Shrink the pool by removing the specified keys."""
        if keys is not None and len(keys) > 0:
            self.redis.srem(self._pool_str(), *keys)

    @property
    def _assign_lua_string(self):
        return f'''
        {self._lua_required_string}
        redis.call('DEL', pool_str())
        redis.call('SADD', pool_str(), unpack(ARGV))
        '''

    @cached_property
    def _assign_lua_script(self):
        return self.redis.register_script(self._assign_lua_string)

    def assign(self, keys: Optional[Sequence[str]] = None):
        """Assign keys to the pool, replacing any existing keys."""
        if keys is not None and len(keys) > 0:
            self._assign_lua_script(args=keys)
        else:
            self.clear()

    def clear(self):
        """Empty the pool."""
        self.redis.delete(self._pool_str())

    def keys(self) -> Iterable[str]:
        """Get the keys in the pool."""
        return self.redis.smembers(self._pool_str())

    def __contains__(self, key):
        """Check if a key is in the pool."""
        return self.redis.sismember(self._pool_str(), key)

    def _get_key_lock_status(self, keys: Iterable[str]) -> Iterable[bool]:
        """Get the lock status of the specified keys."""
        return map(lambda x: x is not None, self.redis.mget(map(self._key_str, keys)))


@dataclass
class LockData:
    """Data structure to store lock information.

    Attributes:
        value: The lock value.
        expiry: The expiration timestamp.
    """
    value: str
    expiry: float


class ThreadLock(BaseLock):
    """Thread-safe lock implementation for local process use.

    Provides similar functionality to RedisLock but works locally
    within a single Python process using threading mechanisms.

    Attributes:
        eps: Epsilon value for floating point comparison.
    """

    def __init__(self, eps: float = 1e-6):
        """Initialize a ThreadLock instance.

        Args:
            eps: Epsilon value for floating point comparison.
        """
        super().__init__(eps=eps)
        self._locks = defaultdict(lambda: LockData(value='1', expiry=0))
        self._lock = threading.RLock()  # Thread lock to protect access to _locks

    def _is_expired(self, key: str) -> bool:
        """Check if a lock has expired."""
        return self._get_ttl(key) <= 0

    def _get_ttl(self, key: str):
        """Get the TTL of a lock in seconds."""
        return self._locks[key].expiry - time.time()

    def key_status(self, key: str, timeout: int = 120) -> LockStatus:
        """Get the status of a key."""
        ttl = self._get_ttl(key)
        if ttl <= 0:
            return LockStatus.FREE
        elif ttl > timeout:
            return LockStatus.UNAVAILABLE
        return LockStatus.LOCKED

    def update(self, key: str, value='1', timeout: Timeout = 120):
        """Lock a key for a specified duration without checking if already locked."""
        expiry = time.time() + self._to_seconds(timeout)
        self._locks[key] = LockData(value=value, expiry=expiry)

    def lock(self, key: str, value: str = '1', timeout: Timeout = 120) -> bool:
        """Try to lock a key for a specified duration."""
        with self._lock:
            if not self._is_expired(key):
                return False
            expiry = time.time() + self._to_seconds(timeout)
            self._locks[key] = LockData(value=value, expiry=expiry)
            return True

    def is_locked(self, key: str) -> bool:
        """Check if a key is locked."""
        return not self._is_expired(key)

    def lock_value(self, key: str) -> Optional[str]:
        """Get the value of a locked key."""
        data = self._locks[key]
        if data.expiry <= time.time():
            return None
        return str(data.value)

    def rlock(self, key: str, value: str = '1', timeout=120) -> bool:
        """Try to relock a key for a specified duration."""
        with self._lock:
            data = self._locks[key]
            if data.expiry > time.time() and data.value != value:
                return False
            expiry = time.time() + self._to_seconds(timeout)
            self._locks[key] = LockData(value=value, expiry=expiry)
            return True

    def unlock(self, key: str) -> bool:
        """Forcefully release a key."""
        return self._locks.pop(key, None) is not None

    def _compare_values(self, op: str, compare_value: float, current_value: float) -> bool:
        """Compare two values using the specified operator."""
        match op:
            case '>':
                return compare_value > current_value
            case '<':
                return compare_value < current_value
            case '>=':
                return compare_value >= current_value - self.eps
            case '<=':
                return compare_value <= current_value + self.eps
            case '==':
                return abs(compare_value - current_value) < self.eps
            case '!=':
                return abs(compare_value - current_value) > self.eps
            case _:
                raise ValueError(f"Invalid operator: {op}")

    def _conditional_setdel(self, op: str, key: str, value: float, set_value: Optional[float] = None,
                            ex: Optional[int] = None, isdel: bool = False) -> bool:
        """Conditionally set or delete a key's value based on comparison."""
        compare_value = float(value)
        if set_value is None:
            set_value = value

        with self._lock:
            # Get current value if key exists and is not expired
            current_data = self._locks[key]
            if current_data.expiry <= time.time():
                current_value = None
            else:
                current_value = float(current_data.value)

            # Condition check
            if current_value is None or self._compare_values(op, compare_value, current_value):
                if isdel:
                    # Delete the key
                    self._locks.pop(key, None)
                else:
                    # Set the key
                    expiry = time.time() + self._to_seconds(ex)
                    self._locks[key] = LockData(value=set_value, expiry=expiry)
                return True
            return False


class ThreadLockPool(ThreadLock, BaseLockPool):
    """Thread-safe lock pool implementation for local process use.

    Maintains a set of keys as the pool and provides operations to manage them.
    """

    def __init__(self, eps: float = 1e-6):
        """Initialize a ThreadLockPool instance."""
        super().__init__(eps=eps)
        self._locks = defaultdict(lambda: LockData(value='1', expiry=0))
        self._lock = threading.RLock()
        self._pool = set()

    def extend(self, keys: Optional[Sequence[str]] = None):
        """Extend the pool with the specified keys."""
        with self._lock:
            if keys is not None:
                self._pool.update(keys)

    def shrink(self, keys: Sequence[str]):
        """Shrink the pool by removing the specified keys."""
        with self._lock:
            self._pool.difference_update(keys)

    def assign(self, keys: Optional[Sequence[str]] = None):
        """Assign keys to the pool, replacing any existing keys."""
        with self._lock:
            self.clear()
            self.extend(keys=keys)

    def clear(self):
        """Empty the pool."""
        self._pool.clear()

    def keys(self) -> Iterable[str]:
        """Get the keys in the pool."""
        return self._pool

    def __contains__(self, key):
        """Check if a key is in the pool."""
        return key in self._pool

    def _get_key_lock_status(self, keys: Iterable[str]) -> Iterable[bool]:
        """Get the lock status of the specified keys."""
        return [self.is_locked(key) for key in keys]
