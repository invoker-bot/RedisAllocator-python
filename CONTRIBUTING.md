# Contributing to RedisAllocator

Thank you for considering contributing to RedisAllocator! This document provides guidelines and instructions for contributing to this project.

## Code of Conduct

Please be respectful and considerate of others when contributing to this project. We aim to foster an inclusive and welcoming community.

## Getting Started

1. Fork the repository on GitHub
2. Clone your fork locally
3. Create a new branch for your feature or bugfix
4. Make your changes
5. Run tests to ensure your changes don't break existing functionality
6. Submit a pull request

## Development Environment

Set up your development environment:

```bash
# Create and activate a virtual environment (optional but recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install the package in development mode with all dependencies
pip install -e ".[dev]"
```

## Testing

We use pytest for testing. Run the test suite before submitting your changes:

```bash
pytest
```

For more comprehensive testing with coverage reports:

```bash
pytest --cov=redis_allocator
```

## Coding Standards

We follow PEP 8 standards for Python code. Use flake8 to check your code:

```bash
flake8 redis_allocator tests
```

## Commit Messages

Commit messages must follow the [Conventional Commits](https://www.conventionalcommits.org/) standard:

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

Types include:
- feat: A new feature
- fix: A bug fix
- docs: Documentation changes
- style: Code style changes (formatting, missing semicolons, etc)
- refactor: Code changes that neither fix bugs nor add features
- perf: Performance improvements
- test: Adding or updating tests
- build: Changes to build process
- ci: Changes to CI configuration
- chore: Other changes that don't modify source or test files

Examples:
```
feat: add Redis connection pooling
fix(lock): fix race condition in lock acquisition
docs: update installation instructions
```

## Pull Request Process

1. Update the README.md or documentation with details of changes if applicable
2. Update the tests to cover your changes
3. The PR should work for Python 3.10 and above
4. Your PR needs to be approved by at least one maintainer

## Documentation

We use Sphinx for documentation. If you add new features, please update the documentation:

```bash
# Generate the documentation
cd docs
make html
```

## Release Process

Releases are managed by the project maintainers. Version numbers follow [Semantic Versioning](https://semver.org/).

## Questions?

If you have any questions or need help, please open an issue on GitHub.

Thank you for contributing to RedisAllocator! 