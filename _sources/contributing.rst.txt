Contributing
===========

We welcome contributions to RedisAllocator! There are many ways to contribute:

* Reporting bugs
* Suggesting features
* Writing documentation
* Improving test coverage
* Submitting pull requests

Development Setup
---------------

To set up a development environment:

1. Clone the repository:

   .. code-block:: bash

       git clone https://github.com/invoker-bot/RedisAllocator-python.git
       cd RedisAllocator-python

2. Create a virtual environment and install development dependencies:

   .. code-block:: bash

       python -m venv venv
       source venv/bin/activate  # On Windows, use: venv\Scripts\activate
       pip install -e .[test]

3. Run tests:

   .. code-block:: bash

       python -m pytest

Code Style
---------

We follow PEP 8 style guide for Python code. Please ensure your code conforms to this style.

You can check your code style with flake8:

.. code-block:: bash

    flake8 redis_allocator tests

Pull Request Process
------------------

1. Fork the repository and create a branch for your feature or bugfix.
2. Make your changes and ensure tests pass.
3. Update documentation as needed.
4. Submit a pull request.

All pull requests will be reviewed by maintainers. 