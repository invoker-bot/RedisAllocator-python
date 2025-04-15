"""RedisAllocator package for distributed memory allocation using Redis.

This package provides efficient, Redis-based distributed memory allocation
services that simulate traditional memory allocation mechanisms in a
distributed environment.
"""

from redis_allocator.lock import (RedisLock, RedisLockPool, LockStatus,
                                  BaseLock, BaseLockPool, ThreadLock, ThreadLockPool)
from redis_allocator.task_queue import TaskExecutePolicy, RedisTask, RedisTaskQueue
from redis_allocator.allocator import RedisAllocator, RedisAllocatorObject, RedisAllocatorUpdater, RedisAllocatorPolicy, DefaultRedisAllocatorPolicy


__version__ = '0.0.1'

__all__ = [
    'RedisLock',
    'RedisLockPool',
    'LockStatus',
    'BaseLock',
    'BaseLockPool',
    'ThreadLock',
    'ThreadLockPool',
    'TaskExecutePolicy',
    'RedisTask',
    'RedisTaskQueue',
    'RedisAllocator',
    'RedisAllocatorObject',
    'RedisAllocatorUpdater',
    'RedisAllocatorPolicy',
    'DefaultRedisAllocatorPolicy',
]
