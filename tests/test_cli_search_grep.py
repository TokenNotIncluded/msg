"""Search and grep CLI use the signed operations and explicit bounded pages."""
import httpx
import pytest

from msg import cli
from msg.client import ClientState, MsgClient
from msg.core.codec import loads
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app
from test_service import NOW


@pytest.mark.asyncio
async def test_cli_search_one_page_cursor_and_scoped_grep(installed,tmp_path,monkeypatch,capsys):
    app,_=installed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        directory=tmp_path/'client'
        client=MsgClient(ClientState(directory,server=app.settings.service_url),
                         HTTPTransport(app.settings.service_url,http=http),clock=lambda:NOW)
        assert (await client.register('cli-search')).status=='ok'
        for body in ('CLI needle first', 'CLI needle second'):
            assert (await client.call('content.post_create',
                                      {'parent':'/main','body':body})).status=='ok'
        monkeypatch.setitem(cli.TRANSPORTS,'http',
                            lambda server:HTTPTransport(server,http=http))
        monkeypatch.setattr(cli,'MsgClient',lambda state,transport:
                            MsgClient(state,transport,clock=lambda:NOW))

        async def invoke(*command):
            args=cli.parser().parse_args(['--config-dir',str(directory),
                '--server',app.settings.service_url,*command])
            status=await cli.run(args)
            return status,loads(capsys.readouterr().out.encode().strip())

        status,first=await invoke('search','/main','CLI needle','--field','body',
                                  '--limit','1','--order','name')
        assert status==0 and first['status']=='ok'
        assert len(first['data']['items'])==1 and first['data']['cursor']
        status,second=await invoke('search','--cursor',first['data']['cursor'])
        assert status==0 and len(second['data']['items'])==1
        assert first['data']['items'][0]['ref']['id']!=second['data']['items'][0]['ref']['id']
        status,grepped=await invoke('grep','/main','needle','--ignore-case',
                                    '--max-matches','1')
        assert status==0 and len(grepped['data']['matches'])==1
        assert grepped['data']['truncated'] is True
        status,counted=await invoke('grep','/main','needle','--ignore-case','--count-only')
        assert status==0 and counted['data']['count']==2


def test_cli_search_grep_reject_unbounded_or_mixed_options():
    p=cli.parser()
    parsed=p.parse_args(['search','--cursor','opaque','--limit','100'])
    assert parsed.cursor=='opaque' and parsed.limit==100
    with pytest.raises(SystemExit):
        p.parse_args(['grep','/main','needle','--count-only','--files-with-matches'])
