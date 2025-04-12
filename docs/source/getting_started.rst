Getting Started
===============

This guide will help you get started with the RedisAllocator library. It covers the basic concepts 
and provides examples for the main functionality.

Basic Concepts
-------------

RedisAllocator is designed for distributed resource management using Redis as the backend. 
It provides several key components:

- **RedisLock**: Distributed locking mechanism
- **RedisAllocator**: Resource allocation and management
- **RedisTaskQueue**: Distributed task processing

Examples
-------

Using RedisLock for Distributed Locking
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

RedisLock provides a robust distributed locking mechanism with several important characteristics:

- **Single Redis Instance Support**: Works only with a single Redis instance, not with Redis Cluster
- **Automatic Expiry**: Locks automatically expire after a configured timeout to prevent deadlocks
- **Active Update Required**: Lock holders must actively update locks to maintain ownership
- **Thread Identification**: Locks can be associated with thread/process identifiers
- **Reentrant Locking**: Support for reentrant locks with the `rlock` method

.. code-block:: python

    from redis import Redis
    from redis_allocator import RedisLock
    import threading
    import time

    # Initialize Redis client (requires a single Redis instance)
    redis = Redis(host='localhost', port=6379, decode_responses=True)

    # Create a RedisLock instance
    lock = RedisLock(redis, "myapp", "resource-lock")

    # Use the current thread ID as the lock identifier
    thread_id = str(threading.get_ident())

    # Acquire a lock with a 60-second timeout
    if lock.lock("resource-123", value=thread_id, timeout=60):
        try:
            # Perform operations with the locked resource
            print("Resource locked successfully")
            
            # For long-running operations, periodically update the lock
            # to prevent timeout expiration
            for _ in range(5):
                time.sleep(10)  # Do some work
                
                # Extend the lock's lifetime by updating it
                lock.update("resource-123", value=thread_id, timeout=60)
                print("Lock updated, timeout extended")
                
            # Example of reentrant locking with rlock (succeeds because same thread_id)
            if lock.rlock("resource-123", value=thread_id):
                print("Successfully re-locked the resource")
        finally:
            # Release the lock when done
            lock.unlock("resource-123")
            print("Resource unlocked")
    else:
        print("Could not acquire lock - resource is busy")

Understanding Lock Timeouts and Updates
"""""""""""""""""""""""""""""""""""""""

The distributed lock mechanism is designed to prevent deadlocks in case of client failures:

1. When a lock is acquired, it has a specified timeout (in seconds) after which it's automatically released
2. Lock holders must periodically update the lock before the timeout expires to maintain ownership
3. If a client crashes or becomes unresponsive, the lock will be automatically released after the timeout
4. Other clients can acquire the lock after the timeout period expires

Thread Identification and Reentrant Locks
""""""""""""""""""""""""""""""""""""""""

When working with locks, proper thread identification is essential:

1. Use unique identifiers (like thread ID or process ID) as the lock value
2. The same thread/process can reacquire its own lock using the `rlock` method
3. Different threads/processes cannot acquire a lock while it's held by another
4. In distributed systems, ensure each node uses a globally unique identifier

Redis Instance Requirements
""""""""""""""""""""""""""

Important considerations regarding Redis deployment:

1. RedisLock requires a single Redis instance (standalone or master in master-replica setup)
2. It is not compatible with Redis Cluster, as cluster mode doesn't guarantee atomicity
3. For Redis Cluster environments, consider alternative solutions such as RedLock

Using RedisAllocator for Resource Management
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

RedisAllocator manages a pool of resources that can be allocated, freed, and garbage collected:

.. code-block:: python

    from redis import Redis
    from redis_allocator import RedisAllocator

    # Initialize Redis client
    redis = Redis(host='localhost', port=6379)

    # Create a RedisAllocator instance
    allocator = RedisAllocator(
        redis, 
        prefix='myapp',
        suffix='allocator',
        shared=False  # Whether resources can be shared
    )

    # Add resources to the pool
    allocator.extend(['resource-1', 'resource-2', 'resource-3'])

    # Allocate a resource key (returns only the key)
    key = allocator.malloc_key(timeout=120)
    if key:
        try:
            # Use the allocated resource
            print(f"Allocated resource: {key}")
        finally:
            # Free the resource when done
            allocator.free_keys(key)

    # Allocate a resource with object (returns a RedisAllocatorObject)
    allocated_obj = allocator.malloc(timeout=120)
    if allocated_obj:
        try:
            # The key is available as a property
            print(f"Allocated resource: {allocated_obj.key}")
            
            # Update the resource's lock timeout
            allocated_obj.update(timeout=60)
        finally:
            # Free the resource when done
            allocator.free(allocated_obj)

    # Using soft binding (associates a name with a resource)
    allocator.update_soft_bind("worker-1", "resource-1")
    # Later...
    allocator.unbind_soft_bind("worker-1")

    # Garbage collection (reclaims unused resources)
    allocator.gc(count=10)  # Check 10 items for cleanup

Using RedisTaskQueue for Distributed Task Processing
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

RedisTaskQueue enables distributed task processing across multiple workers:

.. code-block:: python

    from redis import Redis
    from redis_allocator import RedisTaskQueue, TaskExecutePolicy
    import json

    # Initialize Redis client
    redis = Redis(host='localhost', port=6379)

    # Process tasks in a worker
    def process_task(task):
        # Process the task (task is a RedisTask object)
        # You can access task.id, task.name, task.params
        # You can update progress with task.update(current, total)
        return json.dumps({"result": "processed"})

    # Create a task queue
    task_queue = RedisTaskQueue(redis, "myapp", task_fn=process_task)

    # Submit a task with query method
    result = task_queue.query(
        id="task-123",
        name="example-task",
        params={"input": "data"},
        timeout=300,  # Optional timeout in seconds
        policy=TaskExecutePolicy.Auto,  # Execution policy
        once=False  # Whether to delete the result after getting it
    )

    # Start listening for tasks
    task_queue.listen(
        names=["example-task"],  # List of task names to listen for
        workers=128,  # Number of worker threads
        event=None  # Optional event to signal when to stop listening
    )

Advanced Usage
-------------

For more advanced usage examples and the complete API reference, please refer to the :doc:`API Reference <api/index>`. 