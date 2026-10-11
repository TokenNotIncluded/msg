from pathlib import Path

p = Path('src/msg/transports/session_http.py')
s = p.read_text().replace('import hmac\n', 'import hmac\nimport re\n')
old = "        if request.headers.getlist('authorization'):\n            await json({'error': 'signature_required'}, 401)(scope, receive, send)\n            return\n"
new = '''        authorization = request.headers.getlist('authorization')
        if authorization:
            # Standard Git clients carry native, key-issued MSG credentials in
            # Basic auth. The Git adapter still validates their current scope,
            # signing source, revocation and request id; this is not OAuth.
            native_git = re.fullmatch(
                r'/-/git/[A-Za-z0-9_-]{1,128}(?:\\.git)?/(?:info/refs|git-receive-pack|info/lfs/objects(?:/batch|/[0-9a-f]{64}/[0-9]+))'
                r'|/[@&][^/]+/[^/]+\\.git/(?:info/refs|git-upload-pack|HEAD|info/lfs/objects(?:/batch|/[0-9a-f]{64}))',
                path,
            )
            if len(authorization) == 1 and authorization[0].startswith('Basic ') and native_git:
                await self.app(scope, receive, send)
                return
            await json({'error': 'signature_required'}, 401)(scope, receive, send)
            return
'''
assert s.count(old) == 1
p.write_text(s.replace(old, new))
p = Path('tests/test_oauth.py')
p.write_text(p.read_text() + '''\n\n@pytest.mark.parametrize('path', ['/-/p/discovery.get', '/-/mcp', '/@key-owner/in'])
async def test_git_basic_header_does_not_authenticate_other_routes(signed_browser, path):
    _, _, _, http = signed_browser
    response = await http.get(path, headers={'Authorization': 'Basic bmF0aXZlOmNyZWRlbnRpYWw='})
    assert response.status_code == 401
    assert response.json()['error'] == 'signature_required'


async def test_git_exception_rejects_bearer_and_duplicate_authorization(signed_browser):
    _, _, _, http = signed_browser
    path = '/-/git/any/info/refs?service=git-receive-pack'
    for headers in [
        [('Authorization', 'Bearer retired')],
        [('Authorization', 'Basic YTpi'), ('Authorization', 'Basic Yzpk')],
    ]:
        response = await http.get(path, headers=headers)
        assert response.status_code == 401
        assert response.json()['error'] == 'signature_required'
''')
p = Path('docs/KEY_IDENTITY.md')
s = p.read_text()
old = '不接受 HTTP `Authorization: Bearer ...` 作为替代。敏感操作仍要求签名。'
new = old + '\n标准 Git 客户端在专用 Git 路径仍可用 Basic 格式传输本地密钥签发的原生凭据；\n这不是第三方登录，仍检查来源密钥、范围、撤销和请求限制，不能用于网页或 MCP 登录。'
assert s.count(old) == 1
p.write_text(s.replace(old, new))
