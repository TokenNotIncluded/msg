import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from msg.core.codec import canonical,wire
from msg.core.errors import Failure
from msg.core.executor import OperationExecutor
from msg.core.requests import request_for
from msg.extensions.ssh import SSHAuthenticator,parse_command,public_key
from msg.security.crypto import Ed25519Signer
from test_service import register,call,NOW
from datetime import timedelta


def test_ssh_command_allowlist_has_no_shell_or_root_proxy():
    assert parse_command("git-upload-pack '/@alice/code.git'")==('git-upload-pack','/@alice/code.git')
    assert parse_command("msg call discovery.get '{\"id\":\"/main\"}'")[0]=='call'
    for command in ('','sh','bash -c id','msgd cert issue a','git-upload-pack /etc/passwd',
                    'git-upload-pack /@a/x.git;id','msg call identity.register {} extra','scp -t .'):
        with pytest.raises(Failure):parse_command(command)


@pytest.mark.asyncio
async def test_registered_ssh_key_binds_identity_and_ceiling(installed):
    app,_=installed
    key,uid,_=await register(app,'ssh-agent')
    ssh=Ed25519Signer.generate()
    line=Ed25519PrivateKey.from_private_bytes(ssh.private_bytes()).public_key().public_bytes(
        serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
    pop=ssh.sign(canonical({'subject_id':uid,'public_key':line}),purpose='ssh-key-add')
    grant=next(g for g in app.base_grants() if g.capability=='discovery.basic')
    added=await call(app,'identity.ssh_key_add',{'public_key':line,'proof':wire(pop),'ceiling':[wire(grant)]},key=key,subject=uid)
    assert added.status=='ok',wire(added)
    authenticator=SSHAuthenticator(app,ssh.key_id)
    executor=OperationExecutor(app.registry,app.metadata,app.contents,authenticator,app.authorizer,app.clock,app.receipt_signer)
    packet=request_for('discovery.get',{'id':'/main'},app.settings.service_url,subject=uid,expires_at=NOW+timedelta(seconds=60))
    read=await executor.execute(packet)
    assert read.status=='ok' and read.actor==uid,wire(read)
    bad=await executor.execute(request_for('content.post_create',{'parent':'/main','body':'not authorized'},
        app.settings.service_url,subject=uid,expires_at=NOW+timedelta(seconds=60)))
    assert bad.status=='error'
    revoked=await call(app,'identity.ssh_key_revoke',{'key_id':ssh.key_id},key=key,subject=uid)
    assert revoked.status=='ok',wire(revoked)
    assert (await executor.execute(packet)).error.code=='credential_revoked'

@pytest.mark.asyncio
async def test_git_reference_guard_real_commit_and_revoked_key(installed,tmp_path):
    from msg.extensions.ssh_git import ReferenceGuard,guarded_command
    from msg.extensions.repositories import NativeGitStore
    app,_=installed
    key,uid,_=await register(app,'ssh-git-owner')
    ssh=Ed25519Signer.generate()
    line=Ed25519PrivateKey.from_private_bytes(ssh.private_bytes()).public_key().public_bytes(
        serialization.Encoding.OpenSSH,serialization.PublicFormat.OpenSSH).decode()
    pop=ssh.sign(canonical({'subject_id':uid,'public_key':line}),purpose='ssh-key-add')
    grant=next(g for g in app.base_grants() if g.capability=='git.basic')
    added=await call(app,'identity.ssh_key_add',{'public_key':line,'proof':wire(pop),'ceiling':[wire(grant)]},key=key,subject=uid)
    assert added.status=='ok',wire(added)
    repo=await call(app,'git.create',{'parent':'/@ssh-git-owner','name':'code.git'},key=key,subject=uid)
    rid=repo.resources[0].id
    store=NativeGitStore(app)
    import asyncio,subprocess
    # Real Git objects with an empty tree; neither arbitrary shell nor fake refs.
    tree=(await asyncio.to_thread(store._run,rid,'mktree',input=b'')).decode().strip()
    env={**store.env,'GIT_AUTHOR_NAME':'Test','GIT_AUTHOR_EMAIL':'test@example.invalid'}
    commit=subprocess.run(['git','--git-dir',str(store.path(rid)),'commit-tree',tree],input=b'initial\n',capture_output=True,env=env,check=True).stdout.decode().strip()
    auth=SSHAuthenticator(app,ssh.key_id)
    executor=OperationExecutor(app.registry,app.metadata,app.contents,auth,app.authorizer,app.clock,app.receipt_signer)
    async def job():
        packet=request_for('git.receive',{'id':rid},app.settings.service_url,subject=uid,expires_at=NOW+timedelta(seconds=60))
        accepted=await executor.execute(packet)
        assert accepted.status=='accepted',wire(accepted)
        async with app.metadata.transaction(write=False) as tx:return await tx.job(accepted.data['job_id'])
    initial=await job()
    # Git 2.54+ invokes reference-transaction with a pre-lock `preparing`
    # phase. Exercise it explicitly so older local Git versions do not hide
    # compatibility regressions.
    probe=ReferenceGuard(app,initial)
    probe_task=asyncio.create_task(probe.run())
    changes=f"{'0'*40} {commit} refs/heads/main"
    assert (await probe.message({'state':'preparing','changes':changes}))['ok']
    await probe.message({'state':'stop'})
    await probe_task
    code=await guarded_command(app,initial,lambda _:['update-ref','refs/heads/main',commit,'0'*40])
    assert code==0
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(initial.id)).state=='done'
        assert (await tx.resource(rid)).generation==repo.data['generation']+1
        assert tx.one('SELECT COUNT(*) FROM audit')[0]>0
    # A real Git ref transaction that loses stdin after prepare must abort
    # every staged ref. This covers EOF, not host power loss or disk durability.
    interrupted=await job()
    before_refs,_=await store.refs(rid)
    async with app.metadata.transaction(write=False) as tx:
        before_generation=(await tx.resource(rid)).generation
    commands=(f'start\ncreate refs/heads/second {commit}\n'
              f'create refs/heads/third {commit}\nprepare\n').encode()
    code,output,changed=await guarded_command(app,interrupted,
        lambda _:['update-ref','--stdin'],input_data=commands,capture_output=True)
    assert code==0 and b'prepare: ok' in output and not changed
    assert (await store.refs(rid))[0]==before_refs
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(rid)).generation==before_generation
    pending=await job()
    await call(app,'identity.ssh_key_revoke',{'key_id':ssh.key_id},key=key,subject=uid)
    denied=await guarded_command(app,pending,lambda _:['update-ref','refs/heads/other',commit,'0'*40])
    assert denied!=0
    refs,_=await store.refs(rid)
    assert [r['name'] for r in refs]==['refs/heads/main']
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(pending.id)).state=='failed'

@pytest.mark.asyncio
async def test_rss_is_read_only_acl_filtered_and_escapes_titles(installed):
    import httpx
    from xml.etree import ElementTree as ET
    from msg.transports.http import create_app
    app,_=installed
    key,uid,_=await register(app,'feed-author')
    post=await call(app,'content.post_create',{'parent':'/main','body':'public'},key=key,subject=uid)
    secret=await call(app,'content.post_create',{'parent':'/@feed-author/files','body':'private'},key=key,subject=uid)
    async with app.metadata.transaction(write=False) as tx:before=tx.one('SELECT COUNT(*) FROM events')[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),base_url=app.settings.service_url) as http:
        feed=await http.get('/rss')
        assert feed.status_code==200
        ids=[x.text for x in ET.fromstring(feed.content).findall('./channel/item/guid')]
        assert post.resources[0].id in ids and secret.resources[0].id not in ids
        latest=await http.get('/latest/post')
        assert latest.status_code==200
        raw=await http.get('/_id/'+post.resources[0].id+'/raw',headers={'Range':'bytes=1-3'})
        assert raw.status_code==206 and raw.content==b'ubl'
        bad=await http.get('/_id/'+post.resources[0].id+'/raw',headers={'Range':'bytes=100-105'})
        assert bad.status_code==416
    async with app.metadata.transaction(write=False) as tx:assert tx.one('SELECT COUNT(*) FROM events')[0]==before