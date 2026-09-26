"""Disposable installs use real PostgreSQL/Git/Ed25519, not mocked principals."""
from datetime import UTC,datetime
import shutil
import uuid
import psycopg
from psycopg import sql
import pytest

from msg.application import Application
from msg.admin.root import _provision,_approve_csr
from msg.config import write_example,load_settings

NOW=datetime(2026,9,27,tzinfo=UTC)

def _create_database(cluster, name, *, template=None):
    with psycopg.connect(cluster.format(database='postgres'),autocommit=True) as connection:
        query=sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name))
        if template is not None:
            query+=sql.SQL(' TEMPLATE {}').format(sql.Identifier(template))
        connection.execute(query)


@pytest.fixture(scope='session')
async def installation_seed(tmp_path_factory,pg_cluster):
    directory=tmp_path_factory.mktemp('msg-seed')
    database='msg_seed_'+uuid.uuid4().hex
    _create_database(pg_cluster,database)
    dsn=pg_cluster.format(database=database)
    settings=write_example(directory/'etc',directory/'data','http://testserver',postgres_dsn=dsn)
    app=Application(settings,clock=lambda:NOW)
    csr,root=await _provision(app,'correct-horse-test-passphrase')
    await _approve_csr(app,csr,root,expected_digest=None,operator='test-fixture')
    await app.close()
    return directory,root,database,dsn

@pytest.fixture
async def installed(tmp_path,installation_seed,pg_cluster):
    seed,root,seed_database,seed_dsn=installation_seed
    database='msg_test_'+uuid.uuid4().hex
    _create_database(pg_cluster,database,template=seed_database)
    dsn=pg_cluster.format(database=database)
    shutil.copytree(seed/'etc',tmp_path/'etc')
    shutil.copytree(seed/'data',tmp_path/'data')
    config=tmp_path/'etc'/'server.toml'
    config.write_text(config.read_text().replace(str(seed),str(tmp_path)).replace(seed_dsn,dsn))
    app=Application(load_settings(tmp_path/'etc'),clock=lambda:NOW)
    await app.load()
    yield app,root
    await app.close()
    with psycopg.connect(pg_cluster.format(database='postgres'),autocommit=True) as connection:
        connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(database)))
