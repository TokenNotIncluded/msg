from dataclasses import replace

import httpx
import pytest
from test_service import call, register

from msg.core.codec import canonical, digest, wire
from msg.core.identifiers import hex_id
from msg.transports.http import create_app
from msg.transports.http_routes import describe_resource, post_title, thread_summary


def test_summary_fallback_counts_unicode_characters_and_does_not_cut_authored_summary():
    body = '你好🙂' * 20
    assert thread_summary({'content': body}) == {
        'summary': body[:20] + '…',
    }
    assert thread_summary({'content': 'short'}) == {'summary': 'short'}
    assert thread_summary({'summary': 'A complete authored summary.', 'content': body}, 2) == {
        'summary': 'A complete authored summary.',
    }
    assert post_title({'name': 'p_' + 'a' * 32 + '.md'}) == {}
    assert post_title({'name': 'Real title.md'}) == {'title': 'Real title'}


def test_front_matter_escapes_values_and_precedes_the_body():
    import json

    title = 'Title: "quoted"\n---\ninjected: true'
    summary = 'Summary\nwith another line'
    page = describe_resource({
        'type': 'post',
        'id': 'r_' + 'a' * 32,
        'revision': 'v_' + 'b' * 32,
        'name': title + '.md',
        'summary': summary,
        'content': 'The real body.',
        'tags': ['one', 'two'],
        'created_at': '2026-10-01T00:00:00Z',
    })
    front, body = page.removeprefix('---\n').split('\n---\n', 1)
    metadata = {
        key: json.loads(value)
        for key, value in (line.split(': ', 1) for line in front.splitlines())
    }
    assert metadata['title'] == title and metadata['summary'] == summary
    assert metadata['tags'] == ['one', 'two']
    assert body.startswith('\nThe real body.')
    assert 'Revision:' not in body and 'ID:' not in body and 'Time:' not in body


@pytest.mark.asyncio
async def test_registration_respects_an_issuer_without_summary_versions(installed, monkeypatch):
    app, _ = installed
    summary_operations = {
        f'{name}@2'
        for name in (
            'content.post_create',
            'content.post_edit',
            'content.post_write',
            'discussion.reply',
            'discussion.quote',
            'discussion.repost',
        )
    }
    async with app.metadata.transaction(write=False) as tx:
        issuer = await app.online_issuer(tx)
    older = replace(
        issuer,
        issuance=replace(
            issuer.issuance,
            issue_grants=tuple(
                replace(grant, operations=grant.operations - summary_operations)
                for grant in issuer.issuance.issue_grants
            ),
        ),
    )

    async def older_issuer(tx):
        return older

    monkeypatch.setattr(app, 'online_issuer', older_issuer)
    key, uid, cert = await register(app, 'older-summary-issuer')
    async with app.metadata.transaction(write=False) as tx:
        issued = await tx.certificate(cert)
        assert all(not (grant.operations & summary_operations) for grant in issued.grants)
        assert all([
            await app.certificates.allowed_issuance(grant, older.issuance, tx)
            for grant in issued.grants
        ])
    result = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'Old posting still works.'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    assert result.status == 'ok', result.error


@pytest.mark.asyncio
async def test_optional_summary_is_revisioned_bounded_preserved_and_authorized(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'summary-author')

    async def write(op, args, **kw):
        result = await call(
            app,
            op,
            args,
            key=key,
            subject=uid,
            certs=(cert,),
            contract_version=2 if 'summary' in args else 1,
            **kw,
        )
        assert result.status == 'ok', result.error
        return result

    original = await write(
        'content.post_create',
        {
            'parent': '/main',
            'name': 'Readable title',
            'summary': 'A short explanation.',
            'body': 'Body ' * 100,
        },
    )
    rid = original.resources[0].id
    reply = await write(
        'discussion.reply', {'target': {'id': rid}, 'body': 'Fallback reply text ' * 5}
    )
    authored_reply = await write(
        'discussion.reply',
        {
            'target': {'id': rid},
            'name': 'Reply title',
            'body': 'Reply body',
            'summary': 'Reply summary',
        },
    )
    too_long = await call(
        app,
        'content.post_create',
        {
            'parent': '/main',
            'body': 'not created',
            'summary': '🙂' * 281,
        },
        key=key,
        subject=uid,
        certs=(cert,),
        contract_version=2,
    )
    assert too_long.status == 'error'
    edited = await write(
        'content.post_edit',
        {
            'id': rid,
            'expected_revision': original.resources[0].revision,
            'summary': 'Updated summary.',
        },
        expected=((rid, original.data['generation']),),
    )
    async with app.metadata.transaction(write=False) as tx:
        before = await tx.revision(original.resources[0])
        after = await tx.revision(edited.resources[0])
        assert before.summary == 'A short explanation.' and after.summary == 'Updated summary.'
        assert before.content == after.content
        assert (
            digest({
                k: v for k, v in wire(after).items() if k not in {'manifest_digest', 'signature'}
            })
            == after.manifest_digest
        )
        # An absent optional summary must not alter legacy manifest bytes.
        legacy = await tx.revision(reply.resources[0])
        assert 'summary' not in wire(legacy)
        assert b'"summary"' not in canonical(wire(legacy))
    body_edit = await write(
        'content.post_edit',
        {
            'id': rid,
            'expected_revision': edited.resources[0].revision,
            'body': 'Changed body',
        },
        expected=((rid, edited.data['generation']),),
    )
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.revision(body_edit.resources[0])).summary == 'Updated summary.'
    short = '/*' + hex_id(rid)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        tree = (await http.get(short + '/thread?limit=1&preview=8')).json()
        assert tree['items'][0]['title'] == 'Readable title'
        assert tree['items'][0]['summary'] == 'Updated summary.'
        rest = await http.get(tree['next'])
        assert rest.status_code == 200 and 'preview=8' in tree['next']
        items = (await http.get(short + '/thread')).json()['items']
        fallback = next(item for item in items if item['id'] == hex_id(reply.resources[0].id))
        assert fallback['summary'] == ('Fallback reply text ' * 5)[:20] + '…'
        assert set(fallback) == {'id', 'revision', 'summary', 'reply_to'}
        assert (
            next(item for item in items if item['id'] == hex_id(authored_reply.resources[0].id))[
                'summary'
            ]
            == 'Reply summary'
        )
        page = await http.get(short)
        assert page.text.startswith('---\n')
        assert 'title: "Readable title"' in page.text and 'summary: "Updated summary."' in page.text
        assert page.text.index('summary:') < page.text.index('---\n\nChanged body')
        historical = await call(
            app, 'discovery.get', {'id': rid, 'revision': original.resources[0].revision}
        )
        assert historical.data['summary'] == 'A short explanation.'
        diff = (
            await http.get(
                short
                + '/diff/'
                + hex_id(original.resources[0].revision)
                + '/'
                + hex_id(edited.resources[0].revision)
            )
        ).json()
        assert diff['diff'] == ''
        assert diff['summary'] == {'from': 'A short explanation.', 'to': 'Updated summary.'}
        for preview in ('-1', '1001', '²'):
            assert (await http.get(short + '/thread?preview=' + preview)).status_code == 400
        cleared = await write(
            'content.post_edit',
            {
                'id': rid,
                'expected_revision': body_edit.resources[0].revision,
                'summary': '',
            },
            expected=((rid, body_edit.data['generation']),),
        )
        async with app.metadata.transaction(write=False) as tx:
            revision = await tx.revision(cleared.resources[0])
            assert revision.summary is None
            assert revision.content == (await tx.revision(body_edit.resources[0])).content
        assert 'summary:' not in (await http.get(short)).text.split('\n---\n', 1)[0]
        restored = await write(
            'content.post_rollback',
            {
                'id': rid,
                'base_revision': cleared.resources[0].revision,
                'target_revision': original.resources[0].revision,
            },
            expected=((rid, cleared.data['generation']),),
        )
        async with app.metadata.transaction(write=False) as tx:
            revision = await tx.revision(restored.resources[0])
            assert revision.summary == 'A short explanation.'
            assert revision.content == (await tx.revision(original.resources[0])).content
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(rid)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
        assert (await http.get(short + '/thread')).status_code == 403
