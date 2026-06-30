Welcome to RedisAllocator's documentation!
=======================================

RedisAllocator is an efficient Redis-based distributed memory allocation system. 
This system simulates traditional memory allocation mechanisms but implements them in a distributed 
environment, using Redis as the underlying storage and coordination tool.

.. note::
   Currently, RedisAllocator only supports single Redis instance deployments. 
   For Redis cluster environments, we recommend using RedLock for distributed locking operations.
   Docker can be used to run Redis, application workers, or stress tests, but RedisAllocator
   does not provide Docker cluster orchestration or container scheduling.

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   installation
   getting_started
   examples/proxy_pool
   api/index
   changelog
   git_history
   contributing

Core Features
------------

- **Distributed Locking**: Provides robust distributed locking mechanisms to ensure data consistency in concurrent environments
- **Resource Allocation**: Implements a distributed resource allocation system with support for priority-based distribution, soft binding, garbage collection, and health checking
- **Task Management**: Implements a distributed task queue system for efficient task processing across multiple workers
- **Object Allocation**: Supports allocation of resources with priority-based distribution and soft binding
- **Health Checking**: Monitors the health of distributed instances and automatically handles unhealthy resources
- **Garbage Collection**: Automatically identifies and reclaims unused resources, optimizing memory usage

Deployment Scope
----------------

RedisAllocator coordinates resources through a single Redis instance. Docker-backed deployments are supported
when Docker hosts, ports, GPUs, or container slots are represented as ordinary allocator keys by application code.
Native Docker cluster orchestration, container scheduling, Redis Cluster support, and multi-key atomic operations
beyond single-instance Lua guarantees are intentionally out of scope.

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
