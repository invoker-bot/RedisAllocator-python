Task Queue Module
===============

.. module:: redis_allocator.task_queue

This module provides a task queue system for distributed task processing using Redis as the backend.

RedisTaskQueue
------------

.. autoclass:: RedisTaskQueue
   :members:

RedisTask
--------

.. autoclass:: RedisTask
   :members:

TaskExecutePolicy
---------------

.. autocitten:: TaskExecutePolicy
   :members: 

Task Queue
==========

.. automodule:: redis_allocator.task_queue
   :members:
   :undoc-members:
   :show-inheritance:

Simplified Task Queue Flow
--------------------------

This diagram shows the interaction between a Client submitting a task, the `RedisTaskQueue`,
Redis itself, and a `Listener` processing the task.

1.  **Submission**: The client calls `query()`. The `RedisTaskQueue` serializes the task data
    and stores it in a Redis key (`result:<id>`) with an expiry. It then pushes the task ID
    onto the corresponding queue list (`queue:<name>`).
2.  **Listening**: A listener calls `listen()` and performs a blocking pop (`BLPOP`) on the queue list.
3.  **Processing**: When a task ID is popped, the listener retrieves the task data from Redis,
    executes the user-defined `task_fn`, and updates the task data in Redis with the result or error.
4.  **Result Retrieval**: The original client can periodically call `get_task()` to fetch the updated
    task data and retrieve the result or error.

.. mermaid::

   sequenceDiagram
       participant Client
       participant TaskQueue
       participant Redis
       participant Listener

       Client->>TaskQueue: query(id, name, params)
       note right of Redis: Stores task state: SETEX result:<id> pickled_task
       TaskQueue->>Redis: SETEX result:<id> pickled_task
       note right of Redis: Adds ID to queue: RPUSH queue:<name> <id>
       TaskQueue->>Redis: RPUSH queue:<name> <id>
       Redis-->>TaskQueue: OK
       TaskQueue-->>Client: (Waits or returns if local)

       Listener->>TaskQueue: listen([name])
       loop Poll Queue
           note over TaskQueue,Redis: BLPOP queue:<name> timeout
           TaskQueue->>Redis: BLPOP queue:<name> timeout
           alt Task Available
               Redis-->>TaskQueue: [queue:<name>, <id>]
               note over TaskQueue,Redis: GET result:<id>
               TaskQueue->>Redis: GET result:<id>
               Redis-->>TaskQueue: pickled_task
               TaskQueue->>Listener: Execute task_fn(task)
               Listener-->>TaskQueue: result/error
               note right of Redis: Updates task state: SETEX result:<id> updated_pickled_task
               TaskQueue->>Redis: SETEX result:<id> updated_pickled_task
           else Timeout
               Redis-->>TaskQueue: nil
           end
       end

       Client->>TaskQueue: get_task(id) (Periodically or when notified)
       note over TaskQueue,Redis: GET result:<id>
       TaskQueue->>Redis: GET result:<id>
       Redis-->>TaskQueue: updated_pickled_task
       TaskQueue-->>Client: Task result/error 