"""Public Root JSON path compatibility; .crt does not imply PEM encoding.

Readers reject conflicting aliases. Only an approved rotation journal may resume
an interrupted alias update, after each existing value is bound to old/new trust.
"""

from pathlib import Path

from msg.core.codec import canonical, loads
from msg.core.errors import require


def trust_paths(config_dir):
    directory = Path(config_dir) / 'trust'
    require(not directory.is_symlink(), 'unsafe_root_trust_path')
    paths = (directory / 'root.crt', directory / 'root.json')
    for path in paths:
        require(
            not path.is_symlink() and (not path.exists() or path.is_file()),
            'unsafe_root_trust_path',
        )
    return paths


def trust_values(config_dir):
    return {path: loads(path.read_bytes()) for path in trust_paths(config_dir) if path.exists()}


def trust_file(config_dir):
    paths = trust_paths(config_dir)
    values = trust_values(config_dir)
    require(len({canonical(value) for value in values.values()}) <= 1, 'root_trust_alias_mismatch')
    return next(iter(values), paths[0])


def write_trust(config_dir, value, *, writer, approved_values=None):
    paths = trust_paths(config_dir)
    values = trust_values(config_dir)
    if approved_values is None:
        trust_file(config_dir)
    else:
        allowed = {canonical(item) for item in approved_values}
        require(
            all(canonical(item) in allowed for item in values.values()), 'rotation_trust_changed'
        )
    for path in values or {paths[0]: None}:
        writer(path, canonical(value), mode=0o444)


def reserved_plugins_directory(config_dir, *, create=False):
    directory = Path(config_dir) / 'plugins.d'
    require(not directory.is_symlink(), 'unsafe_plugins_directory')
    if create:
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    if directory.exists():
        require(
            directory.is_dir() and directory.stat().st_mode & 0o022 == 0, 'unsafe_plugins_directory'
        )
    return directory
