"""HTTP 树形页面只使用当前授权的节点，原始读取保持原样。"""

from dataclasses import replace

import httpx
import pytest
from test_service import call, register

from msg.core.codec import wire
from msg.core.identifiers import hex_id
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_nested_branches_survive_pagination_and_hidden_parent_without_extra_disclosure(
    installed,
):
    app, _ = installed
    key, subject, cert = await register(app, 'reply-tree-reader')

    async def write(operation, arguments):
        result = await call(app, operation, arguments, key=key, subject=subject, certs=(cert,))
        assert result.status == 'ok', wire(result)
        return result.resources[0]

    root = await write(
        'content.post_create', {'parent': '/main', 'name': 'root-tree', 'body': '# 根任务'}
    )
    branch = await write(
        'discussion.reply', {'target': wire(root), 'body': '# 私有父帖标题\n\n私有父帖正文。'}
    )
    child = await write(
        'discussion.reply', {'target': wire(branch), 'body': '# 后续进展\n\n当前验证结果。'}
    )
    sibling = await write('discussion.reply', {'target': wire(root), 'body': '# 另一分工'})
    bad = await call(
        app,
        'discussion.reply',
        {'target': wire(root), 'body': '不能自选父关系', 'relations': []},
        key=key,
        subject=subject,
        certs=(cert,),
    )
    assert bad.error.code == 'schema_validation'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        path = '/*' + hex_id(root.id) + '/thread'
        full = await http.get(path)
        page = await http.get(path, headers={'Accept': 'text/html'})
        assert page.status_code == 200 and page.text.count('<article>') == 4
        assert 'data-depth="2"' in page.text and '当前验证结果。' in page.text
        assert f'href="#reply-{hex_id(branch.id)}"' in page.text
        first = await http.get(path + '?limit=2')
        continuation = await http.get(first.json()['next'], headers={'Accept': 'text/html'})
        assert continuation.text.count('<article>') == 2
        assert '父帖不在当前页' in continuation.text
        # Equal timestamps are ordered by resource ID, not creation order.
        branch_present = f'id="reply-{hex_id(branch.id)}"' in continuation.text
        child_present = f'id="reply-{hex_id(child.id)}"' in continuation.text
        assert (f'href="#reply-{hex_id(branch.id)}"' in continuation.text) == (
            branch_present and child_present
        )
        assert (await http.get(path)).json() == full.json()
        async with app.metadata.transaction(write=True) as tx:
            parent = await tx.resource(branch.id)
            await tx.replace(
                replace(parent, mode=0o600, generation=parent.generation + 1), parent.generation
            )
        filtered = await http.get(path, headers={'Accept': 'text/html'})
        assert filtered.status_code == 200 and filtered.text.count('<article>') == 3
        assert '私有父帖标题' not in filtered.text and '私有父帖正文' not in filtered.text
        assert f'href="#reply-{hex_id(branch.id)}"' not in filtered.text
        assert '父帖不在当前页' in filtered.text and '后续进展' in filtered.text
        assert hex_id(child.id) in filtered.text and hex_id(sibling.id) in filtered.text
