"""Actual owner UI -> shell stdin -> CLI -> signed HTTP -> disposable PostgreSQL.

Run this optional browser acceptance with:
uv run --extra dev --with playwright pytest -q tests/test_profile_art_cli.py
MSG_BROWSER_PATH may select an installed Chromium executable.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

import httpx
import pytest
from test_service import NOW

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import decode, unb64
from msg.core.models import BlobRef
from msg.transports.client import HTTPTransport
from msg.transports.home_page import HOME_BROWSER_HEADERS, document_html
from msg.transports.http import create_app

playwright = pytest.importorskip(
    'playwright.async_api', reason='Run browser acceptance with uv run --with playwright.'
)

FIRST_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 320">'
    '<text x="5" y="40">??? 头像 🚀 猫：$MSG_UNEXPANDED $(printf expanded) '
    "`printf expanded` apostrophe '!</text></svg>"
).encode()
SECOND_SVG = FIRST_SVG.replace('头像'.encode(), '替换头像'.encode())
THIRD_SVG = FIRST_SVG.replace('头像'.encode(), '没有签名密钥'.encode())


def shell_stdin(command, directory):
    """Execute the displayed heredoc with a capturing msg shim, then use the real CLI."""
    directory.mkdir()
    header, separator, tail = command.partition('\n')
    assert separator and command.endswith('\n')
    invocation, redirect, quoted_delimiter = header.rpartition('<<')
    assert redirect
    delimiter_match = re.fullmatch(r"\s*'([A-Z][A-Z0-9_]*)'\s*", quoted_delimiter)
    assert delimiter_match, 'The heredoc delimiter must be single-quoted.'
    delimiter = delimiter_match[1]
    assert tail.splitlines()[-1] == delimiter
    # Only the fixed command header is executed. Uploaded SVG bytes are encoded JSON data.
    assert not re.search(r'[;&|`$()\\]', invocation)
    argv = shlex.split(invocation)
    assert argv[0] == 'msg'
    capture = directory / 'captured.json'
    script = directory / 'capture.py'
    script.write_text(
        'import json, os, sys\n'
        'from pathlib import Path\n'
        "Path(os.environ['MSG_TEST_CAPTURE']).write_text(json.dumps({\n"
        "    'argv': sys.argv[1:], 'stdin': sys.stdin.buffer.read().decode('utf-8')\n"
        "}), encoding='utf-8')\n",
        encoding='utf-8',
    )
    environment = {
        'PATH': os.defpath,
        'MSG_TEST_PYTHON': sys.executable,
        'MSG_TEST_CAPTURE_SCRIPT': str(script),
        'MSG_TEST_CAPTURE': str(capture),
    }
    completed = subprocess.run(
        [
            '/bin/sh',
            '-c',
            'msg() { "$MSG_TEST_PYTHON" "$MSG_TEST_CAPTURE_SCRIPT" "$@"; }\n' + command,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert completed.stdout == completed.stderr == ''
    captured = json.loads(capture.read_text(encoding='utf-8'))
    assert captured['argv'] == argv[1:]
    assert captured['stdin'] == tail.removesuffix(delimiter + '\n')
    return captured


async def run_cli(command, config, directory, monkeypatch, capsys, *, new_request_id=None):
    captured = shell_stdin(command, directory)
    args = cli.parser().parse_args([
        '--config-dir',
        str(config),
        '--format',
        'json',
        *captured['argv'],
    ])
    assert args.arguments == '-'
    if new_request_id is not None:
        args.request_id = new_request_id
    stdin = io.TextIOWrapper(io.BytesIO(captured['stdin'].encode()), encoding='utf-8')
    capsys.readouterr()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(cli.sys, 'stdin', stdin)
            exit_code = await cli.run(args)
    finally:
        stdin.close()
    output = capsys.readouterr()
    assert output.err == ''
    return args, exit_code, json.loads(output.out)


@pytest.mark.asyncio
async def test_ui_heredoc_create_write_replay_and_missing_software_key(
    installed, tmp_path, monkeypatch, capsys
):
    executable = os.environ.get('MSG_BROWSER_PATH') or shutil.which('chromium')
    if not executable:
        pytest.skip('Install Chromium or set MSG_BROWSER_PATH for browser acceptance.')
    app, _ = installed
    packets, browser_requests, page_errors = [], [], []

    async def observe(request):
        if request.url.path in {'/-/p/file.create', '/-/p/file.write'}:
            assert request.method == 'POST'
            packets.append(json.loads(request.content))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url=app.settings.service_url,
        event_hooks={'request': [observe]},
    ) as http:
        config = tmp_path / 'cli-account'
        state = ClientState(config, server=app.settings.service_url)
        client = MsgClient(state, HTTPTransport(state.server, http=http), clock=lambda: NOW)
        registered = await client.register('profile-cli')
        assert registered.status == 'ok', registered.error
        assert state.key_path.is_file() and state.signer is not None
        profile_path = '/@profile-cli'
        meta_path = profile_path + '/AVATAR.svg/meta'
        profile_response = await http.get(profile_path + '/json')
        assert profile_response.status_code == 200
        html = document_html(
            '',
            account={'name': '@profile-cli'},
            resource=profile_response.json(),
            raw_path=profile_path,
            service_url=state.server,
        )
        # Replace connection and fixture clock only; CLI parsing, identity loading,
        # Ed25519 signing, transport envelopes and server authorization remain real.
        monkeypatch.setitem(
            cli.TRANSPORTS, 'http', lambda server, **kw: HTTPTransport(server, http=http, **kw)
        )
        monkeypatch.setattr(
            cli,
            'MsgClient',
            lambda state, transport: MsgClient(state, transport, clock=lambda: NOW),
        )

        async def route(request_route):
            request = request_route.request
            browser_requests.append((request.method, request.url))
            assert request.method == 'GET', 'The browser must only prepare signed CLI requests.'
            url = urlsplit(request.url)
            if url.path == profile_path:
                await request_route.fulfill(
                    status=200,
                    content_type='text/html',
                    headers=HOME_BROWSER_HEADERS,
                    body=html,
                )
            else:
                path = url.path + ('?' + url.query if url.query else '')
                response = await http.get(path)
                await request_route.fulfill(
                    status=response.status_code,
                    headers={
                        key: value
                        for key, value in response.headers.items()
                        if key not in {'content-length', 'content-encoding'}
                    },
                    body=response.content,
                )

        async def meta():
            response = await http.get(meta_path)
            assert response.status_code == 200
            return response.json()

        async def assert_blob(value, expected):
            raw = await client.call('discovery.raw', {'id': value['id']})
            assert raw.status == 'ok', raw.error
            blob = decode(BlobRef, raw.data['content'])
            assert b''.join([piece async for piece in app.contents.read(blob)]) == expected
            visible = await http.get(profile_path + '/art/avatar.svg')
            assert visible.status_code == 200
            assert visible.headers['x-msg-artwork-source'] == 'custom'
            assert visible.content == expected

        async with playwright.async_playwright() as tool:
            # testserver is the disposable fixture's signed authority. Allow only
            # that origin to use crypto.randomUUID, as localhost/HTTPS ordinarily do.
            browser = await tool.chromium.launch(
                executable_path=executable,
                args=[
                    '--no-sandbox',
                    '--unsafely-treat-insecure-origin-as-secure=' + state.server,
                ],
            )
            try:
                page = await browser.new_page()
                page.on('pageerror', lambda error: page_errors.append(str(error)))
                await page.route('**/*', route)
                await page.goto(state.server + profile_path)
                await page.locator('.profile-edit-link').click()

                async def prepare(svg):
                    await page.locator('.profile-art-editor input[type=file]').set_input_files({
                        'name': 'artwork.svg',
                        'mimeType': 'image/svg+xml',
                        'buffer': svg,
                    })
                    await page.locator('.profile-art-prepared').wait_for(state='visible')
                    assert (
                        'not saved yet' in await page.locator('.profile-art-status').text_content()
                    )
                    await page.wait_for_function(
                        "() => { const img=document.querySelector('.profile-art-preview img');"
                        'return img && img.complete && img.naturalWidth > 0; }'
                    )
                    preview = await page.locator('.profile-art-preview img').get_attribute('src')
                    assert base64.b64decode(preview.partition(',')[2], validate=True) == svg
                    return await page.locator('.profile-art-command').text_content()

                # This valid UTF-8 SVG forces all three standard-base64 hazards.
                standard = base64.b64encode(FIRST_SVG).decode()
                assert all(character in standard for character in '+/=')
                create_command = await prepare(FIRST_SVG)
                assert (await http.get(meta_path)).status_code == 404
                args, exit_code, created = await run_cli(
                    create_command, config, tmp_path / 'create', monkeypatch, capsys
                )
                assert args.operation == 'file.create' and not args.expect
                assert exit_code == 0 and created['status'] == 'ok', created
                create_packet = packets[-1]
                assert create_packet['proof']['signature']
                assert create_packet['subject'] == state.subject == created['actor']
                assert create_packet['request_id'] == args.request_id
                assert create_packet['arguments']['parent'] == profile_path
                assert create_packet['arguments']['name'] == 'AVATAR.svg'
                assert unb64(create_packet['arguments']['data']) == FIRST_SVG
                first_meta = await meta()
                await assert_blob(first_meta, FIRST_SVG)
                _, exit_code, replay = await run_cli(
                    create_command, config, tmp_path / 'create-replay', monkeypatch, capsys
                )
                assert exit_code == 0 and replay['replayed'] is True
                assert replay['resources'] == created['resources']
                assert await meta() == first_meta

                write_command = await prepare(SECOND_SVG)
                args, exit_code, written = await run_cli(
                    write_command, config, tmp_path / 'write', monkeypatch, capsys
                )
                assert args.operation == 'file.write'
                assert args.expect == [f'{first_meta["id"]}={first_meta["generation"]}']
                assert exit_code == 0 and written['status'] == 'ok', written
                write_packet = packets[-1]
                assert write_packet['proof']['signature']
                assert write_packet['request_id'] == args.request_id
                assert write_packet['arguments']['id'] == first_meta['id']
                assert write_packet['arguments']['base_revision'] == first_meta['revision']
                assert write_packet['expected_generations'] == [
                    [first_meta['id'], first_meta['generation']]
                ]
                assert unb64(write_packet['arguments']['data']) == SECOND_SVG
                second_meta = await meta()
                assert second_meta['id'] == first_meta['id']
                assert second_meta['revision'] != first_meta['revision']
                assert second_meta['generation'] == first_meta['generation'] + 1
                await assert_blob(second_meta, SECOND_SVG)
                _, exit_code, replay = await run_cli(
                    write_command, config, tmp_path / 'write-replay', monkeypatch, capsys
                )
                assert exit_code == 0 and replay['replayed'] is True
                assert replay['resources'] == written['resources']
                assert await meta() == second_meta

                # A new request with the old UI preconditions must not overwrite.
                _, exit_code, stale = await run_cli(
                    write_command,
                    config,
                    tmp_path / 'stale-write',
                    monkeypatch,
                    capsys,
                    new_request_id=args.request_id + '_stale',
                )
                assert exit_code == 1 and stale['status'] == 'error', stale
                assert await meta() == second_meta
                await assert_blob(second_meta, SECOND_SVG)

                unsigned_command = await prepare(THIRD_SVG)
                # Delete only this temporary account's software key. The real CLI
                # reopens its state; no mock signer or success response is supplied.
                state.key_path.unlink()
                _, exit_code, unsigned = await run_cli(
                    unsigned_command, config, tmp_path / 'unsigned-write', monkeypatch, capsys
                )
                assert exit_code == 1 and unsigned['status'] == 'error', unsigned
                assert unsigned['error']['code'] == 'authentication_required'
                assert packets[-1]['proof'] is None
                assert await meta() == second_meta
                await assert_blob(second_meta, SECOND_SVG)
                assert not page_errors
                assert browser_requests and all(method == 'GET' for method, _ in browser_requests)
            finally:
                await browser.close()
