"""Keep these fixtures in the issue_78_80 package namespace.

Without this package marker, pytest's prepend import mode loads this directory's
conftest as the top-level ``conftest`` module. That shadows tests/conftest.py and
breaks test_service's shared ``installed`` fixture import during full collection.
"""
