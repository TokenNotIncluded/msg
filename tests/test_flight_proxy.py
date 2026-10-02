"""A real TLS proxy upgrades the game path while ordinary HTTP stays separate."""

import threading
from contextlib import contextmanager

import aiohttp
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from test_nginx_secret_logs import _free_port, _proxy


@contextmanager
def websocket_backend():
    async def echo(socket):
        await socket.accept()
        await socket.send_text(await socket.receive_text())
        await socket.close()

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=[WebSocketRoute('/_flight', echo)]),
            host='127.0.0.1',
            port=port,
            ws='websockets-sansio',
            ws_max_size=8192,
            access_log=False,
            log_config=None,
            lifespan='off',
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            assert thread.is_alive(), 'WebSocket backend exited during startup'
            threading.Event().wait(0.01)
        else:
            pytest.fail('WebSocket backend did not start')
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.asyncio
async def test_real_tls_upgrade_and_non_game_paths(tmp_path):
    with websocket_backend() as port, _proxy(tmp_path, port) as proxy:
        origin = f'https://127.0.0.1:{proxy.tls}'
        headers = {'Host': 'msg.example.org', 'Origin': 'https://msg.example.org'}
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(origin + '/_flight', headers=headers, ssl=False) as socket:
                await socket.send_str('flight-proxy-test-only')
                message = await socket.receive(timeout=5)
                assert message.type == aiohttp.WSMsgType.TEXT
                assert message.data == 'flight-proxy-test-only'
            with pytest.raises(aiohttp.WSServerHandshakeError) as rejected:
                await client.ws_connect(origin + '/not-flight', headers=headers, ssl=False)
            assert rejected.value.status == 404
            async with client.get(origin + '/_flight', headers=headers, ssl=False) as ordinary:
                assert ordinary.status == 404
