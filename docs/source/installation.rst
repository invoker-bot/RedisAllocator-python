Installation
============

Requirements
-----------

RedisAllocator requires:

* Python 3.10 or later
* Redis 5.0 or later (with Lua scripting support)

Installing from PyPI
-------------------

RedisAllocator is available on PyPI. You can install it with pip:

.. code-block:: bash

    pip install redis-allocator

This will install RedisAllocator and its dependencies.

Installing from Source
---------------------

You can also install RedisAllocator from source:

.. code-block:: bash

    git clone https://github.com/invoker-bot/RedisAllocator-python.git
    cd RedisAllocator-python
    pip install .

For development installation (with testing dependencies):

.. code-block:: bash

    pip install -e .[test]

Setting up Redis
---------------

RedisAllocator requires a running Redis server. You can install Redis following the 
`official Redis documentation <https://redis.io/docs/getting-started/>`_.

For quick testing, you can use Docker:

.. code-block:: bash

    docker run --name redis-test -p 6379:6379 -d redis

This will start a Redis server on localhost port 6379. 