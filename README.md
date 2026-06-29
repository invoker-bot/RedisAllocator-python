# RedisAllocator

## Project Overview

RedisAllocator provides robust and efficient components for managing distributed resources using Redis. It's designed specifically for scenarios requiring high availability, automatic recovery, and flexible allocation strategies, such as managing pools of proxies, workers, or other limited resources.

The core philosophy is to leverage Redis's speed and atomic Lua scripting capabilities to ensure consistency and performance for resource allocation, locking, and task queuing, while operating within a single Redis instance for simplicity and atomicity guarantees.

> **Note**: RedisAllocator is optimized for single Redis instance deployments. Its reliance on Lua scripting for atomicity makes it unsuitable for standard Redis Cluster configurations. For cluster environments, consider alternative locking mechanisms like RedLock.

## Core Design Principles & Features

RedisAllocator is built around these key ideas:

-   **Efficient Resource Pooling:** Manages a pool of available resources, enabling clients to check out (allocate) and return (free) resources.
-   **Atomic Operations:** Utilizes Redis Lua scripts extensively to guarantee atomicity for critical pool management operations, preventing race conditions in distributed environments.
-   **Automatic Recovery (Garbage Collection):** Implements configurable garbage collection to automatically detect and recycle resources that are no longer in use (e.g., due to client crashes or expired locks), crucial for maintaining pool health.
-   **Flexible Allocation Modes:** Supports both **exclusive** (`shared=False`, default) allocation where a resource is locked for one client, and **shared** (`shared=True`) allocation where multiple clients can use the same resource concurrently.
-   **Resource Prioritization (Planned):** A key upcoming feature allowing resources to be allocated based on defined priorities (e.g., allocating faster proxies first), using Redis Sorted Sets.
-   **Resource Affinity (Soft Binding):** Allows associating specific names (e.g., a worker ID or a specific task type) with resources, enabling consistent reuse of the same resource for that name, useful for caching or specialized tasks.
-   **Distributed Locking:** Provides a standalone, robust distributed lock (`RedisLock`) with automatic expiry and reentrancy support.
-   **Task Queuing:** Includes a basic distributed task queue (`RedisTaskQueue`) for coordinating work among multiple consumers.

### Core Features

- **Distributed Locking**: Provides robust distributed locking mechanisms to ensure data consistency in concurrent environments
- **Resource Allocation**: Implements a distributed resource allocation system with support for:
  - Priority-based distribution
  - Soft binding
  - Garbage collection
  - Health checking
- **Task Management**: Implements a distributed task queue system for efficient task processing across multiple workers
- **Object Allocation**: Supports allocation of resources with priority-based distribution and soft binding
- **Health Checking**: Monitors the health of distributed instances and automatically handles unhealthy resources
- **Garbage Collection**: Automatically identifies and reclaims unused resources, optimizing memory usage
- **Shared Mode**: Configurable allocation modes supporting both exclusive and shared resource usage
- **Soft Binding**: Associates named objects with specific resources for consistent allocation

## Behavior Contracts (Important for API Users)

A few intentional behaviors that may surprise newcomers — knowing them up-front avoids debugging dead-ends:

- **`malloc_key(timeout<=0)` creates a permanent (non-expiring) lock.** Useful for resources whose ownership must outlive process restarts; the caller takes responsibility for releasing via `free_keys`. GC never reclaims it (because expiry is also `-1`). Misuse causes resource leaks — only pass `timeout=0` when you mean "I will manage this lock myself."
- **`cache_timeout=0` (or negative) creates a permanent soft binding.** Sticks until explicitly overwritten by another `update_soft_bind` for the same name, or removed via `unbind_soft_bind`. Symmetric to the lock-timeout contract above.
- **`malloc()` and `malloc_key()` return `None` when no resource is available.** Check the return value before using it; an empty pool is not represented as a wrapper with `key=None`.
- **Prefer `with allocator.malloc(...) as allocation:` or `allocation.release()` for object allocations.** `release()` closes the wrapped object and returns the key to the pool. Plain `close()` only closes the wrapped object and leaves allocation ownership unchanged for callers that manage `free()` manually.
- **`assign()` does not delete the lock keys of items it evicts.** A deliberate write-amplification trade-off: locks expire on their own TTL, and if the same key is later re-`extend`-ed into the pool, the new `extend`/`assign` clears any residual lock so the new entry starts clean. If you evict a key and never re-add it, the lock is an orphan — callers who care must `DEL <prefix>|<suffix>:<key>` themselves.
- **Pool keys are validated at the API boundary.** `extend()` / `assign()` raise `ValueError` for keys that are empty, equal to the literal `"#ALLOCATED"`, contain the `"||"` separator, contain NUL bytes, or are not strings. These shapes break the on-disk encoding and would silently corrupt the linked list.

## Documentation

For complete documentation, please visit our [official documentation site](https://invoker-bot.github.io/RedisAllocator-python/).

## Installation

```bash
pip install redis-allocator
```

## Quick Start

### Using RedisLock for Distributed Locking

RedisLock provides distributed locking with the following important characteristics:

- **Automatic Expiry**: Locks are automatically released after a timeout period, preventing deadlocks when clients fail
- **Active Update Required**: Lock holders must actively update their locks to maintain ownership
- **Thread Identification**: Each lock can include a thread identifier to determine ownership
- **Reentrant Locking**: Same thread/process can reacquire its own locks using the rlock method

```python
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
```

**Key Concepts:**
- If a lock holder fails to update within the timeout period, the lock is automatically released
- Using `rlock()` allows the same thread to reacquire a lock it already holds
- This implementation only works with a single Redis instance (not Redis Cluster)
- In a distributed system, each node should use a unique identifier as the lock value

**Simplified Lock Flow:**

```mermaid
sequenceDiagram
    participant Client
    participant RedisLock
    participant Redis

    Client->>RedisLock: lock("key", "id", timeout=60)
    RedisLock->>Redis: SET key id NX EX 60
    alt Lock Acquired
        Redis-->>RedisLock: OK
        RedisLock-->>Client: True
        Client->>RedisLock: update("key", "id", timeout=60)
        RedisLock->>Redis: SET key id EX 60
        Redis-->>RedisLock: OK
        Client->>RedisLock: unlock("key")
        RedisLock->>Redis: DEL key
        Redis-->>RedisLock: 1 (deleted)
        RedisLock-->>Client: True
    else Lock Not Acquired (Already Locked)
        Redis-->>RedisLock: nil
        RedisLock-->>Client: False
    end
```

### Using RedisAllocator for Resource Management

```python
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
    with allocated_obj:
        # The key is available as a property
        print(f"Allocated resource: {allocated_obj.key}")
        
        # Update the resource's lock timeout
        allocated_obj.update(timeout=60)
    # Leaving the with block calls allocated_obj.release().

# Using soft binding (associates a name with a resource)
allocator.update_soft_bind("worker-1", "resource-1")
# Unbind soft binding
allocator.unbind_soft_bind("worker-1")

# Garbage collection (reclaims unused resources)
allocator.gc(count=10)  # Check 10 items for cleanup
```

### Shared Mode vs Non-Shared Mode

RedisAllocator supports two allocation modes:

#### Non-shared Mode (default, `shared=False`)
- Resources are allocated exclusively to one client/thread
- When allocated, the resource is locked, preventing others from using it
- The resource remains locked until explicitly freed or until its timeout expires
- Ideal for scenarios where resources must be used exclusively

```python
# Non-shared allocator (exclusive resource usage)
exclusive_allocator = RedisAllocator(redis, "myapp", shared=False)

# When a resource is allocated, it's locked and cannot be allocated by others
key = exclusive_allocator.malloc_key(timeout=120)
if key:
    # Only this client can use the key until it's freed or timeout expires
    exclusive_allocator.free_keys(key)
```

#### Shared Mode (`shared=True`)
- Resources can be used concurrently by multiple clients/threads
- When allocated, the resource is made available from the free list but not locked
- Multiple clients can allocate and use the same resource simultaneously
- Ideal for read-only resources or resources that support concurrent access

```python
# Shared allocator (concurrent resource usage)
shared_allocator = RedisAllocator(redis, "myapp", shared=True)

# Resources can be accessed by multiple clients simultaneously
key = shared_allocator.malloc_key(timeout=120)
if key:
    # Other clients can also allocate and use this same key
    shared_allocator.free_keys(key)
```

### Soft Binding Mechanism

Soft binding creates persistent associations between named objects and allocated resources:

**Allocator Pool Structure (Conceptual):**

```mermaid
graph TD
    subgraph Redis Keys
        HKey["<prefix>|<suffix>|pool|head"] --> Key1["Key1: &quot;&quot;||Key2||Expiry"]
        TKey["<prefix>|<suffix>|pool|tail"] --> KeyN["KeyN: KeyN-1||&quot;&quot;||Expiry"]
        PoolHash["<prefix>|<suffix>|pool (Hash)"]
    end

    subgraph "PoolHash Contents (Doubly-Linked Free List)"
        Key1 --> Key2["Key2: Key1||Key3||Expiry"]
        Key2 --> Key3["Key3: Key2||Key4||Expiry"]
        Key3 --> Key4["Key4: Key3||Key5||Expiry"]
        KeyN_1["KeyN: KeyN-1||&quot;&quot;||Expiry"] --> KeyN
    end

    subgraph "Allocated Keys (Non-Shared Mode)"
        LKey1["<prefix>|<suffix>:AllocatedKey1"]
        LKeyX["<prefix>|<suffix>:AllocatedKeyX"]
    end

    subgraph "Soft Bind Cache"
        CacheKey1["<prefix>|<suffix>-cache:bind:name1"] --> AllocatedKey1
    end
```

**Simplified Allocation Flow (Non-Shared Mode):**

```mermaid
flowchart TD
    Start --> CheckSoftBind{Soft Bind Name Provided?}
    CheckSoftBind -- Yes --> GetBind{GET bind cache key}
    GetBind --> IsBoundKeyValid{"Cached Key Found and Unlocked?"}
    IsBoundKeyValid -- Yes --> ReturnCached[Return Cached Key]
    IsBoundKeyValid -- No --> PopHead{Pop Head from Free List}
    CheckSoftBind -- No --> PopHead
    PopHead --> IsKeyFound{Key Found?}
    IsKeyFound -- Yes --> LockKey[SET Lock Key w/ Timeout]
    LockKey --> UpdateCache{"Update Bind Cache (if name provided)"}
    UpdateCache --> ReturnNewKey[Return New Key]
    IsKeyFound -- No --> ReturnNone[Return None]
    ReturnCached --> End
    ReturnNewKey --> End
    ReturnNone --> End
```

**Simplified Free Flow (Non-Shared Mode):**

```mermaid
flowchart TD
    Start --> DeleteLock{DEL Lock Key}
    DeleteLock --> Deleted{"Key Existed? (DEL > 0)"}
    Deleted -- Yes --> PushTail[Push Key to Free List Tail]
    PushTail --> End
    Deleted -- No --> End
```

```python
from redis import Redis
from redis_allocator import RedisAllocator, RedisAllocatableClass

# Create a custom allocatable class with a name
class MyResource(RedisAllocatableClass):
    def __init__(self, resource_name):
        self._name = resource_name
    
    def set_config(self, key, params):
        # Configure the resource when allocated
        self.key = key
        self.config = params
    
    @property
    def name(self):
        # Name used for soft binding
        return self._name

# Initialize allocator
redis = Redis(host='localhost', port=6379)
allocator = RedisAllocator(redis, "myapp")

# Add resources to the pool
allocator.extend(['resource-1', 'resource-2', 'resource-3'])

# Create a named resource object
resource = MyResource("database-connection")

# First allocation will assign a key from the pool
allocation1 = allocator.malloc(timeout=60, obj=resource)
print(f"First allocation: {allocation1.key}")  # e.g., "resource-1"

# Free the resource
allocator.free(allocation1)

# Later allocation of the same named object will try to reuse the same key
# Can specify a custom cache timeout for the binding
allocation2 = allocator.malloc(timeout=60, obj=resource, cache_timeout=300)
print(f"Second allocation: {allocation2.key}")  # Will be "resource-1" again

# Benefits of soft binding:
# 1. Resource affinity - same object gets same resource consistently
# 2. Optimized caching and resource reuse
# 3. Predictable resource mapping for debugging
```

Key features of soft binding:
- Bindings persist even after the resource is freed, with a configurable timeout
- If a bound resource is no longer available, a new resource is automatically allocated
- Explicit unbinding is available with `unbind_soft_bind(name)`
- Soft bindings have their own timeout (default 3600 seconds) separate from resource locks

### Using RedisTaskQueue for Distributed Task Processing

**Simplified Task Queue Flow:**

```mermaid
sequenceDiagram
    participant Client
    participant TaskQueue
    participant Redis
    participant Listener

    Client->>TaskQueue: query(id, name, params)
    TaskQueue->>Redis: SETEX result:<id> pickled_task
    TaskQueue->>Redis: RPUSH queue:<name> <id>
    Redis-->>TaskQueue: OK
    TaskQueue-->>Client: (Waits or returns if local)

    Listener->>TaskQueue: listen([name])
    loop Poll Queue
        TaskQueue->>Redis: BLPOP queue:<name> timeout
        alt Task Available
            Redis-->>TaskQueue: [queue:<name>, <id>]
            TaskQueue->>Redis: GET result:<id>
            Redis-->>TaskQueue: pickled_task
            TaskQueue->>Listener: Execute task_fn(task)
            Listener-->>TaskQueue: result/error
            TaskQueue->>Redis: SETEX result:<id> updated_pickled_task
        else Timeout
            Redis-->>TaskQueue: nil
        end
    end

    Client->>TaskQueue: get_task(id) (Periodically or when notified)
    TaskQueue->>Redis: GET result:<id>
    Redis-->>TaskQueue: updated_pickled_task
    TaskQueue-->>Client: Task result/error
```

```python
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
    timeout=300,  # End-to-end wait timeout in seconds for remote execution
    policy=TaskExecutePolicy.Auto,  # Execution policy
    once=False  # Whether to delete the result after getting it
)

# Start listening for tasks
task_queue.listen(
    names=["example-task"],  # List of task names to listen for
    workers=128,  # Number of worker threads
    event=None  # Optional event to signal when to stop listening
)
```

## Modules

RedisAllocator consists of several modules, each providing specific functionality:

- **lock.py**: Provides `RedisLock` and `RedisLockPool` for distributed locking mechanisms
- **task_queue.py**: Implements `RedisTaskQueue` for distributed task processing
- **allocator.py**: Contains `RedisAllocator` and related classes for resource allocation

*(Note: Internal comments and Lua script explanations within these modules have been recently refactored for better clarity.)*

### RedisAllocator Architecture

The RedisAllocator maintains resources in a doubly-linked list structure stored in Redis:
- Available resources are kept in a "free list"
- In non-shared mode, allocated resources are removed from the free list and locked
- In shared mode, allocated resources are still available for allocation by others
- The Garbage Collector periodically:
  - Reclaims locked resources whose locks have expired
  - Removes expired resources based on their configured timeouts
  - Cleans up inconsistent states between allocations and locks
- Soft bindings are implemented as separate locks with their own timeout period

### Concurrency Safety

Each EVAL is atomic on Redis (single-threaded server, Lua never preempted), so no operation can be observed half-completed. Multi-process coordination is then provided by lock TTL plus the GC pass — a process killed mid-sequence leaves at most an orphaned lock, which expires on its TTL or is reclaimed by GC.

The pool linked-list invariants survive arbitrary interleaving of `malloc` / `free` / `extend` / `assign` / `gc` from concurrent processes (validated by both fakeredis multi-thread tests and a Docker-backed stress driver — see Testing below). If you observe persistent invariant violations under load, please file an issue with the operation sequence.

#### Atomic Operation Boundaries

Allocator state transitions are written as single Redis Lua scripts. Redis runs each script atomically on the server event loop, so concurrent processes cannot observe a half-updated free-list node, lock key, or soft-binding update. This guarantee depends on using one Redis instance; Redis Cluster is out of scope because the allocator mutates multiple related keys in one script.

| Operation | Atomic boundary | Notes |
|---|---|---|
| `malloc_key()` | One Lua EVAL | Checks soft binding, pops/rotates the free-list head, marks the item allocated, creates the lock key, and refreshes binding state together. |
| `free_keys()` | One Lua EVAL | Deletes lock keys and returns still-present pool members to the free-list tail together. Unknown or evicted keys are no-ops. |
| `extend()` / `shrink()` / `assign()` | One Lua EVAL per call | Batch updates are atomic as a batch. `assign()` intentionally preserves lock keys for evicted members. |
| `gc()` | One Lua EVAL per pass | Incrementally reconciles up to the requested scan count and saves the scan cursor. |
| `malloc()` / `RedisAllocatorObject.release()` | Wrapper around atomic calls | `malloc()` wraps `malloc_key()` after the atomic allocation; `release()` wraps `free_keys()` after closing the local object. |

Multi-process coordination is provided by those atomic scripts plus lock TTLs and GC. A process killed after allocation may leave a lock behind; that lock either expires naturally or is reconciled by GC. Permanent locks (`timeout <= 0`) are never reclaimed automatically and must be released explicitly.

### Operation Complexity

The hot path is designed to avoid scanning the whole pool. Use `malloc_key()` / `free_keys()` for high-frequency allocation, use `extend()` for incremental pool refreshes, and reserve `assign()` for lower-frequency wholesale replacement.

| Operation | Time complexity | Space / Redis writes | Recommended use |
|---|---:|---|---|
| `malloc_key()` | Amortized `O(1)`; worst-case `O(k)` when skipping `k` expired or locked head entries | Constant extra metadata writes | Hot path allocation. |
| `free_keys(n)` | `O(n)` | Constant writes per released key | Hot path release; single-key free is `O(1)`. |
| `extend(n)` | `O(n)`, independent of existing pool size | Constant writes per supplied key | Incremental pool additions or expiry refresh. |
| `shrink(n)` | `O(n)` | Constant writes per supplied key | Remove known resources. |
| `assign(n)` | `O(current_pool_size + n)` | Rewrites membership for removed/new keys | Low-frequency full replacement. |
| `gc(count)` | Approximately `O(count)` per pass | Bounded by scanned entries | Periodic incremental repair/reclaim. |

`k` in `malloc_key()` is the number of stale head entries cleaned during that allocation. Those entries are removed or marked allocated as they are skipped, so the cost is paid once and amortizes over later calls.

## Testing

The default unit suite is fast (~7s) and self-contained — `pytest` runs it without any external services:

```bash
pip install -e ".[dev]"
pytest                            # ~180 tests, fakeredis only, ~7s
flake8 redis_allocator tests --max-line-length=150
tox                               # py310 / py311 / flake8, matches CI
```

Several test categories are **opt-in via pytest markers** (excluded from the default run by `pyproject.toml`):

```bash
pytest -m concurrency             # multi-thread fakeredis race tests (~25s)
pytest -m fuzz                    # randomised single-thread sequences
pytest -m benchmark               # operation-complexity assertions for allocator commands
pytest -m "concurrency or fuzz or benchmark"   # all of the above
```

For the strongest validation against real-world conditions there is a Docker-backed stress driver under `tests/stress_pool_corruption.py`. It is a standalone script (not collected by pytest), spawning multiple worker processes that randomly malloc / free / extend / shrink / assign / gc against a real Redis container while a "murderer" process randomly SIGKILLs workers to simulate crashes. An atomic-snapshot watcher checks pool invariants throughout. Typical run:

```bash
docker run --rm -d --name allocator-stress -p 6399:6379 redis:7-alpine
python tests/stress_pool_corruption.py        # 60s default; STRESS_DURATION/STRESS_WORKERS env vars
docker rm -f allocator-stress
```

Performance contracts are validated on both backends. The fakeredis `pytest -m benchmark` suite enforces scaling ratios across pool sizes from 10 to 5000 keys for `malloc_key() + free_keys()`, batched `free_keys(n)`, `extend(n)`, `shrink(n)`, `assign(n)`, and `gc(count)`. The benchmark asserts operation cost follows the documented complexity table instead of silently regressing to full-pool scans. It relies on a temporary monkey-patch in `tests/conftest.py` because of [fakeredis upstream PR #473](https://github.com/cunla/fakeredis-py/pull/473) (merged to master, not yet released). The patch is idempotent and self-removable once fakeredis ships a release containing the fix — see the cleanup checklist embedded in `tests/conftest.py`.

A standalone Python script `tests/_perf_real_redis.py` reproduces the same operation-complexity benchmark against a real Redis container for cross-checking and exits non-zero when a contract fails.

## Live Diagnostics

`redis-allocator` includes an observer-only diagnostics API and local dashboard for inspecting live allocator state under multi-process load.

Python API:

```python
from redis import Redis
from redis_allocator import RedisAllocatorDiagnostics

redis = Redis.from_url("redis://localhost:6379/0", decode_responses=True)
diagnostics = RedisAllocatorDiagnostics(redis, prefix="myapp", suffix="allocator")
snapshot = diagnostics.snapshot()
print(snapshot.to_json(indent=2))
```

One-shot JSON:

```bash
redis-allocator-diagnose --redis-url redis://localhost:6379/0 --prefix myapp --suffix allocator --json
```

Live dashboard:

```bash
redis-allocator-diagnose --redis-url redis://localhost:6379/0 --prefix myapp --suffix allocator --interval 1 --port 8765
```

The diagnostics collector reads pool structure and per-pool-key lock state atomically with one Lua script. Advisory metrics such as Redis memory, commandstats, slowlog, soft bindings, and orphan locks are sampled separately. V1 never mutates allocator state and does not repair corruption.

Use `sample_limit` to bound returned examples and `scan_limit` to cap advisory keyspace scans during dashboard refreshes. When a scan is capped, the JSON marks that section as `truncated`.

## Roadmap

*   **Core Focus & Recently Completed:**
    *   [x] Distributed Lock (`RedisLock`, `RedisLockPool`)
    *   [x] Resource Allocator (`RedisAllocator`) - Exclusive & Shared modes.
    *   [x] Task Queue (`RedisTaskQueue`)
    *   [x] Soft Binding Mechanism
    *   [x] Basic Garbage Collection & Health Checking Foundation
    *   [x] Documentation Improvements (Shared Mode, Soft Binding, Lua Clarity)
    *   [x] Foundational Unit Tests & Allocation Mode Coverage
    *   [x] **Pool hardened against multi-process race corruption** (input validation, head-pointer maintenance across locked/expired skip, zombie-key prevention, residual-lock cleanup on re-add, soft-binding ordering, GC self-healing) — see [BUGHUNT_REPORT.md](BUGHUNT_REPORT.md).
    *   [x] **Comprehensive test infrastructure**: bug-hunt regression suite, atomic-snapshot invariant checker, concurrency / fuzz / benchmark / stress markers, Docker-backed stress driver, real-Redis cross-checks.
    *   [x] **`O(1)` performance contract** for `malloc` / `free` / `extend` validated on both real Redis and patched fakeredis.

*   **Current Development Priorities (Focusing on Proxy Pool Needs):**
    *   [ ] **Resource Prioritization:** Implement priority-based allocation in `RedisAllocator`, likely using Redis Sorted Sets (`ZSET`) for the free pool. *(New - High Priority)*
    *   [ ] **Enhanced GC & Health Checking:** Improve configurability (triggers, timeouts) and potentially add hooks for custom health validation logic. Make GC more robust for scenarios like proxy failures. *(Enhanced - High Priority)*
    *   [ ] **Performance Benchmarking & Optimization:** Profile core allocation, GC, and locking operations under simulated proxy pool load. Optimize Lua scripts and Python code. *(Existing - Medium Priority, relevant for performance)*
    *   [ ] **Enhanced Observability:** Add metrics for allocation rates, pool size, GC activity, lock contention, and soft binding usage. Improve logging. *(Existing - Medium Priority, crucial for monitoring)*

*   **Future Enhancements (Single-Instance Focus):**
    *   **Soft Binding Helpers:** Add API methods like `get_bindings_for_key` to easily manage the proxy-to-item relationships used in the fast update mode. *(New - Medium Priority)*
    *   **Refined Error Handling & Recovery:** Define specific exceptions and improve robustness against Redis issues or inconsistent states. *(Existing - Medium Priority)*
    *   **Task Queue Improvements:** Consider task prioritization, retries, delayed tasks, batch processing, dead-letter queues (Lower priority relative to core allocator needs for now).
    *   **Advanced Allocator Features:** Fairness algorithms, resource weighting, custom metadata storage (Lower priority).
    *   **Locking Enhancements:** Contention diagnostics, fairness options (Lower priority).
    *   **Developer Experience:** Enhanced debugging, more complex examples (like a simplified proxy manager pattern).

*   **Out of Scope (Current Direction):**
    *   Native Redis Cluster support.
    *   Multi-key atomic operations beyond single-instance Lua capabilities.

## Contributing

Contributions and suggestions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for more information.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Contact

For questions or suggestions, please contact us through GitHub Issues.
