"""PostgreSQL is required; Valkey is optional and never stores durable state."""

import pytest

from msg.config import load_settings, write_example
from msg.core.errors import Failure


def test_example_uses_libpq_service_without_secret(tmp_path):
    settings = write_example(tmp_path / 'etc', tmp_path / 'data')
    config = (tmp_path / 'etc' / 'msgd.toml').read_text()
    assert settings.server.postgres_dsn == 'service=msgd'
    assert settings.server.valkey_url is None
    assert 'metadata.sqlite3' not in config
    assert 'password' not in config


def test_accepts_postgres_and_optional_valkey_urls_without_revealing_secrets(tmp_path):
    settings = write_example(
        tmp_path / 'etc',
        tmp_path / 'data',
        postgres_dsn='postgresql://msgd:secret@localhost/msgd',
        valkey_url='rediss://:other-secret@localhost:6379/0',
    )
    assert settings.server.valkey_url == 'rediss://:other-secret@localhost:6379/0'
    assert 'secret' not in repr(settings)


@pytest.mark.parametrize(
    'replacement',
    [
        '',
        'postgres_dsn = ""',
        'postgres_dsn = "sqlite:///tmp/x"',
        'postgres_dsn = "service="',
        'postgres_dsn = "postgresql:///msgd"',
        'database = "/tmp/metadata.sqlite3"',
    ],
)
def test_rejects_missing_or_invalid_postgres_dsn(tmp_path, replacement):
    config_dir = tmp_path / 'etc'
    write_example(config_dir, tmp_path / 'data')
    path = config_dir / 'msgd.toml'
    text = path.read_text().replace('postgres_dsn = "service=msgd"', replacement)
    path.write_text(text)
    with pytest.raises(Failure):
        load_settings(config_dir)


@pytest.mark.parametrize('url', ['http://localhost:6379', 'redis:///0', '', 'unix://'])
def test_rejects_invalid_valkey_url(tmp_path, url):
    config_dir = tmp_path / 'etc'
    write_example(config_dir, tmp_path / 'data')
    path = config_dir / 'msgd.toml'
    path.write_text(path.read_text().replace('[storage]', f'[storage]\nvalkey_url = "{url}"'))
    with pytest.raises(Failure, match='invalid_valkey_url'):
        load_settings(config_dir)
