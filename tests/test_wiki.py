"""Shared wiki permissions, retained history and browser conflict handling."""

from dataclasses import replace

import httpx
import pytest
from test_oauth import browser_login, oauth as oauth
from test_post_actions import action
from test_service import NOW, call, register

from msg.core.codec import wire
from msg.core.identifiers import hex_id
from msg.core.models import ResourceRef
from msg.core.wiki import sync_wiki
from msg.security.oauth import OAuthService
from msg.transports.http import create_app
from msg.transports.wiki_actions import wiki_actions_html


@pytest.mark.asyncio
async def test_every_user_can_edit_public_wiki_and_history_is_retained(installed):
    app, _ = installed
    author_key, author, _ = await register(app, 'wiki-author')
    editor_key, editor, _ = await register(app, 'wiki-peer')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/wiki', 'name': 'knowledge', 'body': 'before'},
        key=author_key,
        subject=author,
    )
    assert created.status == 'ok', wire(created)
    ref = created.resources[0]
    edited = await call(
        app,
        'content.post_edit',
        {'id': ref.id, 'expected_revision': ref.revision, 'body': 'after'},
        key=editor_key,
        subject=editor,
        expected=((ref.id, created.data['generation']),),
    )
    assert edited.status == 'ok', wire(edited)
    async with app.metadata.transaction(write=False) as tx:
        resource = await tx.resource(ref.id)
        latest = await tx.revision(ResourceRef(id=ref.id))
        assert resource.mode == 0o666
        assert latest.author == author and latest.subject == editor
        assert latest.parents == (ref.revision,)
    stale = await call(
        app,
        'content.post_edit',
        {'id': ref.id, 'expected_revision': ref.revision, 'body': 'lost update'},
        key=author_key,
        subject=author,
        expected=((ref.id, created.data['generation']),),
    )
    assert stale.status == 'error' and stale.error.code == 'generation_conflict'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        path = '/*' + hex_id(ref.id)
        assert 'after' in (await http.get(path)).text
        history = await http.get(path + '/history')
        assert len(history.json()['revisions']) == 2
        diff = await http.get(path + '/diff')
        assert '-before' in diff.json()['diff'] and '+after' in diff.json()['diff']
        rendered = await http.get(path + '/diff', headers={'Accept': 'text/html'})
        assert rendered.status_code == 200 and '<pre' in rendered.text
        assert '-before' in rendered.text and '+after' in rendered.text
        rendered = await http.get(path + '/history', headers={'Accept': 'text/html'})
        assert rendered.status_code == 200 and 'Edit history' in rendered.text
        assert hex_id(editor) in rendered.text
        old = await http.get(path + '/rev/' + hex_id(ref.revision))
        assert 'before' in old.text
        html = await http.get(path, headers={'Accept': 'text/html'})
        assert 'id="wiki-actions"' in html.text and 'Edit article' in html.text
        assert 'sha256-' in html.headers['content-security-policy']
        assert (await http.post('/wiki')).status_code == 405
    for op, args in (
        ('content.chmod', {'id': ref.id, 'mode': '0600'}),
        ('content.archive', {'id': ref.id}),
        ('content.move', {'id': ref.id, 'parent': '/main'}),
        ('content.purge', {'id': ref.id, 'reason': 'erase history'}),
    ):
        denied = await call(
            app,
            op,
            args,
            key=author_key,
            subject=author,
            expected=((ref.id, edited.data['generation']),),
        )
        assert denied.status == 'error' and denied.error.code == 'wiki_shared_resource', wire(
            denied
        )
    restored = await call(
        app,
        'content.post_rollback',
        {
            'id': ref.id,
            'base_revision': edited.resources[0].revision,
            'target_revision': ref.revision,
        },
        key=editor_key,
        subject=editor,
        expected=((ref.id, edited.data['generation']),),
    )
    assert restored.status == 'ok', wire(restored)
    async with app.metadata.transaction(write=False) as tx:
        revision = await tx.revision(ResourceRef(id=ref.id))
        assert revision.id not in {ref.revision, edited.resources[0].revision}
        assert (await app.contents.read_bytes(revision.content)).decode() == 'before'
        assert len((await tx.history(ref.id)).items) == 3
    ordinary = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'private editor'},
        key=author_key,
        subject=author,
    )
    denied = await call(
        app,
        'content.post_edit',
        {
            'id': ordinary.resources[0].id,
            'expected_revision': ordinary.resources[0].revision,
            'body': 'no',
        },
        key=editor_key,
        subject=editor,
        expected=((ordinary.resources[0].id, ordinary.data['generation']),),
    )
    assert denied.status == 'error' and denied.error.code == 'permission_denied'


@pytest.mark.asyncio
async def test_wiki_categories_cannot_exclude_other_users(installed):
    app, _ = installed
    key, user, _ = await register(app, 'wiki-category-owner')
    other_key, other, _ = await register(app, 'wiki-category-peer')
    category = await call(
        app, 'content.topic_create', {'parent': '/wiki', 'name': 'Science'}, key=key, subject=user
    )
    assert category.status == 'ok', wire(category)
    ref = category.resources[0]
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one(
            'SELECT role FROM topic_memberships WHERE topic=? AND subject=?', (ref.id, user)
        ) == ('member',)
    for op, args in (
        ('content.topic_ban', {'id': ref.id, 'subject_id': other}),
        ('content.topic_configure', {'id': ref.id, 'policy': {'editable': False}}),
        ('content.topic_policy_set', {'id': ref.id, 'membership_policy': 'invite'}),
        ('content.chmod', {'id': ref.id, 'mode': '0700'}),
    ):
        result = await call(
            app, op, args, key=key, subject=user, expected=((ref.id, category.data['generation']),)
        )
        assert result.status == 'error' and result.error.code == 'wiki_shared_resource', wire(
            result
        )
    result = await call(
        app,
        'content.post_create',
        {'parent': ref.id, 'body': 'shared'},
        key=other_key,
        subject=other,
    )
    assert result.status == 'ok', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(result.resources[0].id)).mode == 0o666


@pytest.mark.asyncio
async def test_existing_wiki_upgrade_preserves_revisions_and_is_idempotent(installed):
    app, _ = installed
    key, user, _ = await register(app, 'wiki-legacy')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/wiki', 'body': 'old knowledge'},
        key=key,
        subject=user,
    )
    ref = created.resources[0]
    async with app.metadata.transaction(write=True) as tx:
        original = await tx.resource(ref.id)
        await tx.replace(
            replace(original, mode=0o600, generation=original.generation + 1), original.generation
        )
        tx.set_setting('wiki_policy_version', 0)
        tx.set_setting(
            'policy:t_wiki',
            {'editable': False, 'retention_seconds': 60, 'rules': 'Community guidance'},
        )
        await sync_wiki(tx, app.registry, NOW)
        upgraded = await tx.resource(ref.id)
        assert upgraded.mode == 0o666 and upgraded.revision == original.revision
        assert tx.setting('policy:t_wiki') == {'rules': 'Community guidance'}
        assert len((await tx.history(ref.id)).items) == 1
        await sync_wiki(tx, app.registry, NOW)
        assert (await tx.resource(ref.id)).generation == upgraded.generation


@pytest.mark.asyncio
async def test_browser_wiki_create_edit_conflicts_and_scope(oauth):
    app, _, _, http = oauth
    author_key, author, _ = await register(app, 'browser-wiki-author')
    article = await call(
        app,
        'content.post_create',
        {'parent': '/wiki', 'body': 'First version'},
        key=author_key,
        subject=author,
    )
    ref = article.resources[0]
    cookie = await browser_login(oauth)
    async with app.metadata.transaction(write=False) as tx:
        browser_subject, credential, token = await OAuthService(app).browser_credentials(tx, cookie)
    bypass = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'Outside wiki'},
        subject=browser_subject,
        token=(credential, token),
    )
    assert bypass.status == 'error' and bypass.error.code == 'credential_ceiling', wire(bypass)
    page = await http.get('/wiki', headers={'Accept': 'text/html'})
    assert page.status_code == 200 and 'New article' in page.text
    page = await http.get('/_id/' + ref.id, headers={'Accept': 'text/html'})
    assert 'First version</textarea>' in page.text
    changed = await action(
        oauth,
        'content.post_edit',
        ref.id,
        body='Second version',
        revision=ref.revision,
        generation=article.data['generation'],
    )
    assert changed.status_code == 200, changed.text
    stale = await action(
        oauth,
        'content.post_edit',
        ref.id,
        body='Stale',
        revision=ref.revision,
        generation=article.data['generation'],
    )
    assert stale.status_code == 403 and stale.json()['error']['code'] == 'generation_conflict'
    new = await action(oauth, 'content.post_create', 't_wiki', name='browser-entry', body='New')
    assert new.status_code == 200, new.text
    denied = await action(oauth, 'content.post_create', 't_main', name='outside', body='No')
    assert denied.status_code == 400 and denied.json()['error'] == 'wiki_only'


def test_browser_editor_never_opens_with_incomplete_content():
    resource = {'id': 'r_' + 'a' * 32, 'revision': 'v_' + 'b' * 32, 'generation': 1, 'type': 'post'}
    assert 'data-open' not in wiki_actions_html(resource, None, '')
    assert 'data-open' not in wiki_actions_html(dict(resource, content='a' * 20001), None, '')
    safe = wiki_actions_html(
        dict(resource, content='</textarea><script>alert(1)</script>'), None, ''
    )
    assert '<script>alert' not in safe and '&lt;/textarea&gt;' in safe
