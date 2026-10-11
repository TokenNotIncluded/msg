from pathlib import Path


def replace(path, old, new, count=1):
    path = Path(path)
    text = path.read_text()
    assert text.count(old) == count, (path, text.count(old), count)
    path.write_text(text.replace(old, new))


replace('src/msg/admin/config_check.py',
        '    if isinstance(value, tuple):\n        return list(value)',
        "    if isinstance(value, tuple):\n        if field == 'recovery.custodians':\n            return [\n                {key: getattr(item, key) for key in ('id', 'name', 'recipient')} for item in value\n            ]\n        return list(value)")
replace('tests/test_flight_websocket.py', 'flight.hub.signed_browser', 'flight.hub.sessions', 2)
replace('tests/test_flight_websocket.py', 'A slow legitimate OAuth check', 'A slow legitimate key-session check')
replace('tests/test_mcp_agent.py', "connect = await tool(http, 'msg_connect', session=session)",
        "connect = await tool(http, 'msg_connect', session=session, rpc_id='connect')")
replace('tests/test_mcp_tools.py',
        "async def test_connect_requests_all_limited_scopes_and_returns_host_challenge(registry):",
        "async def test_connect_explains_native_signing_without_external_authority(registry):")
replace('tests/test_mcp_tools.py',
        "    assert connect['securitySchemes'][1]['scopes'] == [\n        'msg.mcp.read',\n        'msg.mcp.message',\n        'msg.mcp.post',\n    ]",
        "    assert 'securitySchemes' not in connect\n    assert 'oauth' not in json.dumps(catalog).lower()")
replace('tests/test_mcp_tools.py',
        "    result = await run_tool('msg_connect', {}, call=call)\n    assert result['error']['code'] == 'authentication_required'",
        "    result = await run_tool('msg_connect', {}, call=call)\n    assert result['status'] == 'ok'\n    assert result['data']['authentication'] == 'public-key-signature'\n    assert '_meta' not in result")
replace('tests/test_mcp_tools.py',
        "    assert 'Reconnect' in result['error']['hint'] and SECRET not in json.dumps(result)",
        "    assert 'local signing key' in result['error']['hint'] and SECRET not in json.dumps(result)")
replace('tests/test_post_read_views.py',
        "                assert (\n                    denied.status_code == 400\n                    and denied.json()['error']['code'] == 'invalid_request'\n                )",
        "                if header == 'Authorization':\n                    assert denied.status_code == 401\n                    assert denied.json()['error'] == 'signature_required'\n                else:\n                    assert denied.status_code == 400\n                    assert denied.json()['error']['code'] == 'invalid_request'")
replace('tests/test_public_board_read_fallback.py',
        "            assert explicit.status_code == 400\n            assert explicit.json()['error']['code'] in {'ambiguous_credentials', 'invalid_request'}",
        "            if header == 'Authorization':\n                assert explicit.status_code == 401\n                assert explicit.json()['error'] == 'signature_required'\n            else:\n                assert explicit.status_code == 400\n                assert explicit.json()['error']['code'] in {'ambiguous_credentials', 'invalid_request'}")
replace('tests/test_public_terminal.py',
        "                    assert (await http.get(path, headers={header: value})).status_code == 400",
        "                    response = await http.get(path, headers={header: value})\n                    assert response.status_code == (401 if header == 'Authorization' else 400)\n                    if header == 'Authorization':\n                        assert response.json()['error'] == 'signature_required'")
replace('tests/test_thread_panel_http.py',
        "        assert response.status_code == 400 and response.json()['error']['code'] == 'invalid_request'",
        "        if 'Authorization' in header:\n            assert response.status_code == 401\n            assert response.json()['error'] == 'signature_required'\n        else:\n            assert response.status_code == 400\n            assert response.json()['error']['code'] == 'invalid_request'", 2)
replace('tests/test_universe.py',
        "await http.get('/_universe/account', headers={'Authorization': 'Bearer inert'})\n        ).status_code == 400",
        "await http.get('/_universe/account', headers={'Authorization': 'Bearer inert'})\n        ).status_code == 401")
