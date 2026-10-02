"""Post manifests expose bounded, pinned attachments under current target authority."""

import hashlib
import time
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_authorization_sources import grant
from test_service import NOW, call, register
from test_share_grants_v2 import invoke, ok

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.core.models import BlobRef, Relation, ResourceRef
from msg.core.requests import request_for
from msg.plugins import attachment_projection
from msg.transports.http import create_app


class ManifestTx:
    def __init__(self):
        self.resources = {}
        self.revisions = {}
        self.resource_reads = []
        self.revision_reads = []

    async def resource(self, rid):
        self.resource_reads.append(rid)
        if rid not in self.resources:
            raise Failure('not_found')
        return self.resources[rid]

    async def revision(self, ref):
        self.revision_reads.append(ref)
        key = (ref.id, ref.revision)
        if key not in self.revisions:
            raise Failure('revision_not_found')
        return self.revisions[key]


class ManifestContents:
    def __init__(self):
        self.payloads = {}
        self.reads = []
        self.error = None

    async def read(self, content, byte_range):
        self.reads.append((content.digest, byte_range))
        if self.error is not None:
            raise self.error
        start, end = byte_range
        assert start == 0 and end <= 512
        yield self.payloads[content.digest][start:end]


@pytest.fixture
def manifest(monkeypatch):
    tx = ManifestTx()
    contents = ManifestContents()
    denied = {}
    authority_reads = []

    async def check_access(app, ctx, request, transaction, rid, permission):
        assert transaction is tx and permission == 'read'
        authority_reads.append(rid)
        if rid in denied:
            raise Failure(denied[rid])

    monkeypatch.setattr(attachment_projection, 'check_access', check_access)
    return SimpleNamespace(
        tx=tx,
        contents=contents,
        app=SimpleNamespace(contents=contents),
        ctx=SimpleNamespace(deadline_monotonic=time.monotonic() + 60),
        denied=denied,
        authority_reads=authority_reads,
    )


def attachment(manifest, rid='attachment-one', *, media_type='image/png', body=b'private bytes'):
    ref = ResourceRef(id=rid, revision=rid + '-published')
    blob = BlobRef(
        digest='sha256:' + hashlib.sha256(body).hexdigest(), size=len(body), media_type=media_type
    )
    manifest.tx.resources[rid] = SimpleNamespace(
        type='attachment', state='active', name='private-name.png', revision=ref.revision
    )
    manifest.tx.revisions[(rid, ref.revision)] = SimpleNamespace(
        id=ref.revision, content=blob, summary=None
    )
    manifest.contents.payloads[blob.digest] = body
    return ref


async def descriptors(manifest, *refs, other_relations=()):
    revision = SimpleNamespace(
        relations=(*other_relations, *(Relation(type='attachment', target=ref) for ref in refs))
    )
    return await attachment_projection.attachment_descriptors(
        manifest.app, manifest.ctx, None, manifest.tx, revision
    )


def unavailable(ref):
    return {'ref': wire(ref), 'available': False, 'name': 'Unavailable attachment', 'kind': 'file'}


@pytest.mark.parametrize(
    ('media_type', 'kind'),
    [
        ('image/png', 'image'),
        ('IMAGE/GIF; charset=binary', 'image'),
        ('video/mp4', 'video'),
        ('audio/ogg', 'audio'),
        ('application/pdf', 'file'),
        ('image/svg+xml', 'file'),
        ('text/html', 'file'),
    ],
)
async def test_binary_and_active_documents_are_metadata_only(manifest, media_type, kind):
    ref = attachment(manifest, media_type=media_type)
    manifest.tx.resources[ref.id].revision = 'newer-current-version'
    items, more = await descriptors(manifest, ref)
    item = items[0]
    assert not more and item['available'] and item['kind'] == kind
    assert item['ref'] == wire(ref)
    assert item['raw_url'] == f'/_id/{ref.id}/revisions/{ref.revision}/raw'
    assert item['media_type'] == media_type and item['size'] == len(b'private bytes')
    assert not {'content', 'data', 'preview', 'blob'} & item.keys()
    assert manifest.authority_reads == [ref.id]
    assert manifest.tx.revision_reads == [ref]
    assert manifest.contents.reads == []


async def test_large_text_has_a_byte_bounded_plain_preview_and_summary(manifest):
    body = ('# **Read** [this](https://example.test) <b>first</b>\n' + '中文正文 ' * 300).encode()
    ref = attachment(manifest, media_type='text/markdown', body=body)
    target = manifest.tx.revisions[(ref.id, ref.revision)]
    target.summary = '**Summary** <i>text</i> ' + 'word ' * 100
    items, _ = await descriptors(manifest, ref)
    item = items[0]
    assert item['kind'] == 'text' and item['available']
    assert item['preview'].startswith('# Read this first')
    assert len(item['preview']) <= 180 and item['preview_truncated'] is True
    assert len(item['summary']) <= 280 and item['summary'].startswith('Summary text')
    assert manifest.contents.reads == [(target.content.digest, (0, 512))]
    assert item['size'] == len(body) and 'content' not in item


@pytest.mark.parametrize('failure', ['denied', 'missing', 'revision', 'purged', 'file', 'floating'])
async def test_unavailable_targets_reveal_only_a_generic_placeholder(manifest, failure):
    ref = attachment(manifest)
    if failure == 'denied':
        manifest.denied[ref.id] = 'permission_denied'
    elif failure == 'missing':
        del manifest.tx.resources[ref.id]
    elif failure == 'revision':
        del manifest.tx.revisions[(ref.id, ref.revision)]
    elif failure == 'purged':
        manifest.tx.resources[ref.id].state = 'purged'
    elif failure == 'file':
        manifest.tx.resources[ref.id].type = 'file'
    else:
        ref = ResourceRef(id=ref.id)
    items, more = await descriptors(manifest, ref)
    assert items == [unavailable(ref)] and not more
    assert manifest.contents.reads == []
    if failure in {'denied', 'floating'}:
        assert manifest.tx.resource_reads == [] and manifest.tx.revision_reads == []


async def test_every_manifest_rechecks_live_target_authority(manifest):
    ref = attachment(manifest)
    first, _ = await descriptors(manifest, ref)
    assert first[0]['available']
    manifest.denied[ref.id] = 'credential_ceiling'
    before = len(manifest.tx.resource_reads)
    second, _ = await descriptors(manifest, ref)
    assert second == [unavailable(ref)]
    assert manifest.authority_reads == [ref.id, ref.id]
    assert len(manifest.tx.resource_reads) == before


@pytest.mark.parametrize(
    'error', [Failure('not_found'), Failure('permission_denied'), FileNotFoundError()]
)
async def test_failed_text_preview_discards_already_collected_metadata(manifest, error):
    ref = attachment(manifest, media_type='text/plain')
    manifest.contents.error = error
    items, _ = await descriptors(manifest, ref)
    assert manifest.contents.reads
    assert items == [unavailable(ref)]


async def test_query_cost_failure_is_not_misrepresented_as_an_unavailable_file(manifest):
    ref = attachment(manifest, media_type='text/plain')
    manifest.contents.error = Failure('query_cost_exceeded')
    with pytest.raises(Failure, match='query_cost_exceeded'):
        await descriptors(manifest, ref)
    manifest.ctx.deadline_monotonic = 0
    manifest.authority_reads.clear()
    with pytest.raises(Failure, match='query_cost_exceeded'):
        await descriptors(manifest, ref)
    assert manifest.authority_reads == []


async def test_manifest_cap_does_not_authorize_or_read_the_overflow(manifest):
    refs = [attachment(manifest, f'attachment-{index}') for index in range(33)]
    unrelated = Relation(type='quote', target=ResourceRef(id='secret-quoted-target'))
    items, more = await descriptors(manifest, *refs, other_relations=(unrelated,))
    assert len(items) == 32 and more
    assert manifest.authority_reads == [ref.id for ref in refs[:32]]
    assert manifest.contents.reads == []


async def test_post_publication_pins_private_sources_and_rechecks_retracted_attachment(installed):
    app, _ = installed
    owner = await register(app, 'attachment-manifest-owner')
    reader = await register(app, 'attachment-manifest-reader')
    post = ok(
        await invoke(app, owner, 'content.post_create', {'parent': '/main', 'body': 'See files.'})
    )
    post_id = post.resources[0].id
    image_bytes = b'published image bytes\x00\xff'
    long_text = b'Long note with explicit full reading. ' * 1200
    sources = []
    attached_refs = []
    for name, body, media in (
        ('image.png', image_bytes, 'image/png'),
        ('notes.md', long_text, 'text/markdown'),
    ):
        source = ok(
            await invoke(
                app,
                owner,
                'content.file_put',
                {
                    'parent': '/@attachment-manifest-owner/files',
                    'name': name,
                    'data': b64(body),
                    'media_type': media,
                },
            )
        )
        source_id = source.resources[0].id
        ok(
            await invoke(
                app,
                owner,
                'content.chmod',
                {'id': source_id, 'mode': '0600'},
                expected=((source_id, source.data['generation']),),
            )
        )
        async with app.metadata.transaction(write=False) as tx:
            source_mode = (await tx.resource(source_id)).mode
            generation = (await tx.resource(post_id)).generation
        published = ok(
            await invoke(
                app,
                owner,
                'content.attach',
                {'post': post_id, 'source': wire(source.resources[0]), 'name': name},
                expected=((post_id, generation),),
            )
        )
        attached = published.data['attachment']
        attached_refs.append(attached)
        sources.append(source.resources[0])
        async with app.metadata.transaction(write=False) as tx:
            assert (await tx.resource(source_id)).mode == source_mode == 0o600
            assert (await tx.resource(attached['id'])).mode == (
                await tx.resource(post_id)
            ).mode & 0o777
        denied = await call(app, 'discovery.get', {'id': source_id})
        assert denied.error and denied.error.code == 'permission_denied'

    pinned_post = published.resources[0]
    original = ok(
        await call(
            app,
            'discovery.get',
            {'id': post_id, 'revision': post.resources[0].revision},
        )
    ).data
    assert original['content'] == 'See files.'
    assert not original['attachments'] and original['attachments_more'] is False
    projection = ok(await call(app, 'discovery.get', {'id': post_id})).data
    assert projection['content'] == 'See files.' and projection['attachments_more'] is False
    image, text = projection['attachments']
    assert image['ref'] == attached_refs[0] and image['kind'] == 'image'
    assert (
        image['size'] == len(image_bytes)
        and image['digest'] == 'sha256:' + hashlib.sha256(image_bytes).hexdigest()
    )
    assert text['ref'] == attached_refs[1] and text['kind'] == 'text'
    assert (
        text['size'] == len(long_text) and len(text['preview']) <= 180 and text['preview_truncated']
    )
    assert 'content' not in image and 'content' not in text
    assert b64(image_bytes) not in str(projection) and long_text.decode() not in str(projection)
    async with app.metadata.transaction(write=False) as tx:
        source_generation = (await tx.resource(sources[0].id)).generation
    ok(
        await invoke(
            app,
            owner,
            'file.write',
            {
                'id': sources[0].id,
                'base_revision': sources[0].revision,
                'data': b64(b'new source bytes'),
            },
            expected=((sources[0].id, source_generation),),
        )
    )
    pinned = ok(
        await call(
            app,
            'discovery.get',
            {'id': post_id, 'revision': pinned_post.revision, 'fields': ['attachments']},
        )
    ).data
    assert pinned == {'attachments': projection['attachments']}
    stat = ok(await invoke(app, owner, 'file.stat', {'id': post_id})).data
    assert 'attachments' not in stat and 'content' not in stat

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        page_json = await http.get(f'/_id/{post_id}/json')
        assert page_json.status_code == 200
        assert page_json.json()['attachments'] == wire(projection['attachments'])
        page_html = await http.get(projection['path'], headers={'accept': 'text/html'})
        assert page_html.status_code == 200 and page_html.headers['content-type'].startswith(
            'text/html'
        )
        assert page_html.text.count('<section class="attachments"') == 1
        assert page_html.text.count('class="attachment-item"') == 2
        assert '\n## Attachments\n' not in page_html.text
        assert 'class="attachment-image"' in page_html.text
        assert image['raw_url'] in page_html.text and 'image/png' in page_html.text
        page_markdown = await http.get(projection['path'], headers={'accept': 'text/markdown'})
        assert page_markdown.status_code == 200
        assert page_markdown.headers['content-type'].startswith('text/markdown')
        assert page_markdown.text.count('## Attachments') == 1
        assert image['raw_url'] in page_markdown.text and text['raw_url'] in page_markdown.text
        assert image['digest'][7:] in page_markdown.text and 'Source: ' in page_markdown.text
        for page in (page_json, page_html, page_markdown):
            assert long_text.decode() not in page.text and b64(image_bytes) not in page.text
        old_path = projection['path'] + '/revisions/' + post.resources[0].revision
        old_html = await http.get(old_path, headers={'accept': 'text/html'})
        assert old_html.status_code == 200 and 'See files.' in old_html.text
        assert '<section class="attachments"' not in old_html.text

    attachment_id = image['ref']['id']
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(attachment_id)).generation
    ok(
        await invoke(
            app,
            owner,
            'content.chmod',
            {'id': attachment_id, 'mode': '0600'},
            expected=((attachment_id, generation),),
        )
    )
    share_id = await grant(app, owner, attachment_id, reader[1])
    packet = request_for(
        'discovery.raw',
        image['ref'],
        app.settings.service_url,
        signer=reader[0],
        subject=reader[1],
        expires_at=NOW + timedelta(seconds=60),
    )
    headers = {'x-msg-request': b64(canonical(packet))}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        raw = await http.get(image['raw_url'], headers=headers)
        assert raw.status_code == 200 and raw.content == image_bytes
        assert raw.headers['content-type'] == 'image/png'
        assert raw.headers['accept-ranges'] == 'bytes'
        assert raw.headers['content-disposition'].startswith('attachment;')
        partial = await http.get(image['raw_url'], headers={**headers, 'range': 'bytes=0-5'})
        assert partial.status_code == 206 and partial.content == image_bytes[:6]
        assert partial.headers['content-range'] == f'bytes 0-5/{len(image_bytes)}'
        assert partial.headers['content-length'] == '6'
        allowed = ok(await invoke(app, reader, 'discovery.get', {'id': post_id})).data
        assert allowed['attachments'][0]['available']
        ok(await invoke(app, owner, 'sharing.revoke', {'grant_id': share_id}, version=2))
        retracted = ok(
            await invoke(
                app, reader, 'discovery.get', {'id': post_id, 'revision': pinned_post.revision}
            )
        ).data
        assert retracted['content'] == 'See files.'
        assert retracted['attachments'][0] == unavailable(ResourceRef(**image['ref']))
        assert retracted['attachments'][1]['available']
        revoked_html = await http.get(projection['path'], headers={'accept': 'text/html'})
        assert revoked_html.status_code == 200
        assert 'Unavailable attachment' in revoked_html.text
        assert 'image.png' not in revoked_html.text and image['raw_url'] not in revoked_html.text
        denied = await http.get(
            image['raw_url'],
            headers={**headers, 'if-none-match': raw.headers['etag'], 'range': 'bytes=0-5'},
        )
        assert denied.status_code == 403 and denied.json()['error']['code'] == 'permission_denied'
        assert image_bytes not in denied.content and 'etag' not in denied.headers
        assert 'content-range' not in denied.headers and 'content-disposition' not in denied.headers
