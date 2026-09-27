"""Two-segment dictionary details bind an operation to its namespace."""
import httpx
import pytest

from msg.transports.dictionary import build_dictionary
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_namespace_operation_dictionary_by_name_and_code(installed):
    app, _ = installed
    dictionary = build_dictionary(app.registry)
    namespace_code = dictionary.code_for('namespace', 'transfer')
    operation_code = dictionary.code_for('operation', 'transfer.part_put@1')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        by_name = await http.get('/-/d/transfer/transfer.part_put')
        assert by_name.status_code == 200, by_name.text
        assert [row['name'] for row in by_name.json()['operations']] == ['transfer.part_put']
        assert by_name.headers['etag'] == dictionary.etag_for('transfer.part_put')
        by_code = await http.get(f'/-/d/{namespace_code}/{operation_code}')
        assert by_code.status_code == 200, by_code.text
        assert [row['code'] for row in by_code.json()['operations']] == [operation_code]
        head = await http.head('/-/d/transfer/transfer.part_put')
        assert head.status_code == 200 and head.content == b''
        assert head.headers['etag'] == by_name.headers['etag']
        assert (await http.get('/-/d/transfer/transfer.part_put',
                               headers={'If-None-Match': by_name.headers['etag']})).status_code == 304


@pytest.mark.asyncio
async def test_dictionary_rejects_mismatched_or_unknown_namespace(installed):
    app, _ = installed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        for path in ('/-/d/content/transfer.part_put',
                     '/-/d/missing/transfer.part_put',
                     '/-/d/transfer/content.post_create',
                     '/-/d/transfer/part_put'):
            response = await http.get(path)
            assert response.status_code == 404, (path, response.text)
        assert (await http.post('/-/d/transfer/transfer.part_put')).status_code == 405
