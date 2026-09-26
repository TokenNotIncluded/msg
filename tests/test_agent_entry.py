"""The public agent entry is an ordinary read-only resource."""

import httpx

from msg.transports.http import create_app


async def test_global_agents_document_is_public_and_read_only(installed):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url=app.settings.service_url,
    ) as client:
        response = await client.get('/AGENTS.md')
        assert response.status_code == 200, response.text
        assert '/-/d' in response.text
        assert '/-/p/' in response.text
        assert (await client.head('/AGENTS.md')).status_code == 200
        assert (await client.post('/AGENTS.md', content=b'overwrite')).status_code == 405


async def test_public_skill_directory_has_an_explicit_empty_state(installed):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url=app.settings.service_url,
    ) as client:
        response = await client.get('/.agents/skills/')
        assert response.status_code == 200


async def test_service_entry_skill_is_a_read_only_resource(installed):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url=app.settings.service_url,
    ) as client:
        response = await client.get('/.agents/skills/msg-entry/SKILL.md')
        assert response.status_code == 200, response.text
        assert '/-/d' in response.text
        assert (await client.post('/.agents/skills/msg-entry/SKILL.md')).status_code == 405
