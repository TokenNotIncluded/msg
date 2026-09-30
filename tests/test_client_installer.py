"""The installer is a read-only, packaged bootstrap with GET/HEAD parity."""

import os
import subprocess
from importlib.resources import files

import httpx
import pytest

from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_install_endpoint_is_packaged_read_only_and_head_matches(installed):
    app, _ = installed
    script = files('msg.data').joinpath('install.sh').read_bytes()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get('/install')
        assert response.status_code == 200
        assert response.content == script
        assert response.headers['content-type'].startswith('text/plain')
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['cache-control'] == 'no-cache'
        head = await http.head('/install')
        assert head.status_code == 200 and not head.content
        assert head.headers['content-length'] == str(len(script))
        assert (await http.post('/install', content='overwrite')).status_code == 405
        assert (await http.get('/install?source=elsewhere')).status_code == 400


def test_checksum_failure_keeps_existing_launcher_and_identity(tmp_path):
    home = tmp_path / 'home'
    bin_dir = home / '.local/bin'
    release_root = home / '.local/share/msg/client-releases'
    old = release_root / 'previous/bin/msg'
    old.parent.mkdir(parents=True)
    old.write_text('#!/bin/sh\nexit 0\n')
    old.chmod(0o755)
    bin_dir.mkdir(parents=True)
    launcher = bin_dir / 'msg'
    launcher.symlink_to(old)
    identity = home / '.local/share/msg/services/example.test/identity.key'
    identity.parent.mkdir(parents=True)
    identity.write_text('identity marker')
    fake = tmp_path / 'fake-bin'
    fake.mkdir()
    curl = fake / 'curl'
    curl.write_text('#!/bin/bash\nprintf "invalid download" > "${@: -1}"\n')
    curl.chmod(0o755)
    result = subprocess.run(
        ['bash', str(files('msg.data').joinpath('install.sh'))],
        env={
            **os.environ,
            'HOME': str(home),
            'XDG_DATA_HOME': str(home / '.local/share'),
            'PATH': str(fake) + os.pathsep + os.environ['PATH'],
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0 and 'uv checksum mismatch' in result.stderr
    assert launcher.readlink() == old
    assert identity.read_text() == 'identity marker'
    assert not (release_root / '.install-lock').exists()
    assert not [entry for entry in release_root.iterdir() if entry.name != 'previous']
