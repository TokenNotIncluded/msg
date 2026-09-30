"""List tracked host filesystem call sites and deployment declarations for review."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CALLS = {
    'Path',
    'TemporaryDirectory',
    'mkdtemp',
    'mkstemp',
    'open',
    'durable_write',
    'private_directory',
    'mkdir',
    'chmod',
    'chown',
    'unlink',
    'rename',
    'replace',
    'read_bytes',
    'write_bytes',
    'read_text',
    'write_text',
    'rglob',
    'glob',
    'copytree',
    'rmtree',
    'copyfile',
    'copy2',
    'symlink',
    'stat',
    'lstat',
    'is_file',
    'is_dir',
    'exists',
}


def main():
    for directory in ('src/msg', 'scripts', 'packaging', 'deploy'):
        for path in sorted((ROOT / directory).rglob('*')):
            if not path.is_file() or path.suffix not in {
                '.py',
                '.sh',
                '.service',
                '.conf',
                '.toml',
            }:
                continue
            source = path.read_text()
            lines = source.splitlines()
            if path.suffix == '.py':
                matches = set()
                for node in ast.walk(ast.parse(source)):
                    if isinstance(node, ast.Call):
                        name = (
                            node.func.id
                            if isinstance(node.func, ast.Name)
                            else node.func.attr
                            if isinstance(node.func, ast.Attribute)
                            else ''
                        )
                        if name in CALLS:
                            matches.add(node.lineno)
            else:
                matches = {
                    number
                    for number, line in enumerate(lines, 1)
                    if re.search(r'/(?:etc|usr|var|run|tmp|opt|proc|dev)/', line)
                }
            for number in sorted(matches):
                print(f'{path.relative_to(ROOT)}:{number}: {lines[number - 1].strip()}')


if __name__ == '__main__':
    main()
