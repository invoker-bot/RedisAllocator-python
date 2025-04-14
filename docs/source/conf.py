"""Configuration file for the Sphinx documentation builder."""
import os
import sys
import time
import redis_allocator

# Add the package directory to the path so sphinx can find it
sys.path.insert(0, os.path.abspath('../..'))

# -- Project information -----------------------------------------------------
project = 'RedisAllocator'
copyright = f'2024-{time.strftime("%Y")}, Invoker Bot'
author = 'Invoker Bot'

# The full version, including alpha/beta/rc tags
release = redis_allocator.__version__
version = release

# -- General configuration ---------------------------------------------------
extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
    'sphinx.ext.intersphinx',
    'sphinx.ext.autosectionlabel',
    'sphinx_rtd_theme',
    'sphinx_git',  # For automatic changelog generation from git commits
    'sphinxcontrib.mermaid',  # Added for Mermaid diagrams
]

# Napoleon settings
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False
napoleon_include_special_with_doc = True
napoleon_use_admonition_for_examples = False
napoleon_use_admonition_for_notes = False
napoleon_use_admonition_for_references = False
napoleon_use_ivar = False
napoleon_use_param = True
napoleon_use_rtype = True
napoleon_preprocess_types = False
napoleon_type_aliases = None
napoleon_attr_annotations = True

# Autodoc settings
autodoc_default_options = {
    'members': True,
    'member-order': 'bysource',
    'special-members': '__init__',
    'undoc-members': True,
    'exclude-members': '__weakref__',
}
autodoc_typehints = 'description'
autodoc_class_signature = 'separated'

# Sphinx Git configuration
git_show_sourcelink = True

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
exclude_patterns = []

# The name of the Pygments (syntax highlighting) style to use.
pygments_style = 'sphinx'

# -- Options for HTML output -------------------------------------------------
html_theme = 'sphinx_rtd_theme'
# Comment out the line below to avoid the warning until _static is populated
# html_static_path = ['_static']
html_title = f'RedisAllocator {version}'
html_logo = None  # Add logo path here if needed
html_favicon = None

# Custom sidebar templates
html_sidebars = {
    '**': [
        'relations.html',  # needs 'show_related': True theme option to display
        'searchbox.html',
    ]
}

# Output file base name for HTML help builder.
htmlhelp_basename = 'RedisAllocatordoc'

# Intersphinx mapping
intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
    'redis': ('https://redis-py.readthedocs.io/en/stable/', None),
} 