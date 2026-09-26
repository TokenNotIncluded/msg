"""Retention and rebuilding apply deployment-wide, never personal storage quotas."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from datetime import timedelta
import hashlib
import os
import time
from pathlib import Path

from msg.constants import ROOT_SPACE, ONLINE_CA
from msg.core.codec import canonical, decode, digest, loads, wire, parse_time
from msg.core.errors import Failure, require
from msg.core.models import AuditEvent, Event, ResourceRef, Revision, Resource, TransferSession
from msg.plugins.common import new_id


async def purge_revisions(app, tx, resource, *, actor, request_id, reason):
    revisions = [decode(Revision,loads(raw)) for (raw,) in tx.execute(
        'SELECT body FROM revisions WHERE resource_id=?',(resource.id,))]
    tx.set_setting('purge_record:'+resource.id, {'revisions':wire(revisions),'reason':reason,'time':wire(app.clock())})
    changed = replace(resource, state='purged', revision=None, generation=resource.generation+1,
                      modified_at=app.clock(), modified_by=actor)
    await tx.replace(changed,resource.generation)
    tx.execute('DELETE FROM revisions WHERE resource_id=?',(resource.id,),write=True)
    tx.execute('DELETE FROM projections WHERE resource_id=?',(resource.id,),write=True)
    # A surviving reply keeps its own revision. Relations into this tombstone
    # remain stable references, not dangling filesystem paths.
    event = Event(id=new_id('audit'), type='lifecycle.purge', time=app.clock(), request_id=request_id,
        actor=actor, subject=actor, resources=(ResourceRef(id=resource.id),), data={'reason':reason,'system_policy':True})
    await tx.append_audit(AuditEvent(event=event,authority=(),before_digest=digest(resource),after_digest=digest(changed),
        previous_digest=None,entry_digest='',result='purged'))
    return len(revisions)


async def _cleanup(app,tx, *, scheduled):
    counts={'expired_resources':0,'expired_transfers':0}
    if not tx.setting('runtime_config',{}).get('cleanup_enabled',True):
        return counts
    now=app.clock()
    policies=tx.rows("SELECT key,value FROM settings WHERE key LIKE 'policy:%'")
    for key,raw in policies:
        policy=loads(raw)
        if 'retention_seconds' not in policy:
            continue
        rid=key.removeprefix('policy:')
        last=tx.setting('cleanup_last:'+rid)
        if scheduled and last and (now-parse_time(last)).total_seconds()<policy.get('cleanup_interval_seconds',3600):
            continue
        cutoff=now-timedelta(seconds=policy['retention_seconds'])
        # Children are captured under this transaction. Descendants are evaluated
        # through the unique safety parent, never reply/quote relations.
        rows=tx.rows('WITH RECURSIVE descendants(id,depth) AS '
            '(SELECT id,1 FROM resources WHERE parent=? UNION ALL '
            'SELECT r.id,d.depth+1 FROM resources r JOIN descendants d ON r.parent=d.id) '
            'SELECT r.body,d.depth FROM descendants d JOIN resources r ON r.id=d.id ORDER BY d.depth DESC', (rid,))
        for raw,_depth in rows:
            resource=decode(Resource,loads(raw))
            if resource.state=='purged' or resource.created_at>cutoff:
                continue
            # Controlled identities/credentials/certificates never expire through
            # a generic topic rule. A container with retained children stays.
            if resource.type not in {'post','file','attachment','skill','template','topic','keystore'}:
                continue
            if tx.one("SELECT id FROM resources WHERE parent=? AND state<>'purged' LIMIT 1",(resource.id,)):
                continue
            await purge_revisions(app,tx,resource,actor=ONLINE_CA,request_id='retention:'+rid+':'+wire(now),reason='retention_expired')
            counts['expired_resources']+=1
        tx.set_setting('cleanup_last:'+rid,wire(now))
    for (raw,) in tx.execute('SELECT body FROM transfers'):
        transfer=decode(TransferSession,loads(raw))
        if transfer.expires_at<=now and transfer.state!='expired':
            await tx.save_transfer(replace(transfer,state='expired',generation=transfer.generation+1),transfer.generation)
            tx.execute('DELETE FROM chunks WHERE transfer_id=?',(transfer.id,),write=True)
            counts['expired_transfers']+=1
    tx.execute('DELETE FROM email_challenges WHERE expires<=?',(wire(now),),write=True)
    return counts


async def _rebuild(app,tx):
    tx.execute('DELETE FROM projections',write=True)
    count=0
    for (raw,) in tx.execute("SELECT body FROM resources WHERE revision IS NOT NULL AND state<>'purged'"):
        resource=decode(Resource,loads(raw))
        rev=await tx.revision(ResourceRef(id=resource.id,revision=resource.revision))
        if rev.content.size<=1048576 and (rev.content.media_type.startswith('text/') or rev.content.media_type=='application/msg-template'):
            text=(await app.contents.read_bytes(rev.content)).decode('utf-8',errors='replace')
            tx.execute('INSERT INTO projections VALUES (?,?)',(resource.id,text),write=True)
            count+=1
    # Rebuilding a projection emits no duplicate email, certificate or message.
    return {'indexed_resources':count}


async def _collect(app,tx, *, grace_seconds=3600):
    """Remove unreferenced content after a grace period and retained Git ancestry.

    All remaining revisions are retained, including ordinary archived resources.
    A topic commit can retain old parents: only unreferenced commits/objects are
    pruned. Shared bytes referenced elsewhere are never promised to be erased.
    """
    live=set()
    revisions=set()
    for (raw,) in tx.execute('SELECT body FROM revisions'):
        rev=decode(Revision,loads(raw)); live.add(rev.content.digest[7:]); revisions.add(rev.id)
    # In-flight chunks and sealed uploads are roots until the transfer expires.
    for raw, in tx.execute('SELECT c.body FROM chunks c JOIN transfers t ON t.id=c.transfer_id'):
        live.add(loads(raw)['content']['digest'][7:])
    recent=time.time()-grace_seconds
    removed=0
    for entry in app.contents.index.iterdir():
        if not entry.is_file() or entry.name in live or entry.stat().st_mtime>recent:
            continue
        # Ref removal is idempotent. Git's reachability remains the final guard
        # for shared/parent objects before physical pruning.
        key=entry.name
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):
            continue
        data=loads(entry.read_bytes())
        if data['kind']=='git':
            refs=await asyncio.to_thread(app.contents._run,'for-each-ref','--format=%(refname)','refs/pins','refs/staging')
            for ref in refs.splitlines():
                if ref.endswith('/'+key):
                    await asyncio.to_thread(app.contents._run,'update-ref','-d',ref)
        else:
            (app.contents.binary/key).unlink(missing_ok=True)
            pins=app.contents.path/'pins'
            if pins.exists():
                for pin in pins.glob('*/'+key):pin.unlink(missing_ok=True)
        entry.unlink(missing_ok=True)
        removed+=1
    # Only roots belonging to revisions actually removed from metadata are removed.
    refs=await asyncio.to_thread(app.contents._run,'for-each-ref','--format=%(refname)','refs/topics')
    keep_hashes={hashlib.sha256(r.encode()).hexdigest() for r in revisions}
    for ref in refs.splitlines():
        if ref.rsplit('/',1)[-1] not in keep_hashes:
            await asyncio.to_thread(app.contents._run,'update-ref','-d',ref)
    revdir=app.contents.path/'revisions'
    if revdir.exists():
        for entry in revdir.iterdir():
            if entry.name not in revisions and entry.stat().st_mtime<=recent:
                entry.unlink(missing_ok=True)
    await asyncio.to_thread(app.contents._run,'gc','--prune=1.hour.ago')
    return {'unreferenced_contents_removed':removed,'shared_bytes':'retained_if_referenced',
            'backups':'managed_separately'}


async def run_maintenance(app,action, *, scheduled=False,principal=None):
    require(action in {'cleanup_expired','rebuild_search','collect_garbage'},'unknown_maintenance_action')
    async with app.metadata.transaction(write=True) as tx:
        if not scheduled:
            require(principal is not None,'maintenance_authority_required')
            from msg.workers.effects import current_principal
            live=await current_principal(app,principal,tx)
            require(await app.authorizer.has(live,'system.maintenance','system.maintenance@1',ROOT_SPACE,tx),
                    'capability_required')
        if action=='cleanup_expired':return await _cleanup(app,tx,scheduled=scheduled)
        if action=='rebuild_search':return await _rebuild(app,tx)
        return await _collect(app,tx)
