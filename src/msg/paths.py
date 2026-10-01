"""Host filesystem layout. MSG resource paths and URLs are unrelated to these paths."""

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from msg.atomic_file import durable_write
from msg.core.errors import require
from msg.service_origin import service_namespace

SERVER_CONFIG_DIR = Path('/etc/msgd')
SERVER_DATA_DIR = Path('/var/lib/msgd')
ROOT_PRIVATE_DIR = Path('/var/lib/msgd-root')
SERVER_CACHE_DIR = Path('/var/cache/msgd')
SERVER_RUNTIME_DIR = Path('/run/msgd')


@dataclass(frozen=True)
class ServerPaths:
    """Stable installation identifiers, deliberately independent of DNS names."""

    config: Path
    data: Path
    root: Path
    cache: Path
    runtime: Path

    @classmethod
    def for_instance(cls, name):
        require(
            isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,25}', name) is not None,
            'invalid_instance_name',
        )
        return cls(
            SERVER_CONFIG_DIR / name,
            SERVER_DATA_DIR / name,
            Path('/var/lib/private/msgd') / name / 'root',
            SERVER_CACHE_DIR / name,
            SERVER_RUNTIME_DIR / name,
        )


def private_directory(path):
    """Validate before chmod; never follow a symlink in an application directory."""
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), 'unsafe_client_directory')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    require(info.st_uid == os.geteuid(), 'client_directory_not_owned')
    require(stat.S_ISDIR(info.st_mode), 'unsafe_client_directory')
    path.chmod(0o700)
    return path


def xdg_directory(variable, fallback):
    value = os.environ.get(variable)
    return Path(value) if value and Path(value).is_absolute() else Path.home() / fallback


@dataclass(frozen=True)
class ClientPaths:
    config: Path
    data: Path
    state: Path
    cache: Path
    portable: bool = False

    @classmethod
    def discover(cls, directory=None, *, profile=None, server=None, account=None):
        if directory is not None:
            require(
                profile is None and server is None and account is None,
                'profile_conflicts_with_config_dir',
            )
            directory = Path(directory).expanduser().absolute()
            return cls(directory, directory, directory, directory / 'cache', True)
        require(
            profile is None or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', profile),
            'invalid_profile_name',
        )
        suffix = (
            Path('msg') / 'services' / service_namespace(server)
            if server is not None
            else Path('msg') / 'profiles' / profile
            if profile
            else Path('msg')
        )
        if account is not None:
            require(
                isinstance(account, str)
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', account) is not None,
                'invalid_account_name',
            )
            suffix = suffix / 'accounts' / account
        return cls(
            xdg_directory('XDG_CONFIG_HOME', '.config') / suffix,
            xdg_directory('XDG_DATA_HOME', '.local/share') / suffix,
            xdg_directory('XDG_STATE_HOME', '.local/state') / suffix,
            xdg_directory('XDG_CACHE_HOME', '.cache') / suffix,
        )

    def prepare(self):
        for path in {self.config, self.data, self.state, self.cache}:
            # Protect intermediate application-owned profile directories too.
            parents = list(path.parents)
            app = next((parent for parent in parents if parent.name == 'msg'), None)
            if app is not None:
                private_directory(app)
                if path.is_relative_to(app / 'profiles'):
                    private_directory(app / 'profiles')
                if path.is_relative_to(app / 'services'):
                    private_directory(app / 'services')
            if path.parent.name == 'accounts':
                private_directory(path.parent)
                private_directory(path.parent.parent)
            private_directory(path)

    def account_names(self):
        """Inspect a service's saved accounts without creating or migrating files."""
        names = set()
        for base in {self.config, self.data, self.state}:
            root = base / 'accounts'
            for parent in (root, *root.parents):
                require(not parent.is_symlink(), 'unsafe_client_directory')
            if not root.exists():
                continue
            info = root.stat()
            require(
                stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid(),
                'unsafe_client_directory',
            )
            for entry in root.iterdir():
                info = entry.lstat()
                require(not stat.S_ISLNK(info.st_mode), 'unsafe_client_directory')
                if not stat.S_ISDIR(info.st_mode):
                    continue
                require(
                    info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
                    'unsafe_client_directory',
                )
                self.discover(account=entry.name)
                names.add(entry.name)
        return sorted(names)

    def temporary_parent(self):
        value = os.environ.get('XDG_RUNTIME_DIR')
        if not value or not Path(value).is_absolute():
            return None
        runtime = Path(value)
        require(not runtime.is_symlink() and runtime.is_dir(), 'unsafe_runtime_directory')
        info = runtime.stat()
        require(
            info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0, 'unsafe_runtime_directory'
        )
        return private_directory(runtime / 'msg')

    def file(self, name):
        """Keys and exported certificates are durable data; sessions/journals are state."""
        require(
            isinstance(name, str)
            and name not in {'', '.', '..'}
            and Path(name).name == name
            and '/' not in name
            and '\\' not in name,
            'invalid_client_filename',
        )
        base = (
            self.data
            if name.endswith(('.key', '.agekey', '.crt'))
            or name in {'bank-grant.json', 'custodial-history.json'}
            else self.state
        )
        return base / name

    def migrate(self, source):
        import fcntl

        sources = (
            {source.data, source.state}
            if isinstance(source, ClientPaths)
            else {Path(source).expanduser().absolute()}
        )
        sources = {path for path in sources if path.exists()}
        if not sources:
            return
        require(not sources & {self.data, self.state}, 'invalid_migration_source')
        directories = sorted(sources | {self.data, self.state}, key=str)
        descriptors = []
        try:
            for directory in directories:
                private_directory(directory)
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                descriptors.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX)
            self._migrate(
                sources, source_data=source.data if isinstance(source, ClientPaths) else None
            )
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def _migrate(self, source, *, source_data=None):
        """Copy a legacy profile durably, check conflicts, then retire its original files.

        A crash leaves either identical copies or the old file. Retrying never
        generates a replacement identity or overwrites a different credential.
        """
        entries = []
        targets = {}
        candidates = []
        nested = []

        def collect(directory, relative=Path()):
            for entry in sorted(directory.iterdir()):
                if (
                    not relative.parts
                    and entry.name in {'cache', 'profiles', 'services', 'accounts'}
                    and entry.is_dir()
                    and not entry.is_symlink()
                ):
                    continue
                info = entry.lstat()
                require(not entry.is_symlink(), 'unsafe_legacy_client_file')
                if stat.S_ISDIR(info.st_mode):
                    require(
                        info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0,
                        'unsafe_legacy_client_file',
                    )
                    nested.append(entry)
                    collect(entry, relative / entry.name)
                else:
                    candidates.append((entry, relative))

        for directory in sorted(source):
            collect(directory)
        for entry, relative in candidates:
            info = entry.lstat()
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and info.st_mode & 0o077 == 0
                and info.st_nlink == 1,
                'unsafe_legacy_client_file',
            )
            if relative.parts:
                base = (
                    self.data
                    if (source_data is not None and entry.is_relative_to(source_data))
                    or relative.parts[0] == 'subagents'
                    else self.state
                )
                target = base / relative / entry.name
            else:
                target = self.file(entry.name)
            if relative.parts:
                for parent in reversed(target.parent.parents):
                    if (
                        parent in {self.data, self.state}
                        or parent.is_relative_to(self.data)
                        or parent.is_relative_to(self.state)
                    ):
                        if parent.exists() or parent.is_symlink():
                            private_directory(parent)
                if target.parent.exists() or target.parent.is_symlink():
                    private_directory(target.parent)
            if target in targets:
                require(
                    entry.read_bytes() == targets[target].read_bytes(), 'client_migration_conflict'
                )
            targets[target] = entry
            if target.exists() or target.is_symlink():
                current = target.lstat()
                require(
                    stat.S_ISREG(current.st_mode)
                    and current.st_uid == os.geteuid()
                    and current.st_mode & 0o077 == 0
                    and current.st_nlink == 1,
                    'unsafe_client_state_permissions',
                )
                require(target.read_bytes() == entry.read_bytes(), 'client_migration_conflict')
            entries.append((entry, target))
        require(
            not (
                (self.file('identity.key') in targets or self.file('identity.key').exists())
                and (
                    self.file('hardware-signer.json') in targets
                    or self.file('hardware-signer.json').exists()
                )
            ),
            'client_key_backend_conflict',
        )
        for entry, target in entries:
            if not target.exists():
                private_directory(target.parent)
                durable_write(target, entry.read_bytes(), mode=0o600)
        for entry, target in entries:
            require(target.read_bytes() == entry.read_bytes(), 'client_migration_conflict')
        for entry, _ in entries:
            entry.unlink()
        for directory in sorted(nested, key=lambda p: len(p.parts), reverse=True):
            directory.rmdir()
        for directory in source:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
