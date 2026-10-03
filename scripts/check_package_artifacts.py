"""Reject stale code, changed release resources and mislabeled build artifacts."""

import argparse
import configparser
import re
import tomllib
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from tarfile import open as open_tar
from zipfile import ZipFile


def normalized_name(name):
    return re.sub(r'[-_.]+', '_', name).lower()


def package_sources(root):
    package = root / 'src' / 'msg'
    result = {}
    for source in package.rglob('*'):
        if '__pycache__' in source.parts or source.suffix in {'.pyc', '.pyo'}:
            continue
        if source.is_symlink():
            raise ValueError(f'package source must not be a symlink: {source}')
        if source.is_file():
            result['msg/' + source.relative_to(package).as_posix()] = source.read_bytes()
    if 'msg/__init__.py' not in result:
        raise ValueError('missing package source')
    return result


def validate_names(names):
    if len(names) != len(set(names)):
        raise ValueError('duplicate archive member')
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name:
            raise ValueError(f'unsafe archive member: {name}')


def check(root, dist):
    project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    version = project['version']
    distribution = normalized_name(project['name'])
    prefix = f'{distribution}-{version}'
    wheels = sorted(dist.glob(f'{prefix}-*.whl'))
    sdists = sorted(dist.glob(f'{prefix}.tar.gz'))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError('expected exactly one wheel and one source distribution')
    expected = package_sources(root)

    with ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        validate_names(names)
        packaged = {name for name in names if name.startswith('msg/') and not name.endswith('/')}
        if packaged != set(expected):
            difference = sorted(packaged ^ set(expected))
            raise ValueError(f'wheel package file set differs from source: {difference}')
        for name, content in expected.items():
            if wheel.read(name) != content:
                raise ValueError(f'missing or changed wheel source: {name}')
        metadata_path = prefix + '.dist-info/METADATA'
        metadata_files = [name for name in names if name.endswith('.dist-info/METADATA')]
        if metadata_files != [metadata_path]:
            raise ValueError('missing or ambiguous wheel metadata')
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
        for field, value in (
            ('Name', project['name']),
            ('Version', version),
            ('Requires-Python', project['requires-python']),
        ):
            if metadata.get_all(field) != [value]:
                raise ValueError(f'wheel metadata differs from project: {field}')
        entry_path = prefix + '.dist-info/entry_points.txt'
        entry_files = [name for name in names if name.endswith('.dist-info/entry_points.txt')]
        if entry_files != [entry_path]:
            raise ValueError('missing or ambiguous wheel entry points')
        entries = configparser.ConfigParser(interpolation=None)
        entries.read_string(wheel.read(entry_path).decode())
        if not entries.has_section('console_scripts'):
            raise ValueError('missing console scripts')
        if dict(entries['console_scripts']) != project['scripts']:
            raise ValueError('wheel console targets differ from project scripts')

    with open_tar(sdists[0]) as sdist:
        names = sdist.getnames()
        validate_names(names)
        if any(name != prefix and not name.startswith(prefix + '/') for name in names):
            raise ValueError('unexpected source distribution root')
        if any(name.endswith(('/setup.py', '/MANIFEST.in')) for name in names):
            raise ValueError('legacy build files present in sdist')
        source_names = {
            name.removeprefix(prefix + '/src/')
            for name in names
            if name.startswith(prefix + '/src/msg/') and sdist.getmember(name).isfile()
        }
        if source_names != set(expected):
            raise ValueError('sdist package file set differs from source')
        required = {
            **{'src/' + name: data for name, data in expected.items()},
            **{
                name: (root / name).read_bytes()
                for name in (
                    'uv.lock',
                    '.python-version',
                    'docs/DEPLOYMENT.md',
                    'docs/README.md',
                    'docs/ai-game.md',
                    'examples/game_bot.py',
                    *sorted(
                        path.relative_to(root).as_posix()
                        for path in (root / 'docs/archive').glob('*.md')
                    ),
                    'deploy/msgd.service',
                    'tests/test_system_rules.py',
                    'conformance/test_transports.py',
                )
            },
        }
        for relative, content in required.items():
            name = prefix + '/' + relative
            if name not in names or not sdist.getmember(name).isfile():
                raise ValueError(f'missing or non-regular sdist file: {relative}')
            stream = sdist.extractfile(name)
            if stream is None:
                raise ValueError(f'unreadable sdist file: {relative}')
            with stream:
                if stream.read() != content:
                    raise ValueError(f'changed sdist source: {relative}')
        project_path = prefix + '/pyproject.toml'
        if project_path not in names or not sdist.getmember(project_path).isfile():
            raise ValueError('missing sdist project metadata')
        stream = sdist.extractfile(project_path)
        if stream is None:
            raise ValueError('unreadable sdist project metadata')
        with stream:
            packaged_project = tomllib.loads(stream.read().decode())['project']
        for field in ('name', 'version', 'requires-python', 'scripts'):
            if packaged_project.get(field) != project[field]:
                raise ValueError(f'sdist project metadata differs from source: {field}')


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dist', nargs='?', type=Path, default=root / 'dist')
    args = parser.parse_args()
    try:
        check(root, args.dist)
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')
    print('package artifacts match current code, rules, metadata, lockfile, and entry points')


if __name__ == '__main__':
    main()
