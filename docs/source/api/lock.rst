Lock Module
==========

.. module:: redis_allocator.lock

This module provides distributed locking mechanisms using Redis as the backend.

Overview
--------

The lock module implements distributed locking patterns for coordinating access to shared resources 
in a distributed environment. It offers two primary implementations:

1. **Redis-based locks** (RedisLock, RedisLockPool): Distributed locks using Redis
2. **Thread-local locks** (ThreadLock, ThreadLockPool): In-memory locks for single-process applications

Key Features
^^^^^^^^^^^

- **Automatic Expiry**: Locks automatically expire after a configured timeout
- **Active Update**: Lock owners must periodically update locks to maintain ownership
- **Reentrant Locks**: Same owner can re-acquire its own locks with `rlock`
- **Lock Pools**: Manage collections of locks for allocation and tracking

Implementation Notes
^^^^^^^^^^^^^^^^^^

* Redis-based locks require a single Redis instance (not compatible with Redis Cluster)
* Locks have a timeout to prevent deadlocks in case of client failures
* Thread identification is used to determine lock ownership

RedisLock
--------

.. autoclass:: RedisLock
   :members:
   :inherited-members:

RedisLockPool
------------

.. autoclass:: RedisLockPool
   :members:
   :inherited-members:

LockStatus
---------

.. autoclass:: LockStatus
   :members:

Base Classes
-----------

.. autoclass:: BaseLock
   :members:

.. autoclass:: BaseLockPool
   :members:

Thread-Local Implementations
--------------------------

.. autoclass:: ThreadLock
   :members:

.. autoclass:: ThreadLockPool
   :members:

Usage Patterns
------------

**Basic Lock Usage**

.. code-block:: python

    lock = RedisLock(redis_client, "app", "lock")
    
    # Acquire lock with thread ID as value and 60-second timeout
    if lock.lock("resource-1", value=thread_id, timeout=60):
        try:
            # Resource is locked for 60 seconds
            # Process the resource...
            
            # Extend lock timeout by updating it
            lock.update("resource-1", value=thread_id, timeout=60)
            
            # Continue processing...
        finally:
            # Always release the lock when done
            lock.unlock("resource-1")

**Reentrant Lock Usage**

.. code-block:: python

    # First acquire the lock normally
    if lock.lock("resource-1", value=thread_id, timeout=60):
        try:
            # Later, the same thread can re-acquire the lock
            if lock.rlock("resource-1", value=thread_id):
                # Process the resource again...
                pass
        finally:
            # Release the lock once
            lock.unlock("resource-1")
            
**Using Lock Pools**

.. code-block:: python

    pool = RedisLockPool(redis_client, "app", "pool")
    
    # Add resources to the pool
    pool.extend(["resource-1", "resource-2"])
    
    # Lock a specific resource
    if pool.lock("resource-1"):
        try:
            # Use the resource
            pass
        finally:
            # Release the lock
            pool.unlock("resource-1") 