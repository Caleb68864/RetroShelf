"""Sphinx configuration for RetroShelf API documentation (autodoc)."""
import os
import sys

# Make the project importable for autodoc.
sys.path.insert(0, os.path.abspath("../.."))
# A dummy config so importing app.main (which builds the app at import) succeeds.
os.environ.setdefault("KAVITA_OPDS_URL", "http://kavita:5000/api/opds/DOC")

project = "RetroShelf"
author = "Caleb Bennett"
copyright = "2026, Logic Nebraska"
release = "1.0.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",     # tolerate Google/NumPy style too
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
]

autodoc_typehints = "description"
autodoc_member_order = "bysource"
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
}
napoleon_use_param = True
napoleon_use_rtype = True

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

html_theme = "alabaster"
exclude_patterns = ["_build"]
nitpicky = False
