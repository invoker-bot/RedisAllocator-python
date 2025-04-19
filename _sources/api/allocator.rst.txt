Allocator Module
===============

.. module:: redis_allocator.allocator

This module provides resource allocation mechanisms using Redis as the backend.

RedisAllocator
-------------

.. autoclass:: RedisAllocator
   :members:
   :inherited-members:

RedisAllocatorObject
------------------

.. autocitten:: RedisAllocatorObject
   :members:

RedisAllocatableClass
-------------------

.. autoclass:: RedisAllocatableClass
   :members:

RedisThreadHealthCheckPool
-------------------------

.. autoclass:: RedisThreadHealthCheckPool
   :members:

Allocator
=========

.. automodule:: redis_allocator.allocator
   :members:
   :undoc-members:
   :show-inheritance:

Core Concepts & Visualization
-----------------------------

Allocator Pool Structure (Conceptual)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The allocator manages a pool of resources using several Redis keys:

*   **Pool Hash (`<prefix>|<suffix>|pool`)**: Stores the resource keys currently in the pool. The value for each key represents its position in a doubly-linked list used for the free list (e.g., `prev_key||next_key||expiry_timestamp`). A special value (`#ALLOCATED`) indicates the key is part of the pool but not currently free.
*   **Head Pointer (`...|pool|head`)**: A simple key holding the resource key currently at the head of the free list.
*   **Tail Pointer (`...|pool|tail`)**: A simple key holding the resource key currently at the tail of the free list.
*   **Lock Keys (`<prefix>|<suffix>:<key>`)**: Standard Redis keys used as locks in non-shared mode. Their existence indicates a resource is allocated.
*   **Soft Bind Cache Keys (`<prefix>|<suffix>-cache:bind:<name>`)**: Store the mapping between a logical name and an allocated resource key for soft binding.

.. mermaid::

   graph TD
       subgraph Redis Keys
           HKey["<prefix>|<suffix>|pool|head"] --> Key1["Key1: ""||Key2||Expiry"]
           TKey["<prefix>|<suffix>|pool|tail"] --> KeyN["KeyN: KeyN-1||""||Expiry"]
           PoolHash["<prefix>|<suffix>|pool (Hash)"]
       end

       subgraph "PoolHash Contents (Doubly-Linked Free List)"
           Key1 --> Key2["Key2: Key1||Key3||Expiry"]
           Key2 --> Key3["Key3: Key2||...||Expiry"]
           Key3 --> ...
           KeyN_1[...] --> KeyN
       end

       subgraph "Allocated Keys (Non-Shared Mode)"
           LKey1["<prefix>|<suffix>:AllocatedKey1"]
           LKeyX["<prefix>|<suffix>:AllocatedKeyX"]
       end

       subgraph "Soft Bind Cache"
           CacheKey1["<prefix>|<suffix>-cache:bind:name1"] --> LKey1
       end

Simplified Allocation Flow (Non-Shared Mode)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When `malloc` or `malloc_key` is called in non-shared mode:

1.  It checks if a name was provided for soft binding.
2.  If yes, it looks up the name in the soft bind cache.
3.  If a cached key is found and the corresponding lock key does *not* exist, the cached key is reused.
4.  Otherwise (no name, no valid cached key), it attempts to pop a key from the head of the free list (using the Pool Hash and Head Pointer).
5.  If a key is popped, a lock key is created for it with the specified timeout.
6.  If a name was provided, the soft bind cache is updated.
7.  The allocated key (or None) is returned.

.. mermaid::

   flowchart TD
       Start --> CheckSoftBind{Soft Bind Name Provided?}
       CheckSoftBind -- Yes --> GetBind{GET bind cache key}
       GetBind --> IsBoundKeyValid{"Cached Key Found and Unlocked?"}
       IsBoundKeyValid -- Yes --> ReturnCached[Return Cached Key]
       IsBoundKeyValid -- No --> PopHead{Pop Head from Free List (Lua pop_from_head)}
       CheckSoftBind -- No --> PopHead
       PopHead --> IsKeyFound{Key Found?}
       IsKeyFound -- Yes --> LockKey[SET Lock Key w/ Timeout]
       LockKey --> UpdateCache{"Update Bind Cache (if name provided)"}
       UpdateCache --> ReturnNewKey[Return New Key]
       IsKeyFound -- No --> ReturnNone[Return None]
       ReturnCached --> End
       ReturnNewKey --> End
       ReturnNone --> End

Simplified Free Flow (Non-Shared Mode)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When `free` or `free_keys` is called in non-shared mode:

1.  It attempts to delete the lock key (`<prefix>|<suffix>:<key>`).
2.  If the lock key existed and was deleted (DEL returns 1), the resource key is added back to the tail of the free list within the Pool Hash.

.. mermaid::

   flowchart TD
       Start --> DeleteLock{DEL Lock Key}
       DeleteLock --> Deleted{"Key Existed? (DEL > 0)"}
       Deleted -- Yes --> PushTail[Push Key to Free List Tail (Lua push_to_tail)]
       PushTail --> End
       Deleted -- No --> End

Shared Mode
-----------

In shared mode (`shared=True`), the flow is simpler:

*   **Allocation**: Pops a key from the head, but immediately pushes it back to the tail without creating a lock key. Soft binding still applies.
*   **Free**: Effectively a no-op, as no lock key was created during allocation. 