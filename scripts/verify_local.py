"""Verify an uncommitted checkout without GitHub writes or production deployment.

Use the locked Python 3.15 development environment. Every stage records its
actual result; missing tools, incomplete tests and source drift fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

PINNED_TOOLS = {'ruff': '0.16.9', 'uv': '0.12.20'}
REQUIRED_PACKAGES = (
    'pytest', 'pytest-asyncio', 'build', 'cryptography', 'httpx', 'jsonschema',
    'referencing', 'starlette', 'uvicorn', 'aiohttp', 'dnspython', 'graphql-core',
    'psycopg', 'valkey', 'tiktoken',
)
REQUIRED_COMMANDS = ('git', 'git-lfs', 'age', 'openssl', 'nginx', 'bwrap', 'pg_dump', 'pg_restore')


def content_fingerprint(root, names):
    """Hash the actual files, not the last commit; never follow source symlinks."""
    digest = hashlib.sha256()
    records = []
    for name in sorted(set(names)):
        path = root / name
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('invalid source path')
        if path.is_symlink():
            kind = 'symlink'
            data = os.fsencode(os.readlink(path))
        elif not path.exists():
            continue
        elif path.is_file():
            kind = 'executable' if path.stat().st_mode & 0o111 else 'file'
            data = path.read_bytes()
        else:
            raise ValueError(f'source entry is not a file: {name}')
        item = {'path': name, 'kind': kind, 'sha256': hashlib.sha256(data).hexdigest()}
        digest.update(json.dumps(item, sort_keys=True, ensure_ascii=True).encode() + b'\n')
        records.append(item)
    if not records:
        raise ValueError('empty source inventory')
    return {'sha256': digest.hexdigest(), 'files': records}


def source_snapshot(root):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root)

    tracked = git('ls-files', '-z', '--cached', '--others', '--exclude-standard')
    names = [os.fsdecode(name) for name in tracked.split(b'\0') if name]
    result = content_fingerprint(root, names)
    result['base_commit'] = git('rev-parse', 'HEAD').decode().strip()
    result['dirty'] = bool(git('status', '--porcelain', '--untracked-files=normal').strip())
    return result


def assert_same_source(before, after):
    if before['sha256'] != after['sha256']:
        raise ValueError('source changed during verification; results belong to different code')


def preflight(root):
    blockers = []
    tools = {'python': platform.python_version()}
    if sys.version_info[:2] != (3, 15):
        blockers.append('Python 3.15 is required; do not substitute an older interpreter')
    project = tomllib.loads((root / 'pyproject.toml').read_text())
    if project['project']['requires-python'] != '>=3.15':
        blockers.append('project requires-python is not >=3.15')
    if project['build-system']['requires'] != ['uv_build==0.12.20']:
        blockers.append('uv_build is not pinned to 0.12.20')
    for name in ('ruff', *REQUIRED_PACKAGES):
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            blockers.append(f'missing Python package: {name}')
            continue
        tools[name] = version
        if name in PINNED_TOOLS and version != PINNED_TOOLS[name]:
            blockers.append(f'{name} must be {PINNED_TOOLS[name]}; found {version}')
    for name in REQUIRED_COMMANDS:
        executable = shutil.which(name)
        if executable is None:
            blockers.append(f'missing executable: {name}')
        else:
            tools[name] = executable
    if shutil.which('uv') is None:
        blockers.append('missing uv executable')
    else:
        try:
            version = subprocess.check_output(['uv', '--version'], text=True, timeout=10).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            blockers.append('uv version check failed: ' + type(exc).__name__)
        else:
            tools['uv'] = version
            if version.split()[:2] != ['uv', PINNED_TOOLS['uv']]:
                blockers.append(f'uv must be {PINNED_TOOLS["uv"]}; found {version}')
    for name in ('MSG_TEST_POSTGRES_URL_TEMPLATE', 'MSG_TEST_VALKEY_URL'):
        configured = os.environ.get(name)
        if configured:
            parsed = urlsplit(configured)
            if parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
                blockers.append(f'{name} must point to a disposable local test service')
    if not os.environ.get('MSG_TEST_POSTGRES_URL_TEMPLATE'):
        for name in ('initdb', 'pg_ctl'):
            if shutil.which(name) is None:
                blockers.append(f'missing local PostgreSQL executable: {name}')
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            blockers.append('initdb requires a non-root user, or configure disposable local PostgreSQL')
    if not os.environ.get('MSG_TEST_VALKEY_URL'):
        blockers.append('MSG_TEST_VALKEY_URL must identify disposable local Valkey')
    legacy = os.environ.get('MSG_TEST_LEGACY_SOURCE', str(root / '.legacy-ledger'))
    if not (Path(legacy) / 'src').is_dir():
        blockers.append('missing pinned legacy source; set MSG_TEST_LEGACY_SOURCE')
    else:
        manifest = json.loads((root / 'tests/fixtures/legacy_market_source.json').read_text())
        for name, expected in manifest['sha256'].items():
            path = Path(legacy) / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                blockers.append('pinned legacy source mismatch: ' + name)
    return {'tools': tools, 'blockers': blockers}


def run_step(name, command, directory, *, cwd, env=None, clean_stderr=False, timeout=1200):
    started = time.monotonic()
    stdout = directory / (name + '.stdout')
    stderr = directory / (name + '.stderr')
    result = {'name': name, 'command': command, 'status': 'failed'}
    try:
        with stdout.open('wb') as output, stderr.open('wb') as errors:
            completed = subprocess.run(
                command, cwd=cwd, env=env, stdout=output, stderr=errors,
                check=False, timeout=timeout,
            )
        result['returncode'] = completed.returncode
        result['stderr_bytes'] = stderr.stat().st_size
        if completed.returncode == 0 and (not clean_stderr or not result['stderr_bytes']):
            result['status'] = 'passed'
    except OSError as exc:
        result['error'] = type(exc).__name__
    except subprocess.TimeoutExpired:
        result['error'] = 'timeout'
    result['duration_seconds'] = round(time.monotonic() - started, 3)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shards', type=int, default=8)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.shards <= 64:
        parser.error('--shards must be between 1 and 64')
    root = Path(__file__).resolve().parents[1]
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')
    directory = (args.directory or root / 'artifacts' / ('local-' + stamp)).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    report = {'status': 'blocked', 'stages': [], 'scope': 'local source verification; not deployment'}
    report_path = directory / 'result.json'

    def save():
        report_path.write_text(json.dumps(report, indent=2) + '\n')

    def execute(name, command, *, cwd=root, env=None, clean_stderr=False):
        stage = run_step(name, command, directory, cwd=cwd, env=env, clean_stderr=clean_stderr)
        report['stages'].append(stage)
        save()
        if stage['status'] != 'passed':
            raise ValueError(f'{name} failed; see {directory}')

    try:
        report['source_before'] = source_snapshot(root)
        report['preflight'] = preflight(root)
        save()
        if report['preflight']['blockers']:
            print('\n'.join(report['preflight']['blockers']))
            return 1
        if args.preflight_only:
            report['status'] = 'preflight_passed'
            save()
            return 0
        python = sys.executable
        execute('lock', ['uv', 'lock', '--check', '--python', python])
        execute('dependencies', ['uv', 'pip', 'check', '--python', python])
        execute('ruff', [python, '-m', 'ruff', 'check', '.', '--output-format', 'json'], clean_stderr=True)
        execute('format', [python, '-m', 'ruff', 'format', '--check', '.'], clean_stderr=True)
        execute('compile', [python, '-W', 'error', '-m', 'compileall', '-q', 'src', 'tests', 'scripts', 'conformance', 'conftest.py'], clean_stderr=True)
        execute('tool-sandbox', [python, 'scripts/check_tool_sandbox.py'])
        # Sequential shards keep one local Valkey service from cross-shard races.
        for index in range(args.shards):
            execute(f'shard-{index}', [python, 'scripts/ci_shards.py', 'run', str(index), str(args.shards), '--directory', str(directory)])
        execute('test-union', [python, 'scripts/ci_shards.py', 'check', str(args.shards), '--directory', str(directory)])
        environment = {**os.environ, 'MSG_BENCHMARK_PATH': str(directory / 'token-budget.json')}
        execute('conformance', [python, '-m', 'pytest', 'conformance', f'--junitxml={directory / "conformance.xml"}'], env=environment)
        # Pytest exits zero for skips too; enforce the same no-skips acceptance here.
        import xml.etree.ElementTree as ET

        conformance = ET.parse(directory / 'conformance.xml').getroot()
        if not conformance.findall('.//testcase') or any(
            conformance.findall('.//' + tag) for tag in ('failure', 'error', 'skipped')
        ):
            raise ValueError('conformance evidence is incomplete, failed or skipped')
        dist = directory / 'dist'
        execute('build', [python, '-m', 'build', '--outdir', str(dist)])
        execute('artifacts', [python, 'scripts/check_package_artifacts.py', str(dist)])
        requirements = directory / 'client-requirements.txt'
        execute('client-lock', ['uv', 'export', '--locked', '--no-dev', '--no-emit-project', '--format', 'requirements-txt', '--output-file', str(requirements)])
        with tempfile.TemporaryDirectory(prefix='msg-verify-client-') as temporary:
            folder = Path(temporary)
            venv = folder / 'venv'
            execute('client-env', ['uv', 'venv', '--python', python, str(venv)])
            client_python = str(venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
            wheels = list(dist.glob('*.whl'))
            if len(wheels) != 1:
                raise ValueError('ambiguous client wheel')
            execute('client-install', ['uv', 'pip', 'install', '--python', client_python, '--constraint', str(requirements), str(wheels[0])])
            execute('client-dependencies', ['uv', 'pip', 'check', '--python', client_python])
            environment = dict(os.environ)
            environment.pop('PYTHONPATH', None)
            execute('client-transports', [client_python, str(root / 'scripts/check_client_install.py'), '--minimal-install'], cwd=folder, env=environment)
        report['source_after'] = source_snapshot(root)
        assert_same_source(report['source_before'], report['source_after'])
        report['status'] = 'passed'
        save()
        print(f'Local verification passed: {report_path}')
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
        save()
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
