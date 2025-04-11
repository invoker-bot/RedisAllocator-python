Lock Module
==========

.. module:: redis_allocator.lock

This module provides distributed locking mechanisms using Redis as the backend.

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