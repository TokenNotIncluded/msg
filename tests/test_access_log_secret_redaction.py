"""The daemon's Uvicorn entry points disable raw-URI access logging."""
import ast
import asyncio
import logging
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn

from msg.transports.http import create_app


def _daemon_uvicorn_access_log_values():
    source = Path(__file__).parents[1] / 'src/msg/daemon.py'
    tree = ast.parse(source.read_text())
    values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'run':
            if isinstance(node.func.value, ast.Name) and node.func.value.id == 'uvicorn':
                values.append({keyword.arg: keyword.value for keyword in node.keywords
                               if keyword.arg == 'access_log'})
    return values


async def _serve_one_request(app, *, access_log, caplog):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host='127.0.0.1', port=port,
                            access_log=access_log, log_config=None, lifespan='off')
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f'http://127.0.0.1:{port}/?token=QUERY_SECRET',
                headers={'host': 'testserver'})
            assert response.status_code == 400
            await client.get(
                f'http://127.0.0.1:{port}/-/g/content.post_create/token/PATH_SECRET',
                headers={'host': 'testserver'})
        await asyncio.sleep(0.02)
    finally:
        server.should_exit = True
        await task
        sock.close()
    return caplog.text


@pytest.mark.asyncio
async def test_daemon_uvicorn_access_logs_do_not_record_secret_urls(installed, caplog):
    access_log_values = _daemon_uvicorn_access_log_values()
    assert len(access_log_values) == 2
    assert all(value.get('access_log') is not None and
               isinstance(value['access_log'], ast.Constant) and
               value['access_log'].value is False for value in access_log_values)

    app, _ = installed
    caplog.set_level(logging.INFO, logger='uvicorn.access')
    actual = await _serve_one_request(create_app(app), access_log=False, caplog=caplog)
    assert 'QUERY_SECRET' not in actual
    assert 'PATH_SECRET' not in actual
    assert 'GET /?token=QUERY_SECRET' not in actual


@pytest.mark.asyncio
async def test_uvicorn_default_access_logger_records_raw_secret_query(installed, caplog):
    """Pin the reason the daemon must keep access_log=False."""
    app, _ = installed
    caplog.set_level(logging.INFO, logger='uvicorn.access')
    actual = await _serve_one_request(create_app(app), access_log=True, caplog=caplog)
    assert 'QUERY_SECRET' in actual
    assert 'PATH_SECRET' in actual
    assert 'GET /?token=QUERY_SECRET' in actual
