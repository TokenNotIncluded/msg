"""System notices remain readable without bypassing real contact privacy."""

from dataclasses import replace
from datetime import timedelta

import pytest
from read_only_evidence import readonly_evidence
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.core.codec import wire
from msg.workers.maintenance import run_maintenance


@pytest.mark.asyncio
async def test_browser_system_notice_has_no_fake_user_and_reads_have_no_effects(oauth, monkeypatch):
    app, key, subject, http = oauth
    todo = await call(
        app,
        'identity.todo_put',
        {
            'name': 'browser-due',
            'title': 'Private browser reminder',
            'due_at': wire(app.clock() - timedelta(hours=1)),
        },
        key=key,
        subject=subject,
    )
    assert todo.status == 'ok', wire(todo)
    assert await run_maintenance(app, 'deliver_due_todos', scheduled=True) == {
        'delivered_todo_reminders': 1
    }
    assert (await http.get('/@oauth-owner/in')).status_code == 401
    await browser_login(oauth)
    async with readonly_evidence(app, monkeypatch):
        for accept in ('text/html', 'text/markdown'):
            response = await http.get('/@oauth-owner/in', headers={'Accept': accept})
            assert response.status_code == 200, response.text
            assert 'System notification' in response.text
            assert '/@None' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('mailbox', ['inbox', 'outbox'])
async def test_real_private_contact_still_hides_name_and_path(installed, monkeypatch, mailbox):
    app, _ = installed
    author_key, author, _ = await register(app, 'contact-author')
    recipient_key, recipient, _ = await register(app, 'contact-recipient')
    post = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'Readable reference with a private contact'},
        key=author_key,
        subject=author,
    )
    assert post.status == 'ok', wire(post)
    sent = await call(
        app,
        'communication.send',
        {'recipient': recipient, 'resource': {'id': post.resources[0].id}},
        key=author_key,
        subject=author,
    )
    assert sent.status == 'ok', wire(sent)
    key, subject, hidden, label = (
        (recipient_key, recipient, author, 'sender_contact')
        if mailbox == 'inbox'
        else (author_key, author, recipient, 'recipient_contact')
    )
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(hidden)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    async with readonly_evidence(app, monkeypatch):
        result = await call(app, 'communication.' + mailbox, {}, key=key, subject=subject)
        assert result.status == 'ok', wire(result)
        item = next(item for item in result.data['items'] if item['id'] == sent.data['message_id'])
        assert item[label] == {'name': 'Private account'}
        assert item['preview']['excerpt'] == 'Readable reference with a private contact'
