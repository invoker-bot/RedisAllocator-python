Welcome to RedisAllocator's documentation!
=======================================

RedisAllocator is an efficient Redis-based distributed memory allocation system. 
This system simulates traditional memory allocation mechanisms but implements them in a distributed 
environment, using Redis as the underlying storage and coordination tool.

.. note::
   Currently, RedisAllocator only supports single Redis instance deployments. 
   For Redis cluster environments, we recommend using RedLock for distributed locking operations.

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   installation
   getting_started
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

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search` 