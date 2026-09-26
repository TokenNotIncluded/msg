"""Disposable installs use real SQLite/Git/Ed25519, not mocked principals."""
from datetime import UTC,datetime
from pathlib import Path
import shutil
import pytest

from msg.application import Application
from msg.admin.root import _provision,_approve_csr
from msg.config import write_example,load_settings

NOW=datetime(2026,9,27,tzinfo=UTC)

@pytest.fixture(scope='session')
async def installation_seed(tmp_path_factory):
    directory=tmp_path_factory.mktemp('msg-seed')
    settings=write_example(directory/'etc',directory/'data','http://testserver')
    app=Application(settings,clock=lambda:NOW)
    csr,root=await _provision(app,'correct-horse-test-passphrase')
    await _approve_csr(app,csr,root,expected_digest=None,operator='test-fixture')
    await app.close()
    return directory,root

@pytest.fixture
async def installed(tmp_path,installation_seed):
    seed,root=installation_seed
    shutil.copytree(seed/'etc',tmp_path/'etc')
    shutil.copytree(seed/'data',tmp_path/'data')
    config=tmp_path/'etc'/'server.toml'
    config.write_text(config.read_text().replace(str(seed),str(tmp_path)))
    app=Application(load_settings(tmp_path/'etc'),clock=lambda:NOW)
    await app.load()
    yield app,root
    await app.close()
