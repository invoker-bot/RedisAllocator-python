# RedisAllocator Documentation

This directory contains the documentation for the RedisAllocator package, built with [Sphinx](https://www.sphinx-doc.org/).

## Building the Documentation

### Prerequisites

Install the documentation dependencies:

```bash
pip install -e .[docs]
```

### Build HTML Documentation

To build the HTML documentation:

```bash
cd docs
make html
```

The built documentation will be available in `docs/build/html`.

### Other Build Formats

Sphinx supports various output formats. Some common ones include:

- `make latexpdf` - Build LaTeX documentation and run it through pdflatex
- `make epub` - Build epub documentation
- `make text` - Build plain text documentation

## Documentation Structure

- `source/` - Source files for the documentation
  - `api/` - API reference documentation
  - `_static/` - Static files (CSS, JavaScript, images)
  - `_templates/` - Custom HTML templates
  - `conf.py` - Sphinx configuration file
  - `index.rst` - Documentation homepage
  - `*.rst` - Other documentation pages

## Automatic Changelog Generation

The documentation automatically generates a changelog from Git commit history using the `sphinx-git` extension. There are two pages that show Git history:

- `changelog.rst` - Contains official release information and a brief commit history
- `git_history.rst` - Contains a more detailed Git commit history

To see these in action, you need to:

1. Make sure your local clone has the complete Git history (`git clone` without `--depth` option)
2. Have the `sphinx-git` extension installed (included in the docs dependencies)

The changelog is automatically filtered to show only changes to Python and documentation files. To customize this, edit the `filename-filter` parameter in the respective RST files.

## Automatic Deployment

The documentation is automatically built and deployed to GitHub Pages when changes are pushed to the main branch. The deployed documentation is available at: https://invoker-bot.github.io/RedisAllocator-python/

## Contributing to Documentation

Contributions to the documentation are welcome! To contribute:

1. Fork the repository
2. Make your changes to the documentation
3. Build the documentation locally to verify your changes
4. Submit a pull request

Please make sure your documentation changes are clear, accurate, and follow the existing style. 