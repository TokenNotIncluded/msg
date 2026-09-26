"""New installations keep durable Git, blobs, and resumable staging apart."""

from types import SimpleNamespace

from msg.config import load_settings, write_example
from msg.extensions.repositories import NativeGitStore
from msg.storage.git import GitContentStore


async def test_new_installation_uses_documented_persistent_layout(tmp_path):
    data = tmp_path / 'data'
    settings = write_example(tmp_path / 'etc', data, postgres_dsn='service=msgd')
    server = settings.server
    assert server.content_dir == data / 'git' / 'content'
    assert server.repositories_dir == data / 'git' / 'repos'
    assert server.blob_dir == data / 'blobs' / 'sha256'
    assert server.staging_dir == data / 'transfers' / 'staging'
    assert server.service_keys_dir == data / 'service'

    store = GitContentStore(server.content_dir, binary_dir=server.blob_dir,
                            staging_dir=server.staging_dir)
    binary = await store.put_bytes(b'\x00\x01', 'application/octet-stream')
    assert (server.blob_dir / binary.digest[7:]).read_bytes() == b'\x00\x01'
    assert not (server.content_dir / 'binary').exists()
    text = await store.put_bytes(b'hello', 'text/markdown')
    assert store.repo.is_dir()
    assert (server.content_dir / 'index' / text.digest[7:]).is_file()

    app = SimpleNamespace(settings=SimpleNamespace(server=server))
    assert NativeGitStore(app).path('example') == server.repositories_dir / 'example.git'

    config = tmp_path / 'etc' / 'msgd.toml'
    config.write_text('\n'.join(line for line in config.read_text().splitlines()
                                if not line.startswith(('repositories =', 'blobs =',
                                                        'service_keys ='))) + '\n')
    defaults = load_settings(tmp_path / 'etc').server
    assert defaults.repositories_dir == server.repositories_dir
    assert defaults.blob_dir == server.blob_dir
    assert defaults.service_keys_dir == server.service_keys_dir
