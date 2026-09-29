import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from msg.storage.legacy_sqlite import preserve


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / 'legacy.db'
    db = sqlite3.connect(path)
    db.executescript((Path(__file__).parent / 'fixtures/legacy_sqlite_0221.sql').read_text())
    db.execute("INSERT INTO boards VALUES ('main','legacy',0,1.0)")
    db.execute("INSERT INTO revocations VALUES ('serial',1.0,'issuer','revoked')")
    db.commit()
    db.close()
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_preserve_is_inert_lossless_and_bound(snapshot, tmp_path):
    before = digest(snapshot)
    dest = tmp_path / 'inert.db'
    plan = preserve(snapshot, expected_sha256=before, destination=dest)
    assert digest(snapshot) == before
    assert plan['authority_enabled'] is False
    assert plan['external_jobs_enabled'] is False
    assert plan['source_provenance_verified'] is False
    assert len(plan['tables']) == 33
    assert plan['tables']['revocations']['count'] == 1
    db = sqlite3.connect(dest)
    row = json.loads(
        db.execute("SELECT envelope FROM legacy_rows WHERE source_table='revocations'").fetchone()[
            0
        ]
    )
    assert row == [
        ['text', 'serial'],
        ['real', '0x1.0000000000000p+0'],
        ['text', 'issuer'],
        ['text', 'revoked'],
    ]
    assert json.loads(db.execute('SELECT manifest FROM provenance').fetchone()[0]) == plan
    db.close()
    assert dest.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        preserve(snapshot, expected_sha256=before, destination=dest)
    assert dest.exists()


@pytest.mark.parametrize(
    'mutation',
    [
        'CREATE TABLE surprise(x)',
        'ALTER TABLE boards ADD COLUMN surprise TEXT',
        'CREATE VIEW surprise AS SELECT * FROM boards',
        'PRAGMA user_version=1',
    ],
)
def test_unknown_schema(snapshot, mutation):
    with closing(sqlite3.connect(snapshot)) as db, db:
        db.execute(mutation)
    with pytest.raises(ValueError):
        preserve(snapshot, expected_sha256=digest(snapshot))


def test_digest_and_sidecar_rejected(snapshot):
    with pytest.raises(ValueError, match='digest'):
        preserve(snapshot, expected_sha256='0' * 64)
    Path(str(snapshot) + '-wal').touch()
    with pytest.raises(ValueError, match='sidecars'):
        preserve(snapshot, expected_sha256=digest(snapshot))


def test_transaction_failure_removes_partial_output(snapshot, tmp_path, monkeypatch):
    import msg.storage.legacy_sqlite as module

    original = module._cell

    def fail_late(value):
        if value == 'serial':
            raise ValueError('injected failure after boards imported')
        return original(value)

    monkeypatch.setattr(module, '_cell', fail_late)
    dest = tmp_path / 'archive.db'
    with pytest.raises(ValueError, match='injected failure'):
        preserve(snapshot, expected_sha256=digest(snapshot), destination=dest)
    assert not dest.exists()


def test_posts_and_ciphertext_preserved_without_new_signature(snapshot, tmp_path):
    import base64

    raw_body = '旧帖\n  exact spacing\n'
    ciphertext = b'\x00\xffopaque\x00'
    with closing(sqlite3.connect(snapshot)) as db, db:
        db.execute(
            'INSERT INTO posts(id,board,seq,body,created,updated,signature,sig_version,sig_nonce,sig_issued,deleted,hidden) VALUES (41,?,?,?,?,?,?,?,?,?,?,?)',
            ('main', 3, raw_body, 1.0, 2.0, 'old-signature', 2, 'nonce', 1, 1, 1),
        )
        db.execute(
            'INSERT INTO keystore_entries VALUES (?,?,?,?,?,?,?)',
            (
                'old-owner',
                'backup',
                ciphertext,
                hashlib.sha256(ciphertext).hexdigest(),
                1,
                1.0,
                1.0,
            ),
        )
    dest = tmp_path / 'archive.db'
    manifest = preserve(snapshot, expected_sha256=digest(snapshot), destination=dest)
    with closing(sqlite3.connect(dest)) as db:
        cells = json.loads(
            db.execute("SELECT envelope FROM legacy_rows WHERE source_table='posts'").fetchone()[0]
        )
        values = dict(zip(manifest['tables']['posts']['columns'], cells, strict=True))
        assert values['id'] == ['integer', '41']
        assert values['body'] == ['text', raw_body]
        assert values['signature'] == ['text', 'old-signature']
        assert values['deleted'] == values['hidden'] == ['integer', '1']
        cells = json.loads(
            db.execute(
                "SELECT envelope FROM legacy_rows WHERE source_table='keystore_entries'"
            ).fetchone()[0]
        )
        assert cells[2] == ['blob', base64.b64encode(ciphertext).decode('ascii')]
        assert (
            db.execute(
                "SELECT name FROM sqlite_schema WHERE name IN ('jobs','identities','certificates')"
            ).fetchall()
            == []
        )


def test_bad_attachment_digest_aborts(snapshot, tmp_path):
    with closing(sqlite3.connect(snapshot)) as db, db:
        db.execute(
            'INSERT INTO attachments(post_id,slot,name,content_type,data,nbytes,sha256,created,uploader_name,downloads) VALUES (1,0,?,?,?,?,?,1.0,?,0)',
            ('a.txt', 'text/plain', b'content', 7, '0' * 64, 'legacy'),
        )
    destination = tmp_path / 'inert.db'
    with pytest.raises(ValueError, match='attachment digest'):
        preserve(snapshot, expected_sha256=digest(snapshot), destination=destination)
    assert not destination.exists()


def test_count_bound_aborts(snapshot, tmp_path, monkeypatch):
    import msg.storage.legacy_sqlite as module

    monkeypatch.setattr(module, 'MAX_ROWS', 0)
    destination = tmp_path / 'inert.db'
    with pytest.raises(ValueError, match='row limit'):
        preserve(snapshot, expected_sha256=digest(snapshot), destination=destination)
    assert not destination.exists()
