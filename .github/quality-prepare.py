"""One-off source-only integration/format preparation. Never imports msg."""
from __future__ import annotations
import ast
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path.cwd()
EVIDENCE = pathlib.Path(os.environ['RUNNER_TEMP']) / 'quality-evidence'
EVIDENCE.mkdir(exist_ok=True)
BASE = 'ff57e660f61ebb051e3c70a16ccbda124a233a78'
PARENTS = [BASE, '22b5e86cc5e2947aa334528b9440f71ed3c7c71f',
           'bf1a166a0c9fd1494d148b94db1875c656a6c2fe',
           'bf07d48d8036e863ac95333f7f84c028b2b14668',
           '4d3f3c0817ca69ed8cc22d46bb9c06818c4cc3f8']


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


def replace(path, old, new):
    target = ROOT / path
    text = target.read_text()
    if old not in text:
        raise RuntimeError('reviewed source fragment changed: ' + path + ' / ' + old[:70])
    target.write_text(text.replace(old, new))


PYPROJECT = '''[build-system]
requires = ["uv_build==0.12.20"]
build-backend = "uv_build"

[project]
name = "msgctl"
version = "0.1.0a1"
description = "Atomic communication infrastructure for sandboxed agents"
readme = "README.md"
requires-python = ">=3.15"
license = "MIT"
authors = [{name = "TokenNotIncluded"}]
dependencies = [
    "cryptography>=46,<52",
    "httpx>=0.28,<1",
    "jsonschema>=4.25,<5",
    "referencing>=0.36,<1",
]

[project.scripts]
msg = "msg.cli:main"
msgd = "msg.daemon:main"

[project.optional-dependencies]
server = [
    "starlette>=0.50,<2",
    "uvicorn>=0.38,<1",
    "aiohttp>=3.13,<4",
    "dnspython>=2.8,<3",
    "graphql-core>=3.2.7,<3.3",
    "psycopg[binary]>=3.3,<4",
    "valkey>=6.1,<7",
]
dev = [
    "msgctl[server]",
    "pytest>=8.4",
    "pytest-asyncio>=1.1",
    "pytest-cov>=6",
    "ruff==0.16.9",
    "build>=1.2",
    "tiktoken>=0.12",
]

[tool.uv.build-backend]
module-name = "msg"
source-include = [
    "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md",
    "conftest.py", "tests/**", "conformance/**", "scripts/**",
    "docs/*.md", "deploy/**", "uv.lock", ".python-version",
]

[tool.pytest.ini_options]
addopts = "-ra --strict-config --strict-markers"
testpaths = ["tests"]
pythonpath = ["src"]
asyncio_mode = "auto"
filterwarnings = ["error"]

[tool.ruff]
required-version = "==0.16.9"
line-length = 100
target-version = "py315"
# Python 3.15 support still requires preview in this pinned Ruff release.
# Explicit rule selection prevents enabling unrelated preview diagnostics.
preview = true
unsafe-fixes = false
src = ["src"]
extend-exclude = [".legacy-ledger"]

[tool.ruff.lint]
select = [
    "E4", "E7", "E9", "F", "I", "UP", "B", "C4", "ICN",
    "PIE", "PGH", "RSE", "T10", "TID", "YTT", "RUF100",
]
explicit-preview-rules = true

[tool.ruff.format]
quote-style = "single"
'''


def integrate():
    git('config', 'user.name', 'LIghtJUNction')
    git('config', 'user.email', 'lightjunction.me@gmail.com')
    git('switch', '--detach', BASE)
    for parent in PARENTS[1:]:
        result = subprocess.run(['git', 'merge', '--no-commit', '--no-ff', parent], check=False)
        conflicts = git('diff', '--name-only', '--diff-filter=U').splitlines()
        if conflicts == ['pyproject.toml'] and parent == PARENTS[1]:
            (ROOT / 'pyproject.toml').write_text(PYPROJECT)
        elif conflicts == ['tests/test_http_effect_order.py'] and parent == PARENTS[2]:
            (ROOT / conflicts[0]).write_text(git('show', parent + ':' + conflicts[0]) + '\n')
        elif conflicts or result.returncode:
            raise RuntimeError(f'unreviewed merge conflict for {parent}: {conflicts}')
        git('add', '-u')
        git('commit', '--no-verify', '-m', 'merge: preserve reviewed integration source ' + parent[:12])
    (ROOT / 'pyproject.toml').write_text(PYPROJECT)
    for name in ('README.md', 'docs/CLIENT_INSTALLATION.md', 'docs/DEPLOYMENT.md',
                 'src/msg/daemon.py', 'tests/test_client_boundary.py'):
        target = ROOT / name
        text = target.read_text().replace('msg-lmm-best', 'msgctl').replace('msg_lmm_best', 'msgctl')
        if name == 'docs/DEPLOYMENT.md':
            text = text.replace('msgctl-1.0.0a1-py3-none-any.whl', 'msgctl-0.1.0a1-py3-none-any.whl')
        target.write_text(text)
    replace('scripts/check_package_artifacts.py', 'import sys\n', 'import re\nimport sys\n')
    replace('scripts/check_package_artifacts.py',
        'version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]',
        'project = tomllib.loads((root / "pyproject.toml").read_text())["project"]\n'
        'version = project["version"]\n'
        'distribution = re.sub(r"[-_.]+", "_", project["name"]).lower()')
    replace('scripts/check_package_artifacts.py', 'f"msg_lmm_best-{version}', 'f"{distribution}-{version}')
    replace('.github/workflows/ci.yml', 'shard: [0, 1, 2, 3]', 'shard: [0, 1, 2, 3, 4, 5, 6, 7]')
    replace('.github/workflows/ci.yml', 'matrix.shard }} 4', 'matrix.shard }} 8')
    replace('.github/workflows/ci.yml', 'scripts/ci_shards.py check 4', 'scripts/ci_shards.py check 8')
    replace('tests/test_ci_shards.py', "@pytest.mark.parametrize('count', [1, 2, 4, 16])",
            "@pytest.mark.parametrize('count', [1, 2, 4, 8, 16])")


MODULES = {}
EXPORTS = {}


def module_name(path):
    parts = path.with_suffix('').relative_to(ROOT / 'src').parts
    return '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)


def absolute_import(module, node):
    if not node.level:
        return node.module or ''
    path = MODULES[module]
    base = module.split('.') if path.name == '__init__.py' else module.split('.')[:-1]
    if node.level > 1:
        base = base[:-(node.level - 1)]
    return '.'.join(base + ([node.module] if node.module else []))


def bindings(nodes, module):
    result = set()
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result.add(node.name)
        elif isinstance(node, ast.Import):
            result.update(a.asname or a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module == '__future__':
                continue
            for alias in node.names:
                if alias.name == '*':
                    result.update(exports(absolute_import(module, node)))
                else:
                    result.add(alias.asname or alias.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.TypeAlias)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.name if isinstance(node, ast.TypeAlias) else node.target]
            for target in targets:
                result.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(node, ast.If):
            if isinstance(node.test, ast.Name) and node.test.id == 'TYPE_CHECKING':
                continue
            result.update(bindings(node.body + node.orelse, module))
        elif isinstance(node, ast.Try):
            result.update(bindings(node.body + node.orelse + node.finalbody, module))
            for handler in node.handlers:
                result.update(bindings(handler.body, module))
    return result


def exports(module):
    if module in EXPORTS:
        return EXPORTS[module]
    if module not in MODULES:
        raise RuntimeError('unknown wildcard source: ' + module)
    tree = ast.parse(MODULES[module].read_text())
    explicit = [n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__all__' for t in n.targets)]
    names = set(ast.literal_eval(explicit[-1].value)) if explicit else {n for n in bindings(tree.body, module) if not n.startswith('_')}
    EXPORTS[module] = names
    return names


def rewrite_imports():
    paths = sorted((ROOT / 'src').rglob('*.py'))
    MODULES.update({module_name(p): p for p in paths})
    report = []
    for module, path in MODULES.items():
        source = path.read_text()
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        edits = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and any(a.name == '*' for a in node.names):
                target = absolute_import(module, node)
                names = sorted(exports(target))
                text = 'from ' + '.' * node.level + (node.module or '') + ' import (\n'
                text += ''.join('    ' + name + ' as ' + name + ',\n' for name in names) + ')\n'
                edits.append((node.lineno - 1, node.end_lineno, text))
                report.append({'module': module, 'source': target, 'explicit_exports': names})
        for start, end, text in reversed(edits):
            lines[start:end] = [text]
        path.write_text(''.join(lines))
    imported = set()
    fixture_names = set()
    all_paths = paths + list((ROOT / 'tests').rglob('*.py')) + list((ROOT / 'conformance').rglob('*.py'))
    for path in all_paths:
        tree = ast.parse(path.read_text())
        module = module_name(path) if path.is_relative_to(ROOT / 'src') else ''
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = absolute_import(module, node) if node.level and module else node.module
                imported.update((target, a.name) for a in node.names if a.name != '*')
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    name = decorator.func if isinstance(decorator, ast.Call) else decorator
                    if isinstance(name, ast.Attribute) and name.attr == 'fixture':
                        fixture_names.add(node.name)
    explicit_modules = {'msg.core.permissions', 'msg.plugins.schemas', 'msg.transports.http',
                        'msg.transports.packet', 'msg.core.packet'}
    owner_exports = {'msg.plugins.money', 'msg.plugins.orders', 'msg.plugins.store',
                     'msg.plugins.delivery', 'msg.market.escrow', 'msg.extensions.ssh_git',
                     'msg.plugins.custodial_lifecycle', 'msg.security.vault',
                     'msg.security.custodial_migration'}
    for path in all_paths:
        source = path.read_text()
        module = module_name(path) if path.is_relative_to(ROOT / 'src') else ''
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        starts = [0]
        for line in lines:
            starts.append(starts[-1] + len(line))
        edits = []
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module == '__future__':
                continue
            for alias in node.names:
                if alias.asname or alias.name == '*':
                    continue
                keep = ((module, alias.name) in imported or module in explicit_modules or
                        (module in owner_exports and (node.module or '').startswith(('msg.market.', 'msg.security.custodial_', 'msg.extensions.git_publication'))) or
                        (not module and alias.name in fixture_names))
                if keep:
                    end = starts[alias.end_lineno - 1] + alias.end_col_offset
                    edits.append((end, ' as ' + alias.name))
        for offset, text in sorted(edits, reverse=True):
            source = source[:offset] + text + source[offset:]
        path.write_text(source)
    (EVIDENCE / 'explicit-imports.json').write_text(json.dumps(report, indent=2))
    path = ROOT / 'src/msg/core/models.py'
    source = path.read_text()
    tree = ast.parse(source)
    pos = max(n.end_lineno for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)))
    lines = source.splitlines(keepends=True)
    lines[pos:pos] = ['\nfrom typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from .contracts import MetadataSession\n']
    path.write_text(''.join(lines))
    for filename in ('src/msg/admin/money.py', 'src/msg/plugins/identity.py',
                     'src/msg/plugins/store.py', 'src/msg/storage/postgres.py'):
        path = ROOT / filename
        source = path.read_text()
        tree = ast.parse(source)
        imports = []
        seen_statement = False
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if seen_statement:
                    imports.append(node)
            elif not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                seen_statement = True
        if not imports:
            continue
        lines = source.splitlines(keepends=True)
        moved = [''.join(lines[n.lineno - 1:n.end_lineno]) for n in imports]
        for node in reversed(imports):
            del lines[node.lineno - 1:node.end_lineno]
        source = ''.join(lines)
        tree = ast.parse(source)
        pos = 0
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)) or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                pos = node.end_lineno
            else:
                break
        lines = source.splitlines(keepends=True)
        lines[pos:pos] = ['\n'] + moved + ['\n']
        path.write_text(''.join(lines))


def prepare():
    integrate()
    rewrite_imports()
    with (EVIDENCE / 'before.json').open('w') as out:
        subprocess.run(['ruff', 'check', '--output-format', 'json', '.'], stdout=out, check=False)
    with (EVIDENCE / 'safe-fixes.txt').open('w') as out:
        result = subprocess.run(['ruff', 'check', '--fix', '.'], stdout=out, stderr=subprocess.STDOUT, check=False)
    (EVIDENCE / 'safe-fixes.exit').write_text(str(result.returncode))
    subprocess.run(['ruff', 'format', '.'], check=True)
    with (EVIDENCE / 'remaining.json').open('w') as out, (EVIDENCE / 'remaining.stderr').open('w') as err:
        result = subprocess.run(['ruff', 'check', '--output-format', 'json', '.'], stdout=out, stderr=err, check=False)
    (EVIDENCE / 'remaining.exit').write_text(str(result.returncode))
    with (EVIDENCE / 'lock.txt').open('w') as out:
        result = subprocess.run(['uv', 'lock', '--python', sys.executable], stdout=out, stderr=subprocess.STDOUT, check=False)
    (EVIDENCE / 'lock.exit').write_text(str(result.returncode))
    git('add', '-u')
    git('commit', '--no-verify', '-m', 'quality: pin Python 3.15 toolchain and prepare explicit imports and strict lint fixes')
    git('bundle', 'create', str(EVIDENCE / 'source.bundle'), 'HEAD', *PARENTS)
    (EVIDENCE / 'changes.patch').write_text(git('diff', BASE, 'HEAD') + '\n')
    (EVIDENCE / 'source.json').write_text(json.dumps({
        'commit': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}'),
        'parents': PARENTS, 'run_id': os.environ['GITHUB_RUN_ID'],
        'mode': 'source-only; no project import, tests or build',
    }, indent=2))
    print('Source prepared; remaining diagnostics and lock status are in evidence.')


if __name__ == '__main__':
    prepare()
