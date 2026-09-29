"""Offline, inert preservation of the source-pinned SQLite generation.

This is NOT a v4 restore or authority conversion. Nothing here opens a live
service database, schedules work, decrypts custody keys, or enables identities.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path

CONTRACT = Path(__file__).parents[1] / 'data/legacy/sqlite-0221.json'
MAX_BYTES = 256 * 1024 * 1024
MAX_ROWS = 1_000_000
MAX_CELL = 16 * 1024 * 1024


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _cell(value):
    if value is None:
        return ['null', None]
    if isinstance(value, bytes):
        if len(value) > MAX_CELL:
            raise ValueError('cell exceeds preservation limit')
        return ['blob', base64.b64encode(value).decode('ascii')]
    if isinstance(value, str):
        if len(value.encode('utf-8')) > MAX_CELL:
            raise ValueError('cell exceeds preservation limit')
        return ['text', value]
    if isinstance(value, int):
        return ['integer', str(value)]
    if isinstance(value, float) and math.isfinite(value):
        return ['real', value.hex()]
    raise ValueError('unsupported SQLite value')


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def preserve(source: Path, *, expected_sha256: str, destination: Path | None = None):
    """Preflight a protected, checkpointed offline copy; optionally import inert rows.

    expected_sha256 must come from the operator's protected snapshot manifest.
    A digest is binding, not proof of independent provenance or backup validity.
    """
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError('source must be a regular offline snapshot')
    if source.stat().st_size > MAX_BYTES:
        raise ValueError('snapshot exceeds preservation limit')
    if any(Path(str(source) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('snapshot has live SQLite sidecars; obtain a consistent offline copy')
    if len(expected_sha256) != 64 or _digest(source) != expected_sha256:
        raise ValueError('snapshot digest mismatch')
    contract = json.loads(CONTRACT.read_text())
    source_db = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    output = None
    created = False
    deadline = time.monotonic() + 60
    source_db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    source_db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_CELL)
    try:
        source_db.execute('PRAGMA query_only=ON')
        source_db.execute('PRAGMA trusted_schema=OFF')
        source_db.execute('BEGIN')
        if source_db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('snapshot integrity check failed')
        objects = source_db.execute(
            "SELECT type,name FROM sqlite_schema WHERE type IN ('table','view','trigger')"
        ).fetchall()
        if any(kind != 'table' for kind, _ in objects):
            raise ValueError('unrecognized executable schema objects')
        if {name for _, name in objects} != set(contract['tables']):
            raise ValueError('unrecognized table set')
        if source_db.execute('PRAGMA user_version').fetchone()[0] != 0:
            raise ValueError('unrecognized user_version')
        for name, columns in contract['tables'].items():
            actual = [list(row) for row in source_db.execute(f'PRAGMA table_xinfo({_quote(name)})')]
            if actual != columns:
                raise ValueError(f'unrecognized columns: {name}')
        if destination is not None:
            destination = Path(destination)
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            created = True
            output = sqlite3.connect(destination)
            output.execute('CREATE TABLE provenance (manifest TEXT NOT NULL)')
            output.execute(
                'CREATE TABLE legacy_rows (source_table TEXT NOT NULL, ordinal INTEGER NOT NULL, envelope TEXT NOT NULL, PRIMARY KEY(source_table,ordinal))'
            )
            output.execute('BEGIN')
        tables = {}
        total = 0
        encoded_bytes = 0
        for name, columns in contract['tables'].items():
            if name in ('attachments', 'archived_attachments'):
                for data, nbytes, sha256 in source_db.execute(
                    f'SELECT data,nbytes,sha256 FROM {_quote(name)}'
                ):
                    if (
                        not isinstance(data, bytes)
                        or len(data) != nbytes
                        or hashlib.sha256(data).hexdigest() != sha256
                    ):
                        raise ValueError('legacy attachment digest mismatch')
            count = source_db.execute(f'SELECT count(*) FROM {_quote(name)}').fetchone()[0]
            total += count
            if total > MAX_ROWS:
                raise ValueError('snapshot exceeds row limit')
            digest = hashlib.sha256()
            # Physical row order is stable for this exact digest-bound snapshot.
            for ordinal, row in enumerate(source_db.execute(f'SELECT * FROM {_quote(name)}')):
                envelope = _json([_cell(value) for value in row])
                encoded = envelope.encode('ascii')
                encoded_bytes += len(encoded)
                if encoded_bytes > MAX_BYTES * 4:
                    raise ValueError('encoded archive exceeds size limit')
                digest.update(len(encoded).to_bytes(8, 'big') + encoded)
                if output is not None:
                    output.execute(
                        'INSERT INTO legacy_rows VALUES (?,?,?)', (name, ordinal, envelope)
                    )
            tables[name] = {
                'count': count,
                'rows_sha256': digest.hexdigest(),
                'columns': [column[1] for column in columns],
            }
        if _digest(source) != expected_sha256:
            raise ValueError('snapshot changed during preservation')
        manifest = {
            'format': 'msg-legacy-sqlite-preservation-v1',
            'source_sha256': expected_sha256,
            'schema_contract_sha256': _digest(CONTRACT),
            'reference_source_revision': contract['reference_source_revision'],
            'status': 'inert-preservation-only',
            'authority_enabled': False,
            'external_jobs_enabled': False,
            'source_provenance_verified': False,
            'tables': tables,
            'remaining_gates': [
                'installed-source-and-schema-provenance',
                'protected-snapshot-provenance',
                'independent-current-revocation-checkpoint',
                'root-approved-identity-and-authority-mapping',
                'signed-post-and-stable-url-mapping',
                'custody-reenrollment',
                'filesystem-repository-inventory',
                'isolated-v4-acceptance',
            ],
        }
        if output is not None:
            output.execute('INSERT INTO provenance VALUES (?)', (_json(manifest),))
            output.commit()
        return manifest
    except BaseException:
        if output is not None:
            output.rollback()
            output.close()
            output = None
        if created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        source_db.close()
        if output is not None:
            output.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            preserve(args.snapshot, expected_sha256=args.sha256, destination=args.destination),
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
