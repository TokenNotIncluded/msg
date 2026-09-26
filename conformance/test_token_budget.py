"""Measure actual tokenizer tokens; byte counts are not a token estimate."""
import json
import os
from pathlib import Path
import httpx
import pytest
from msg.application import Application
from msg.admin.root import _provision,_approve_csr
from msg.config import write_example
from msg.client import ClientState,MsgClient
from msg.core.codec import canonical,wire
from msg.core.executor import result_wire
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app

@pytest.mark.asyncio
async def test_real_token_and_roundtrip_budget(tmp_path,pg_dsn):
    import tiktoken
    encoding=tiktoken.get_encoding('cl100k_base')
    app=Application(write_example(tmp_path/'etc',tmp_path/'data','http://testserver',
                                  postgres_dsn=pg_dsn))
    csr,root=await _provision(app,'token-budget-test-passphrase')
    await _approve_csr(app,csr,root,expected_digest=None,operator='conformance-fixture')
    records=[]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),base_url=app.settings.service_url) as http:
            transport=HTTPTransport(app.settings.service_url,http=http)
            client=MsgClient(ClientState(tmp_path/'client',server=app.settings.service_url),transport)
            await client.register('budget-agent')
            async def measured(name,args,budget=900):
                before=transport.calls
                packet=client.prepare(name,args)
                result=client.checked(await client.send(packet))
                text=canonical(result_wire(result)).decode()
                request_text=canonical(packet).decode()
                record={'operation':name,'input_tokens':len(encoding.encode(request_text)),
                    'output_tokens':len(encoding.encode(text)),'output_bytes':len(text.encode()),
                    'roundtrips':transport.calls-before}
                assert record['roundtrips']==1,record
                assert record['output_tokens']<=budget,record
                records.append(record)
                return result
            post=await measured('content.post_create',{'parent':'/main','body':'A small coordination message.'})
            await measured('discussion.reply',{'target':wire(post.resources[0]),'body':'Received; ready for the next step.'})
            await measured('discussion.thread',{'id':post.resources[0].id},budget=2500)
            await measured('transfer.open',{'direction':'upload','size':0})
            target=Path(os.getenv('MSG_BENCHMARK_PATH',str(tmp_path/'token-budget.json')))
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(json.dumps({'tokenizer':'cl100k_base','measurements':records},indent=2))
    finally:
        await app.close()
