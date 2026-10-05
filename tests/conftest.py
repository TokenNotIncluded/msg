"""Disposable installs use real PostgreSQL/Git/Ed25519, not mocked principals."""

import shutil
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from msg.admin.root import _approve_csr, _provision
from msg.application import Application
from msg.config import load_settings, write_example

NOW = datetime(2026, 9, 27, tzinfo=UTC)


def pytest_collection_modifyitems(items):
    for item in items:
        if {'pg_cluster', 'postgres_required'}.intersection(item.fixturenames):
            item.add_marker(pytest.mark.db)


@pytest.fixture
def postgres_required():
    """声明测试内部自建的 PostgreSQL，不提前启动另一个闲置集群。"""


def _create_database(cluster, name, *, template=None):
    with psycopg.connect(cluster.format(database='postgres'), autocommit=True) as connection:
        query = sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name))
        if template is not None:
            query += sql.SQL(' TEMPLATE {}').format(sql.Identifier(template))
        connection.execute(query)


def _drop_database(cluster, name):
    with psycopg.connect(cluster.format(database='postgres'), autocommit=True) as connection:
        connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


@pytest.fixture(scope='session')
async def installation_seed(tmp_path_factory, pg_cluster):
    directory = tmp_path_factory.mktemp('msg-seed')
    database = 'msg_seed_' + uuid.uuid4().hex
    _create_database(pg_cluster, database)
    try:
        dsn = pg_cluster.format(database=database)
        settings = write_example(
            directory / 'etc', directory / 'data', 'http://testserver', postgres_dsn=dsn
        )
        app = Application(settings, clock=lambda: NOW)
        try:
            csr, root = await _provision(app, 'correct-horse-test-passphrase')
            await _approve_csr(app, csr, root, expected_digest=None, operator='test-fixture')
        finally:
            await app.close()
        # Cloning a PostgreSQL template requires all seed connections closed.
        yield directory, root, database, dsn
    finally:
        _drop_database(pg_cluster, database)


@pytest.fixture
async def installed(tmp_path, installation_seed, pg_cluster):
    seed, root, seed_database, seed_dsn = installation_seed
    database = 'msg_test_' + uuid.uuid4().hex
    _create_database(pg_cluster, database, template=seed_database)
    try:
        dsn = pg_cluster.format(database=database)
        shutil.copytree(seed / 'etc', tmp_path / 'etc')
        shutil.copytree(seed / 'data', tmp_path / 'data')
        config = tmp_path / 'etc' / 'msgd.toml'
        config.write_text(
            config.read_text().replace(str(seed), str(tmp_path)).replace(seed_dsn, dsn)
        )
        app = Application(load_settings(tmp_path / 'etc'), clock=lambda: NOW)
        try:
            await app.load()
            yield app, root
        finally:
            await app.close()
    finally:
        _drop_database(pg_cluster, database)
