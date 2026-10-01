"""Exact reading scope, signed evidence, version isolation and current ACLs."""

from dataclasses import replace

import pytest
from test_service import call, register
from test_thread_proofs_capsule import make_post

from msg.core.codec import decode, digest, loads, unb64, wire
from msg.core.models import Signature
from msg.security.crypto import verify


async def manifest(app, ref, ranges=None):
    args = {'target': wire(ref)}
    if ranges is not None:
        args['ranges'] = ranges
    result = await call(app, 'discussion.reading_manifest', args)
    assert result.status == 'ok', wire(result)
    return wire(result.data)


async def prove(app, ref, key, user, ranges=None):
    data = await manifest(app, ref, ranges)
    args = {'target': wire(ref), 'digest': data['digest'], 'parts': data['parts']}
    result = await call(app, 'discussion.reading_prove', args, key=key, subject=user)
    assert result.status == 'ok', wire(result)
    return args, wire(result.data)


@pytest.mark.asyncio
async def test_exact_utf8_parts_signature_and_accumulated_coverage(installed):
    app, _ = installed
    key, user, _ = await register(app, 'reading-author')
    body = '第一节\nsecond\nthird'
    raw = body.encode()
    ref = await make_post(app, key, user, body)
    data = await manifest(app, ref, [{'start': 0, 'end': 9}])
    assert data['parts'] == [{'start': 0, 'end': 9, 'digest': digest(raw[:9])}]
    args, record = await prove(app, ref, key, user, [{'start': 0, 'end': 9}])
    assert record['coverage'] == [[0, 9]] and not record['complete']
    envelope = unb64(record['signed_envelope'])
    verify(key.public_key, envelope, decode(Signature, record['signature']), purpose='request')
    assert loads(envelope)['arguments'] == args
    repeated = await call(app, 'discussion.reading_prove', args, key=key, subject=user)
    assert wire(repeated.data) == record
    _, overlap = await prove(
        app,
        ref,
        key,
        user,
        [
            {'start': 6, 'end': 12},
            {'start': 10, 'end': len(raw)},
        ],
    )
    assert overlap['covered_bytes'] == len(raw) - 6
    page = await call(app, 'discussion.readings', {'id': ref.id, 'limit': 1}, key=key, subject=user)
    assert wire(page.data['my_coverage']) == [[0, len(raw)]] and page.data['my_complete']
    assert len(page.data['items']) == 1 and page.data['cursor']
    next_page = await call(
        app,
        'discussion.readings',
        {
            'id': ref.id,
            'limit': 1,
            'cursor': page.data['cursor'],
        },
    )
    assert len(next_page.data['items']) == 1 and not next_page.data.get('cursor')
    assert not next_page.data['my_complete']
    # Existing ACK counters are deliberately unaffected by range statements.
    old = await call(app, 'discussion.proofs', {'id': ref.id})
    assert not old.data['items'] and not any(old.data['proofs'].values())


@pytest.mark.asyncio
async def test_digest_range_auth_version_and_permission_boundaries(installed):
    app, _ = installed
    key, user, _ = await register(app, 'reading-version')
    ref = await make_post(app, key, user, 'abcdef')
    args, _ = await prove(app, ref, key, user)
    wrong = await call(
        app,
        'discussion.reading_prove',
        {**args, 'digest': digest(b'changed')},
        key=key,
        subject=user,
    )
    assert wrong.error.code == 'proof_digest_mismatch'
    wrong = await call(
        app,
        'discussion.reading_prove',
        {**args, 'parts': [{**args['parts'][0], 'digest': digest(b'wrong')}]},
        key=key,
        subject=user,
    )
    assert wrong.error.code == 'reading_part_digest_mismatch'
    for start, end in [(0, 7), (4, 4), (5, 3)]:
        bad = await call(
            app,
            'discussion.reading_manifest',
            {
                'target': wire(ref),
                'ranges': [{'start': start, 'end': end}],
            },
        )
        assert bad.error.code == 'reading_range_invalid'
    unsigned = await call(app, 'discussion.reading_prove', args)
    assert unsigned.status == 'error'
    unpinned = await call(app, 'discussion.reading_manifest', {'target': {'id': ref.id}})
    assert unpinned.error.code == 'reading_revision_required'
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(ref.id)).generation
    edit = await call(
        app,
        'content.post_edit',
        {
            'id': ref.id,
            'expected_revision': ref.revision,
            'body': 'ABCDEF',
        },
        key=key,
        subject=user,
        expected=((ref.id, generation),),
    )
    assert edit.status == 'ok', wire(edit)
    new = await call(app, 'discussion.readings', {'id': ref.id}, key=key, subject=user)
    assert not new.data['items'] and not new.data['my_complete']
    mixed = await call(
        app,
        'discussion.reading_prove',
        {**args, 'target': wire(edit.resources[0])},
        key=key,
        subject=user,
    )
    assert mixed.error.code == 'proof_digest_mismatch'
    historical = await call(app, 'discussion.readings', {'id': ref.id, 'revision': ref.revision})
    assert len(historical.data['items']) == 1
    stale = await call(
        app,
        'discussion.readings',
        {
            'id': ref.id,
            'cursor': app.cursors.encode(
                'readings', digest({'id': ref.id, 'revision': ref.revision}), ['', '']
            ),
        },
    )
    assert stale.status == 'error'
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(ref.id)
        await tx.replace(
            replace(resource, mode=0o700, generation=resource.generation + 1), resource.generation
        )
    for operation, payload in [
        ('discussion.reading_manifest', {'target': wire(ref)}),
        ('discussion.readings', {'id': ref.id, 'revision': ref.revision}),
    ]:
        denied = await call(app, operation, payload)
        assert denied.error.code == 'permission_denied'


@pytest.mark.asyncio
async def test_lines_preserve_crlf_utf8_and_reject_out_of_bounds(installed):
    app, _ = installed
    key, user, _ = await register(app, 'reading-lines')
    # A trailing LF terminates the last line; it does not create an extra one.
    ref = await make_post(app, key, user, '甲\r\n\nthird\n')
    result = await call(
        app,
        'discussion.reading_manifest',
        {
            'target': wire(ref),
            'line_ranges': [{'start': 1, 'end': 1}, {'start': 2, 'end': 3}],
        },
    )
    assert result.status == 'ok', wire(result)
    assert wire(result.data['parts']) == [
        {'start': 0, 'end': 5, 'digest': digest('甲\r\n'.encode())},
        {'start': 5, 'end': 12, 'digest': digest(b'\nthird\n')},
    ]
    for start, end in [(0, 1), (3, 2), (4, 4)]:
        bad = await call(
            app,
            'discussion.reading_manifest',
            {
                'target': wire(ref),
                'line_ranges': [{'start': start, 'end': end}],
            },
        )
        assert bad.status == 'error'
    conflicting = await call(
        app,
        'discussion.reading_manifest',
        {
            'target': wire(ref),
            'ranges': [{'start': 0, 'end': 1}],
            'line_ranges': [{'start': 1, 'end': 1}],
        },
    )
    assert conflicting.error.code == 'reading_selection_conflict'
    # Line boundaries can cross content-store streaming chunk boundaries.
    from types import SimpleNamespace

    from msg.plugins.reading_proofs import line_ranges

    class Chunks:
        async def read(self, content):
            for chunk in [b'a\r', b'\nb', b'\n', b'c']:
                yield chunk

    fake = SimpleNamespace(content=SimpleNamespace(media_type='text/plain'))
    assert await line_ranges(
        SimpleNamespace(contents=Chunks()), fake, [{'start': 2, 'end': 3}]
    ) == [{'start': 3, 'end': 6}]


@pytest.mark.asyncio
async def test_real_client_and_cli_reading_roundtrip(installed, tmp_path, monkeypatch, capsys):
    import httpx
    from test_service import NOW

    from msg import cli
    from msg.client import ClientState, MsgClient
    from msg.transports.client import HTTPTransport
    from msg.transports.http import create_app

    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url=app.settings.service_url,
    ) as http:
        directory = tmp_path / 'reader'
        client = MsgClient(
            ClientState(directory, server=app.settings.service_url),
            HTTPTransport(app.settings.service_url, http=http),
            clock=lambda: NOW,
        )
        assert (await client.register('cli-reading')).status == 'ok'
        post = await client.call(
            'content.post_create', {'parent': '/main', 'body': 'one\ntwo\nthree'}
        )
        assert post.status == 'ok'
        ref = post.resources[0]
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        monkeypatch.setattr(
            cli,
            'MsgClient',
            lambda state, transport: MsgClient(state, transport, clock=lambda: NOW),
        )

        async def invoke(*command):
            args = cli.parser().parse_args([
                '--config-dir',
                str(directory),
                '--server',
                app.settings.service_url,
                *command,
            ])
            status = await cli.run(args)
            return status, loads(capsys.readouterr().out.encode().strip())

        status, first = await invoke('prove-reading', ref.id, ref.revision, '--lines', '2:2')
        assert status == 0 and first['data']['coverage'] == [[4, 8]]
        assert first['data']['signed_envelope'] and not first['data']['complete']
        status, second = await invoke(
            'prove-reading', ref.id, ref.revision, '--part', '0:4', '--part', '8:13'
        )
        assert status == 0 and second['data']['covered_bytes'] == 9
        status, history = await invoke('readings', ref.id, '--revision', ref.revision)
        assert status == 0 and history['data']['my_complete'] and len(history['data']['items']) == 2
        status, whole = await invoke('prove-reading', ref.id, ref.revision)
        assert status == 0 and whole['data']['complete']


@pytest.mark.asyncio
async def test_token_claims_are_explicitly_unsigned(installed):
    from test_service import temporary_v3_args

    app, _ = installed
    args, rid, _, _ = temporary_v3_args()
    temporary = await call(app, 'identity.temporary', args, rid=rid, contract_version=3)
    assert temporary.status == 'ok', wire(temporary)
    user = temporary.data['subject_id']
    token = (temporary.data['credential_id'], unb64(temporary.data['token']))
    posted = await call(
        app,
        'content.post_create',
        {'parent': '/tmp', 'body': 'token reading'},
        subject=user,
        token=token,
    )
    assert posted.status == 'ok', wire(posted)
    ref = posted.resources[0]
    data = await manifest(app, ref)
    proof = await call(
        app,
        'discussion.reading_prove',
        {
            'target': wire(ref),
            'digest': data['digest'],
            'parts': data['parts'],
        },
        subject=user,
        token=token,
    )
    assert proof.status == 'ok', wire(proof)
    assert proof.data['auth'] == 'token' and proof.data['complete']
    assert 'signature' not in proof.data and 'signed_envelope' not in proof.data
