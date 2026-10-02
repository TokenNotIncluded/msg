"""Read-only inventory of MSG packages; never opens backups or deletes files."""

import argparse
import json
import os
import re
import stat
import time
from pathlib import Path

PACKAGE_PATTERNS = (
    re.compile(
        r'(msgd|msgctl-server)-([0-9][A-Za-z0-9.+_:~]*)-([0-9][A-Za-z0-9.+_~]*)-'
        r'(x86_64|aarch64|any)\.pkg\.tar\.(zst|xz|gz)'
    ),
    re.compile(r'(msgd|msgctl-server)_([0-9][A-Za-z0-9.+:~_-]*)_(amd64|arm64|all)\.deb'),
    re.compile(
        r'(msgd|msgctl-server)-([0-9][A-Za-z0-9.+_:~]*)-([0-9][A-Za-z0-9.+_~]*)\.'
        r'(x86_64|aarch64|noarch)\.rpm'
    ),
)
FORBIDDEN_ROOTS = (
    '/etc',
    '/run',
    '/proc',
    '/sys',
    '/dev',
    '/var/lib',
    '/var/cache/pacman',
)
TOO_BROAD_ROOTS = {'/', '/home', '/var', '/var/backups', '/var/cache'}
PROTECTED_DIRECTORY = re.compile(
    r'(^|[-_.])(root|keys?|trust|credentials?|pins?|secrets?|data|postgres|blobs?|git|'
    r'service|runtime|config|backups?|database|metadata|wallet|money)([-_.]|$)',
    re.I,
)
PROTECTED_ANCESTOR = re.compile(
    r'^(root|keys?|trust|credentials?|pins?|secrets?|data|postgres|blobs?|git|'
    r'service|runtime|config|backups?|database|metadata|wallet|money)(?:[-_.]|$)',
    re.I,
)
OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
MAX_ENTRIES = 50000
MAX_DEPTH = 8


def absolute(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('paths must be absolute and contain no parent traversal')
    return path


def package(name):
    """A strict filename classification, not a package-content verification."""
    for pattern in PACKAGE_PATTERNS:
        if pattern.fullmatch(name):
            return name
    return None


def open_directory(path):
    """Open every ancestor without following symlinks; descriptors pin traversal."""
    descriptor = os.open('/', OPEN_DIRECTORY)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, OPEN_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def identity(value):
    return {
        'device': value.st_dev,
        'inode': value.st_ino,
        'bytes': value.st_size,
        'mtime_ns': value.st_mtime_ns,
        'ctime_ns': value.st_ctime_ns,
        'nlink': value.st_nlink,
    }


def scan(root, entries, *, recursive=True):
    descriptor = open_directory(root)
    device = os.fstat(descriptor).st_dev

    def visit(current, relative, depth):
        with os.scandir(current) as directory:
            names = []
            for entry in directory:
                if len(names) + len(entries) >= MAX_ENTRIES:
                    raise ValueError('inventory entry limit exceeded; choose narrower roots')
                names.append(entry.name)
            names.sort()
        for name in names:
            if len(entries) >= MAX_ENTRIES:
                raise ValueError('inventory entry limit exceeded; choose narrower roots')
            value = os.stat(name, dir_fd=current, follow_symlinks=False)
            child = relative / name
            item = {'path': str(root / child), 'identity': identity(value), 'reasons': []}
            entries.append(item)
            if stat.S_ISLNK(value.st_mode):
                raise ValueError(f'symlink refused: {root / child}')
            if value.st_dev != device:
                item.update(kind='boundary', reasons=['different_filesystem'])
            elif stat.S_ISDIR(value.st_mode):
                item.update(kind='directory', reasons=['directories_are_never_candidates'])
                if not recursive:
                    item['reasons'].append('package_cache_subdirectory_not_scanned')
                    continue
                if PROTECTED_DIRECTORY.search(name):
                    item['reasons'].append('runtime_or_secret_directory_not_scanned')
                    continue
                if depth >= MAX_DEPTH:
                    raise ValueError('inventory depth limit exceeded; choose narrower roots')
                nested = os.open(name, OPEN_DIRECTORY, dir_fd=current)
                try:
                    opened = os.fstat(nested)
                    if (opened.st_dev, opened.st_ino) != (value.st_dev, value.st_ino):
                        raise ValueError('directory identity changed during inventory')
                    visit(nested, child, depth + 1)
                finally:
                    os.close(nested)
            elif stat.S_ISREG(value.st_mode):
                item['kind'] = 'file'
            else:
                item.update(kind='special', reasons=['non_regular_file'])

    try:
        visit(descriptor, Path(), 0)
    finally:
        os.close(descriptor)


def plan(
    roots,
    current_package,
    rollback_package,
    *,
    rollback_tested=False,
    min_age_days=7,
    now=None,
    package_caches=(),
):
    if not rollback_tested:
        raise ValueError(
            'a real rollback test must be explicitly attested; installation is not proof'
        )
    if not isinstance(min_age_days, int) or min_age_days < 0:
        raise ValueError('min_age_days must be a nonnegative integer')
    archive_roots = {absolute(root) for root in roots}
    cache_roots = {absolute(root) for root in package_caches}
    if archive_roots & cache_roots:
        raise ValueError('archive and shallow package-cache roots must be distinct')
    roots = sorted(archive_roots | cache_roots, key=str)
    if not roots:
        raise ValueError('at least one explicit archive or private package-cache root is required')
    for root in roots:
        if str(root) in TOO_BROAD_ROOTS or any(
            root.is_relative_to(Path(forbidden)) for forbidden in FORBIDDEN_ROOTS
        ):
            raise ValueError(f'business/Root/shared-cache or overly broad root refused: {root}')
        if PROTECTED_DIRECTORY.search(root.name) or any(
            PROTECTED_ANCESTOR.search(ancestor.name) and ancestor != Path('/var/backups')
            for ancestor in root.parents
        ):
            raise ValueError(f'runtime or secret directory root refused: {root}')
        if any(root != other and root.is_relative_to(other) for other in roots):
            raise ValueError('inventory roots must not overlap')
    current_path, rollback_path = absolute(current_package), absolute(rollback_package)
    if current_path == rollback_path:
        raise ValueError('current and verified rollback packages must be distinct')
    for anchor in (current_path, rollback_path):
        if not any(anchor.is_relative_to(root) for root in roots):
            raise ValueError('current and rollback packages must be inside the explicit roots')
    entries = []
    for root in roots:
        scan(root, entries, recursive=root not in cache_roots)
    by_path = {item['path']: item for item in entries}
    anchors = []
    for anchor in (current_path, rollback_path):
        item = by_path.get(str(anchor))
        if not item or item['kind'] != 'file' or not package(anchor.name):
            raise ValueError('current/rollback package is missing, protected, or unrecognized')
        anchors.append(item)
    if (anchors[0]['identity']['device'], anchors[0]['identity']['inode']) == (
        anchors[1]['identity']['device'],
        anchors[1]['identity']['inode'],
    ) or current_path.name == rollback_path.name:
        raise ValueError(
            'rollback must be a distinct package release, not a copy of the current one'
        )

    protected_names = {
        current_path.name: 'current_package',
        rollback_path.name: 'verified_rollback_package',
    }
    timestamp = time.time() if now is None else now
    cutoff = int((timestamp - min_age_days * 86400) * 1_000_000_000)
    groups = {}
    for item in entries:
        if item['kind'] != 'file':
            continue
        path = Path(item['path'])
        base_name = package(path.name)
        if base_name:
            item['classification'] = 'package_filename'
            item['package_path'] = item['path']
        elif path.name.endswith('.sig') and package(path.name[:-4]):
            base_name = path.name[:-4]
            owner = by_path.get(str(path.with_name(base_name)))
            if not owner or owner['kind'] != 'file':
                item['reasons'].append('orphan_signature')
                continue
            item['classification'] = 'paired_package_signature'
            item['package_path'] = owner['path']
        else:
            item['reasons'].append('unrecognized_or_business_backup_or_secret')
            continue
        groups.setdefault(item['package_path'], []).append(item)
        if base_name in protected_names:
            item['reasons'].append(protected_names[base_name])
        if item['identity']['nlink'] != 1:
            item['reasons'].append('hardlinked_file')
        if max(item['identity']['mtime_ns'], item['identity']['ctime_ns']) > cutoff:
            item['reasons'].append('within_minimum_age')
    for members in groups.values():
        reasons = sorted({reason for item in members for reason in item['reasons']})
        for item in members:
            item['reasons'] = reasons or ['historical_package_requires_operator_review']
            item['candidate'] = not reasons
    for item in entries:
        item.setdefault('candidate', False)
    candidates = [item for item in entries if item['candidate']]
    return {
        'mode': 'dry-run-only',
        'package_content_verified': False,
        'rollback_test': 'operator_attested; not performed or verified by this planner',
        'current_package': str(current_path),
        'verified_rollback_package': str(rollback_path),
        'roots': [str(root) for root in roots],
        'shallow_package_caches': [str(root) for root in sorted(cache_roots, key=str)],
        'minimum_age_days': min_age_days,
        'generated_at_unix': timestamp,
        'candidate_files': len(candidates),
        'candidate_bytes': sum(item['identity']['bytes'] for item in candidates),
        'candidate_bytes_kind': 'logical file sizes; not measured freed filesystem space',
        'entries': sorted(entries, key=lambda item: item['path']),
        'notice': 'No files opened for content or changed. Candidates require fresh identity, package '
        'and rollback verification plus separate operator authorization. Never delete directories.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--root',
        type=Path,
        action='append',
        default=[],
        help='explicit dedicated MSG archive; may repeat',
    )
    parser.add_argument(
        '--package-cache',
        type=Path,
        action='append',
        default=[],
        help='private cache, top-level files only; shared pacman cache is refused',
    )
    parser.add_argument('--current-package', type=Path, required=True)
    parser.add_argument('--verified-rollback-package', type=Path, required=True)
    parser.add_argument(
        '--confirm-rollback-tested',
        action='store_true',
        help='attest actual restore/data/schema/authorization compatibility; merely installing is insufficient',
    )
    parser.add_argument('--min-age-days', type=int, default=7)
    args = parser.parse_args()
    try:
        result = plan(
            args.root,
            args.current_package,
            args.verified_rollback_package,
            rollback_tested=args.confirm_rollback_tested,
            min_age_days=args.min_age_days,
            package_caches=args.package_cache,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
