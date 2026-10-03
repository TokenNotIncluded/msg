"""A stale or mislabeled build must not pass release verification."""

import io
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'check_package_artifacts.py'
PROJECT = """[build-system]
requires = ["uv_build==0.12.20"]
build-backend = "uv_build"
[project]
name = "msgctl"
version = "0.1.0a1"
requires-python = ">=3.15"
[project.scripts]
msg = "msg.cli:main"
msgd = "msg.daemon:main"
"""
METADATA = 'Metadata-Version: 2.4\nName: msgctl\nVersion: 0.1.0a1\nRequires-Python: >=3.15\n\n'
ENTRY_POINTS = '[console_scripts]\nmsg = msg.cli:main\nmsgd = msg.daemon:main\n'
REQUIRED = (
    'docs/DEPLOYMENT.md',
    'docs/README.md',
    'docs/ai-game.md',
    'docs/archive/fixture.md',
    'examples/game_bot.py',
    'deploy/msgd.service',
    'tests/test_system_rules.py',
    'conformance/test_transports.py',
)


def make_release(root, *, wheel_change=None, source_change=None):
    (root / 'scripts').mkdir()
    shutil.copyfile(SCRIPT, root / 'scripts' / SCRIPT.name)
    files = {
        'pyproject.toml': PROJECT.encode(),
        'uv.lock': b'version = 1\n',
        '.python-version': b'3.15\n',
        'src/msg/__init__.py': b"__version__ = '0.1.0a1'\n",
        'src/msg/cli.py': b'def main():\n    return 0\n',
        'src/msg/data/system/AGENTS.md': b'Read the rules.\n',
        **dict.fromkeys(REQUIRED, b'fixture\n'),
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    dist = root / 'dist'
    dist.mkdir()
    prefix = 'msgctl-0.1.0a1'
    wheel_files = {
        name.removeprefix('src/'): data for name, data in files.items() if name.startswith('src/')
    }
    wheel_files[prefix + '.dist-info/METADATA'] = METADATA.encode()
    wheel_files[prefix + '.dist-info/entry_points.txt'] = ENTRY_POINTS.encode()
    if wheel_change is not None:
        wheel_change(wheel_files)
    with zipfile.ZipFile(dist / (prefix + '-py3-none-any.whl'), 'w') as archive:
        for name, content in wheel_files.items():
            archive.writestr(name, content)
    if source_change is not None:
        source_change(files)
    with tarfile.open(dist / (prefix + '.tar.gz'), 'w:gz') as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(prefix + '/' + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return subprocess.run(
        [sys.executable, str(root / 'scripts' / SCRIPT.name), str(dist)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_matching_release_artifacts_are_accepted(tmp_path):
    result = make_release(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_stale_python_source_in_wheel_is_rejected(tmp_path):
    result = make_release(
        tmp_path,
        wheel_change=lambda files: files.update({'msg/cli.py': b'def main():\n    return 99\n'}),
    )
    assert result.returncode != 0, 'stale Python source was accepted'
    assert 'msg/cli.py' in result.stdout + result.stderr


@pytest.mark.parametrize(
    'field,old,new',
    [
        ('Name', 'msgctl', 'other-project'),
        ('Version', '0.1.0a1', '1.0.0a1'),
        ('Requires-Python', '>=3.15', '>=3.13'),
    ],
)
def test_mislabeled_wheel_metadata_is_rejected(tmp_path, field, old, new):
    def change(files):
        name = 'msgctl-0.1.0a1.dist-info/METADATA'
        files[name] = files[name].replace(f'{field}: {old}'.encode(), f'{field}: {new}'.encode())

    result = make_release(tmp_path, wheel_change=change)
    assert result.returncode != 0, f'incorrect {field} was accepted'


def test_stale_python_source_in_sdist_is_rejected(tmp_path):
    result = make_release(
        tmp_path,
        source_change=lambda files: files.update({
            'src/msg/cli.py': b'def main():\n    return 99\n'
        }),
    )
    assert result.returncode != 0, 'stale sdist Python source was accepted'


def test_missing_locked_dependencies_in_sdist_are_rejected(tmp_path):
    result = make_release(tmp_path, source_change=lambda files: files.pop('uv.lock'))
    assert result.returncode != 0, 'missing lockfile was accepted'


def test_extra_old_python_module_in_wheel_is_rejected(tmp_path):
    result = make_release(
        tmp_path,
        wheel_change=lambda files: files.update({'msg/removed_module.py': b'old = True\n'}),
    )
    assert result.returncode != 0, 'deleted Python module was accepted'


def test_wrong_console_target_is_rejected(tmp_path):
    result = make_release(
        tmp_path,
        wheel_change=lambda files: files.update({
            'msgctl-0.1.0a1.dist-info/entry_points.txt': (
                ENTRY_POINTS.replace('msg.cli:main', 'msg.old_cli:main').encode()
            ),
        }),
    )
    assert result.returncode != 0, 'wrong console target was accepted'
