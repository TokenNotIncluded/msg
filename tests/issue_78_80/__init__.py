"""Keep this isolated suite's conftest out of the root fixture namespace.

The full PostgreSQL suite imports installed from tests/conftest.py. Without
this package boundary, pytest loads the nested fixture as top-level conftest
and breaks collection of the full suite. No tests or fixtures are excluded.
"""
