Diagnostics Module
==================

.. module:: redis_allocator.diagnostics

The diagnostics subsystem provides observer-only snapshots and a local dashboard
for live ``RedisAllocator`` instances.

Python API
----------

.. code-block:: python

   from redis import Redis
   from redis_allocator import RedisAllocatorDiagnostics

   redis = Redis.from_url("redis://localhost:6379/0", decode_responses=True)
   diagnostics = RedisAllocatorDiagnostics(redis, prefix="myapp", suffix="allocator")
   snapshot = diagnostics.snapshot()
   print(snapshot.to_json(indent=2))

CLI
---

.. code-block:: bash

   redis-allocator-diagnose --redis-url redis://localhost:6379/0 --prefix myapp --suffix allocator --json
   redis-allocator-diagnose --redis-url redis://localhost:6379/0 --prefix myapp --suffix allocator --interval 1 --port 8765

Atomicity Boundary
------------------

The collector reads allocator pool structure and per-pool-key lock state in one
Lua EVAL. That atomic snapshot includes:

* the free-list head pointer
* the free-list tail pointer
* all pool hash entries
* lock existence and TTL for each pool member

Advisory Redis metrics, soft bindings, and orphan-lock samples are collected
separately and bounded by the configured ``scan_limit`` and ``sample_limit``.
Those advisory sections expose ``scanned`` and ``truncated`` fields so callers
can tell when a dashboard refresh did not inspect every matching key.

Health States
-------------

``healthy``
   No structural violations and no recommendations.

``degraded``
   The allocator structure is intact, but advisory signals such as orphan locks,
   permanent locks, low free capacity, stale bindings, or truncated scans need
   attention.

``corrupt``
   The atomic pool snapshot violates linked-list or lock invariants. Stop writers
   and capture diagnostics JSON before attempting manual repair.

Class Reference
---------------

.. autoclass:: redis_allocator.diagnostics.RedisAllocatorDiagnostics
   :members:

.. autoclass:: redis_allocator.diagnostics.models.AllocatorDiagnosticsSnapshot
   :members:

.. autoclass:: redis_allocator.diagnostics.models.PoolDiagnostics
   :members:

.. autoclass:: redis_allocator.diagnostics.models.BindingDiagnostics
   :members:

.. autoclass:: redis_allocator.diagnostics.models.OrphanDiagnostics
   :members:

.. autoclass:: redis_allocator.diagnostics.models.PressureDiagnostics
   :members:
