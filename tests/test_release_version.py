"""Runtime and build metadata must identify the same release."""

import tomllib
from pathlib import Path

from msg import __version__


def test_runtime_version_matches_distribution_version():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    assert __version__ == project['version']
