from dataclasses import replace
import httpx
import pytest
from msg.core.codec import b64,wire,canonical,unb64
from msg.core.errors import Failure
from msg.security.sealed_box import generate_key,encrypt,decrypt
from msg.extensions.hosting import hosting_app
from msg.extensions.repositories import NativeGitStore
from test_service import register,call


def test_public_key_encryption_recipient_and_tamper_checks():
    secret,public=generate_key()
    other,other_public=generate_key()
    sealed=encrypt(b'never send plaintext credentials',public)
    assert b'plaintext' not in sealed
    assert decrypt(sealed,secret)==b'never send plaintext credentials'
    with pytest.raises(Failure):decrypt(sealed,other)
    tampered=sealed[:-2]+b'00'
    with pytest.raises(Failure):decrypt(tampered,secret)


@pytest.mark.asyncio
async def test_keystore_stores_only_ciphertext_and_has_common_acl(installed):
    app,_=installed
    ka,ua,_=await register(app,'vault-owner')
    kb,ub,_=await register(app,'vault-outsider')
    secret,public=generate_key()
    ciphertext=encrypt(b'private API key',public)
    added=await call(app,'keystore.put',{'name':'opaque-entry','format':'msg-x25519-v1','ciphertext':b64(ciphertext)},key=ka,subject=ua)
    assert added.status=='ok',wire(added)
    rid=added.resources[0].id
    read=await call(app,'keystore.get',{'id':rid},key=ka,subject=ua)
    assert read.status=='ok' and read.data['format']=='msg-x25519-v1',wire(read)
    denied=await call(app,'discovery.get',{'id':rid},key=kb,subject=ub)
    assert denied.error and denied.error.code=='permission_denied'
    raw=await call(app,'discovery.raw',{'id':rid},key=ka,subject=ua)
    assert raw.status=='ok'
    async with app.metadata.transaction(write=False) as tx:
        revision=await tx.revision(added.resources[0])
        assert decrypt(await app.contents.read_bytes(revision.content),secret)==b'private API key'
        assert (await tx.resource(rid)).type=='keystore'
    bad=await call(app,'keystore.put',{'name':'plaintext','format':'msg-x25519-v1','ciphertext':b64(b'actual secret')},key=ka,subject=ua)
    assert bad.status=='error'


@pytest.mark.asyncio
async def test_hosting_publish_is_explicit_versioned_and_other_origin(installed):
    app,_=installed
    app.settings=replace(app.settings,public_web_origin='https://pages.example.test')
    key,uid,_=await register(app,'site-owner')
    private_file=await call(app,'content.file_put',{'parent':'/@site-owner/files','name':'index.html',
        'data':b64(b'<h1>Published explicitly</h1>'),'media_type':'text/html'},key=key,subject=uid)
    assert private_file.status=='ok',wire(private_file)
    site=await call(app,'hosting.create',{'parent':'/@site-owner','name':'w'},key=key,subject=uid)
    assert site.status=='ok',wire(site)
    published=await call(app,'hosting.deploy',{'id':site.resources[0].id,
        'entries':[{'path':'index.html','source':wire(private_file.resources[0])}]},key=key,subject=uid,
        expected=((site.resources[0].id,site.data['generation']),))
    assert published.status=='ok',wire(published)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosting_app(app)),base_url='https://pages.example.test') as http:
        result=await http.get('/@site-owner/w/')
        assert result.status_code==200 and result.content==b'<h1>Published explicitly</h1>'
        assert 'set-cookie' not in result.headers
        denied=await http.get('/@site-owner/w/../../root',headers={'Host':'testserver'})
        assert denied.status_code==403
    source=await call(app,'discovery.get',{'id':private_file.resources[0].id})
    assert source.error.code=='permission_denied'
    traversal=await call(app,'hosting.deploy',{'id':site.resources[0].id,
        'entries':[{'path':'../bad','source':wire(private_file.resources[0])}]},key=key,subject=uid,
        expected=((site.resources[0].id,published.data['generation']),))
    assert traversal.status=='error'


@pytest.mark.asyncio
async def test_native_git_storage_and_public_invariant(installed,tmp_path):
    app,_=installed
    key,uid,_=await register(app,'git-owner')
    created=await call(app,'git.create',{'parent':'/@git-owner','name':'demo.git'},key=key,subject=uid)
    assert created.status=='ok',wire(created)
    rid=created.resources[0].id
    store=NativeGitStore(app)
    assert store.path(rid).is_dir()
    assert store.path(rid).is_relative_to(app.settings.server.content_dir.parent/'repositories')
    assert not store.path(rid).is_relative_to(app.contents.path)
    denied=await call(app,'content.chmod',{'id':rid,'mode':'0700'},key=key,subject=uid,
        expected=((rid,created.data['generation']),))
    assert denied.error.code=='repo_public_read_required'
    refs=await call(app,'git.refs',{'id':rid})
    assert refs.status=='ok' and list(refs.data['refs'])==[]
    from msg.transports.http import create_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),base_url='http://testserver') as http:
        result=await http.get('/@git-owner/demo.git/info/refs?service=git-upload-pack')
        assert result.status_code==200 and b'git-upload-pack' in result.content
        bad=await http.get('/@git-owner/demo.git/config')
        assert bad.status_code!=200
        push=await http.post('/@git-owner/demo.git/git-receive-pack',content=b'0000')
        assert push.status_code in {401,403,405}

@pytest.mark.asyncio
async def test_signed_bundle_push_changes_refs_once(installed,tmp_path):
    import subprocess
    from msg.workers.effects import EffectWorker
    app,_=installed
    key,uid,_=await register(app,'bundle-owner')
    repo=await call(app,'git.create',{'parent':'/@bundle-owner','name':'code.git'},key=key,subject=uid)
    work=tmp_path/'work';work.mkdir()
    def git(*args):
        return subprocess.run(['git','-C',str(work),*args],check=True,capture_output=True).stdout.decode().strip()
    git('init','--initial-branch=main')
    git('config','user.name','Test');git('config','user.email','test@example.invalid')
    (work/'README.md').write_text('public code\n')
    git('add','.');git('commit','-m','first')
    oid=git('rev-parse','HEAD')
    bundle=tmp_path/'input.bundle';git('bundle','create',str(bundle),'main')
    uploaded=await call(app,'content.file_put',{'parent':'/@bundle-owner/files','name':'input.bundle',
        'data':b64(bundle.read_bytes()),'media_type':'application/octet-stream'},key=key,subject=uid)
    args={'id':repo.resources[0].id,'bundle':wire(uploaded.resources[0]),
        'changes':[{'ref':'refs/heads/main','old':None,'new':oid}]}
    push=await call(app,'git.push',args,key=key,subject=uid,rid='one-native-push')
    assert push.status=='accepted',wire(push)
    await EffectWorker(app).run_once()
    job=await call(app,'job.get',{'id':push.data['job_id']},key=key,subject=uid)
    assert job.data['state']=='done',wire(job)
    refs=await call(app,'git.refs',{'id':repo.resources[0].id})
    assert dict(refs.data['refs'][0])=={'name':'refs/heads/main','oid':oid},wire(refs)
    replay=await call(app,'git.push',args,key=key,subject=uid,rid='one-native-push')
    assert replay.replayed
    assert not await EffectWorker(app).run_once()
