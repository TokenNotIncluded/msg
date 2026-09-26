import asyncio
import io
import os
import socket
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
import uvicorn

from msg.application import Application
from msg.admin.root import _provision,_approve_csr
from msg.client import ClientState,MsgClient
from msg.config import write_example
from msg.core.codec import canonical,loads
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from msg.transports.stdio import serve_stdio
from test_service import NOW


@pytest.mark.asyncio
async def test_local_stdio_signs_tools_and_never_exposes_root(installed,tmp_path):
    app,_=installed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),base_url=app.settings.service_url) as http:
        client=MsgClient(ClientState(tmp_path/'cli',server=app.settings.service_url),
            HTTPTransport(app.settings.service_url,http=http),clock=lambda:NOW)
        await client.register('stdio-agent')
        request={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{
            'name':'content.post_create','arguments':{'parent':'/main','body':'stdio message'}}}
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},
                  {'jsonrpc':'2.0','id':2,'method':'tools/list'},request,request,
                  {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'root.cert.issue','arguments':{}}}]
        source=io.BytesIO(b''.join(canonical(m)+b'\n' for m in messages));target=io.BytesIO()
        await serve_stdio(client,source,target)
        replies=[loads(line) for line in target.getvalue().splitlines()]
        assert len(replies)==5
        assert replies[2]['result']['structuredContent']['status']=='ok'
        assert replies[3]['result']['structuredContent']['replayed'] is True
        assert 'error' in replies[4]
        for tool in replies[1]['result']['tools']:
            assert 'packet' not in tool['inputSchema']['properties']
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0]==1


@pytest.mark.asyncio
async def test_real_cli_process_and_stdio_over_loopback(tmp_path):
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    sock.setblocking(False)
    url=f'http://127.0.0.1:{port}'
    app=Application(write_example(tmp_path/'server-etc',tmp_path/'server-data',url))
    csr,root=await _provision(app,'a-long-test-passphrase')
    await _approve_csr(app,csr,root,expected_digest=None,operator='test')
    server=uvicorn.Server(uvicorn.Config(create_app(app),log_level='critical',access_log=False))
    task=asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started:break
        await asyncio.sleep(.02)
    assert server.started
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1]/'src')}
    base=[sys.executable,'-m','msg.cli','--config-dir',str(tmp_path/'client'),'--server',url]
    async def cli(*args,input=None):
        process=await asyncio.create_subprocess_exec(*base,*args,env=env,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await asyncio.wait_for(process.communicate(input),20)
        assert process.returncode==0,err.decode()+out.decode()
        return [loads(line) for line in out.splitlines()]
    try:
        registration=(await cli('identity','new','real-cli'))[0]
        assert registration['status']=='ok'
        posted=(await cli('--transport','path_get','post','/main','--text','from subprocess'))[0]
        assert posted['status']=='ok'
        rid=posted['resources'][0]['id']
        reply=(await cli('reply',rid,'--text','reply subprocess'))[0]
        assert reply['status']=='ok'
        read=(await cli('read',rid,'--ack'))[0]
        assert read['read']['status']=='ok' and read['ack']['status']=='ok',read
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},
          {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'content.post_create',
           'arguments':{'parent':'/main','body':'real stdio'}}}]
        responses=await cli('mcp',input=b''.join(canonical(m)+b'\n' for m in messages))
        assert responses[1]['result']['structuredContent']['status']=='ok'
    finally:
        server.should_exit=True
        await task
        sock.close()
