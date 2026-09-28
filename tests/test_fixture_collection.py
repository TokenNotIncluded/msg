"""Nested fixture scopes must not break collection of PostgreSQL integration tests."""
from pathlib import Path
import subprocess
import sys


def test_nested_and_root_fixture_suites_collect_in_one_process():
    # A fresh pytest process catches the order-dependent `conftest` module alias
    # collision. Collection does not execute fixtures or open database services.
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '--collect-only', '-q',
         'tests/issue_78_80', 'tests/test_transfer.py', 'tests/test_service.py'],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'test_service_register_post_read_reply_receipt' in result.stdout
    assert 'test_transfer.py::' in result.stdout
    assert 'issue_78_80/test_content_patch_contract.py::' in result.stdout
