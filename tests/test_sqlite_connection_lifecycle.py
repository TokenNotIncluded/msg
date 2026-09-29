"""SQLite setup and backup failures must release every acquired connection."""

import sqlite3

import pytest

from msg.storage.sqlite import SqliteMetadataStore


def assert_closed(connection):
    with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
        connection.execute('SELECT 1')


@pytest.mark.parametrize('statement', ['PRAGMA foreign_keys=ON', 'PRAGMA synchronous=FULL'])
def test_connection_setup_failure_closes_connection(tmp_path, monkeypatch, statement):
    store = SqliteMetadataStore(tmp_path / 'metadata.db')
    connect = sqlite3.connect
    acquired = []

    class BrokenConnection(sqlite3.Connection):
        def execute(self, query, *args, **kwargs):
            if query == statement:
                raise sqlite3.OperationalError('injected setup failure')
            return super().execute(query, *args, **kwargs)

    def acquire(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=BrokenConnection)
        acquired.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', acquire)
    with pytest.raises(sqlite3.OperationalError, match='injected setup failure'):
        store._connect()
    assert len(acquired) == 1
    assert_closed(acquired[0])


def test_backup_target_open_failure_closes_source(tmp_path, monkeypatch):
    store = SqliteMetadataStore(tmp_path / 'metadata.db')
    target = tmp_path / 'backup.db'
    connect = sqlite3.connect
    acquired = []

    def acquire(path, *args, **kwargs):
        if path == target:
            raise sqlite3.OperationalError('injected target open failure')
        connection = connect(path, *args, **kwargs)
        acquired.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', acquire)
    with pytest.raises(sqlite3.OperationalError, match='injected target open failure'):
        store.backup(target)
    assert len(acquired) == 1
    assert_closed(acquired[0])


@pytest.mark.parametrize('fail', [False, True])
def test_backup_closes_source_and_target(tmp_path, monkeypatch, fail):
    store = SqliteMetadataStore(tmp_path / 'metadata.db')
    connect = sqlite3.connect
    acquired = []

    class BackupConnection(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            if fail:
                raise sqlite3.OperationalError('injected copy failure')
            return super().backup(target, *args, **kwargs)

    def acquire(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=BackupConnection)
        acquired.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', acquire)
    if fail:
        with pytest.raises(sqlite3.OperationalError, match='injected copy failure'):
            store.backup(tmp_path / 'backup.db')
    else:
        store.backup(tmp_path / 'backup.db')
    assert len(acquired) == 2
    for connection in acquired:
        assert_closed(connection)
