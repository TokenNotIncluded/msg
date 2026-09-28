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
from msg.core.models import AuditEvent, BlobRef, Event, ResourceRef, Revision, Resource, TransferSession
from msg.plugins.common import new_id

TODO_DELIVERY_BATCH_SIZE = 64
GARBAGE_COLLECTION_INTERVAL_SECONDS = 3600


async def _release_pin(app, tx, blob, lease):
    # Restore a pre-existing pin if a later step aborts the SQL transaction.
    # Metadata writers (including the collector) share one serialization fence.
    if await app.contents.pinned(blob, lease):
        tx.on_rollback(lambda: app.contents.pin(blob, lease))
        await app.contents.unpin(blob, lease)


async def purge_revisions(app, tx, resource, *, actor, request_id, reason):
    revisions = [decode(Revision,loads(raw)) for (raw,) in tx.execute(
        'SELECT body FROM revisions WHERE resource_id=?',(resource.id,))]
    tx.set_setting('purge_record:'+resource.id, {'revisions':wire(revisions),'reason':reason,'time':wire(app.clock())})
    changed = replace(resource, state='purged', revision=None, generation=resource.generation+1,
                      modified_at=app.clock(), modified_by=actor)
    await tx.replace(changed,resource.generation)
    for revision in revisions:
        await _release_pin(app,tx,revision.content,revision.id)
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
    counts={'expired_resources':0,'expired_transfers':0,'expired_query_sources':0,
            'expired_custodial_upgrades':0}
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
            for offset,chunk_raw in tx.execute(
                    'SELECT offset,body FROM chunks WHERE transfer_id=?',(transfer.id,)):
                blob=decode(BlobRef,loads(chunk_raw)['content'])
                await _release_pin(app,tx,blob,transfer.id+':'+str(offset))
            if transfer.state=='sealed' and transfer.direction=='upload':
                # The output revision may already have been purged. The sealed
                # transfer itself retains the content identity needed to unpin.
                blob=BlobRef(digest=transfer.expected_digest,size=transfer.expected_size,
                             media_type='application/octet-stream')
                await _release_pin(app,tx,blob,transfer.id+':sealed')
            tx.execute('DELETE FROM chunks WHERE transfer_id=?',(transfer.id,),write=True)
            counts['expired_transfers']+=1
    for key,raw in tx.rows("SELECT key,value FROM settings WHERE key LIKE 'query_ref_source:%'"):
        marker=loads(raw)
        if parse_time(marker['retain_until'])>now:
            continue
        rid=key.removeprefix('query_ref_source:')
        try:
            resource=await tx.resource(rid)
        except Failure as exc:
            if exc.code!='not_found':raise
            tx.execute('DELETE FROM settings WHERE key=?',(key,),write=True)
            continue
        if resource.state=='purged':
            tx.execute('DELETE FROM settings WHERE key=?',(key,),write=True)
            continue
        # A moved, shared or revised file has become ordinary user content.
        if (resource.type!='file' or resource.owner!=marker['subject'] or
                resource.parent!=marker['parent'] or resource.revision!=marker['revision'] or
                resource.mode!=0o600):
            tx.execute('DELETE FROM settings WHERE key=?',(key,),write=True)
            continue
        if tx.one('''SELECT 1 FROM relations rel JOIN resources source
            ON source.id=rel.source_id AND source.revision=rel.revision_id
            AND source.state<>'purged' WHERE rel.target_id=? LIMIT 1''',(rid,)):
            continue
        active_reference=False
        for (transfer_raw,) in tx.execute('SELECT body FROM transfers'):
            session=decode(TransferSession,loads(transfer_raw))
            if session.expires_at>now and ((session.target and session.target.id==rid) or
                                           (session.output and session.output.id==rid)):
                active_reference=True
                break
        if active_reference:
            continue
        await purge_revisions(app,tx,resource,actor=ONLINE_CA,
            request_id='query-ref-retention:'+rid+':'+wire(now),reason='query_ref_expired')
        tx.execute('DELETE FROM settings WHERE key=?',(key,),write=True)
        counts['expired_query_sources']+=1
    tx.execute('DELETE FROM email_challenges WHERE expires<=?',(wire(now),),write=True)
    counts['expired_custodial_upgrades']=tx.execute(
        "DELETE FROM custodial_upgrades WHERE status IN ('pending','failed') AND expires_at<=?",
        (wire(now),),write=True).rowcount
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


async def _deliver_due_todos(app,tx):
    """Deliver current due todo revisions to their owner, once per due instant.

    The maintenance transaction is the only producer. A revised, completed or
    archived todo is evaluated from its current state rather than a stale job.
    """
    now=app.clock()
    delivered=0
    cursor=tx.setting('todo_delivery_cursor','')
    rows=tx.rows("SELECT id,body FROM resources WHERE type='todo' AND state='active' "
                 "AND id>? ORDER BY id LIMIT ?",(cursor,TODO_DELIVERY_BATCH_SIZE))
    if len(rows)<TODO_DELIVERY_BATCH_SIZE:
        rows+=tx.rows("SELECT id,body FROM resources WHERE type='todo' AND state='active' "
                      "AND id<=? ORDER BY id LIMIT ?",(cursor,TODO_DELIVERY_BATCH_SIZE-len(rows)))
    if rows:
        tx.set_setting('todo_delivery_cursor',rows[-1][0])
    for _id,raw in rows:
        resource=decode(Resource,loads(raw))
        if resource.revision is None or resource.mode&0o077:
            continue
        parent=await tx.resource(resource.parent)
        if (parent.name!='todos' or parent.parent!=resource.owner or
                parent.owner!=resource.owner or
                parent.mode&0o077 or parent.state!='active'):
            continue
        revision=await tx.revision(ResourceRef(id=resource.id,revision=resource.revision))
        details=loads((await app.contents.read_bytes(revision.content)).decode('utf-8'))
        due_at=details.get('due_at')
        if (not due_at or details.get('status') not in {'pending','in_progress'} or
                parse_time(due_at)>now):
            continue
        # An id stable across worker restarts and unrelated edits prevents
        # duplicate reminders, while a changed due instant is a new reminder.
        notice_id='m_'+digest(('todo_due',resource.id,due_at))[7:39]
        record={'id':notice_id,'sender':None,'actor':ONLINE_CA,
                'recipient':resource.owner,'resource':wire(ResourceRef(id=resource.id)),
                'time':wire(now),'state':'delivered','source':'todo_due',
                'due_at':due_at,'todo_revision_at_delivery':resource.revision,
                'status_at_delivery':details['status']}
        inserted=tx.execute('''INSERT INTO messages (id,sender,recipient,resource,event_id,body)
            VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING''',
            (notice_id,None,resource.owner,resource.id,notice_id,
             canonical(record).decode()),write=True).rowcount
        if not inserted:
            continue
        event=Event(id=new_id('audit'),type='identity.todo.due_notice',time=now,
            request_id='todo-due:'+notice_id,actor=ONLINE_CA,subject=resource.owner,
            resources=(ResourceRef(id=resource.id,revision=resource.revision),),
            data={'message_id':notice_id,'due_at':due_at})
        await tx.append_audit(AuditEvent(event=event,authority=(),
            before_digest=digest(resource),after_digest=digest(record),
            previous_digest=None,entry_digest='',result='delivered'))
        delivered+=1
    return {'delivered_todo_reminders':delivered}


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
    from msg.market.references import content_references
    for content in content_references(tx.execute):
        live.add(content['digest'][7:])
    recent=time.time()-grace_seconds
    removed=0
    # Explicit pins are live roots even before a Revision/Transfer pointer is
    # committed. In particular, tool jobs may hold a blob without either row.
    git_pins=await asyncio.to_thread(app.contents._run,'for-each-ref','--format=%(refname)','refs/pins')
    pinned_git={ref.rsplit('/',1)[-1] for ref in git_pins.splitlines()}
    pin_dir=app.contents.path/'pins'
    for entry in app.contents.index.iterdir():
        if not entry.is_file() or entry.name in live or entry.stat().st_mtime>recent:
            continue
        # Ref removal is idempotent. Git's reachability remains the final guard
        # for shared/parent objects before physical pruning.
        key=entry.name
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):
            continue
        if key in pinned_git or (pin_dir.exists() and any(pin_dir.glob('*/'+key))):
            continue
        data=loads(entry.read_bytes())
        if data['kind']=='git':
            refs=await asyncio.to_thread(app.contents._run,'for-each-ref','--format=%(refname)','refs/staging')
            for ref in refs.splitlines():
                if ref.endswith('/'+key):
                    await asyncio.to_thread(app.contents._run,'update-ref','-d',ref)
        else:
            binary=app.contents.binary/key
            # LFS repository links are durable roots of the shared binary CAS.
            # Dropping the content index is safe, but keep the canonical inode
            # while any repository still references it.
            if binary.exists() and binary.stat().st_nlink==1:
                binary.unlink()
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
    from msg.storage.git import LFSObjectStore
    lfs=LFSObjectStore(Path('/nonexistent'),app.settings.server.blob_dir)
    collected,freed=await asyncio.to_thread(lfs.collect_unlinked,
        older_than=recent,index=app.contents.index)
    return {'unreferenced_contents_removed':removed,'shared_bytes':'retained_if_referenced',
            'backups':'managed_separately','unlinked_lfs_objects_removed':collected,
            'unlinked_lfs_bytes_removed':freed}


async def run_maintenance(app,action, *, scheduled=False,principal=None):
    require(action in {'cleanup_expired','deliver_due_todos','rebuild_search','collect_garbage'},'unknown_maintenance_action')
    async with app.metadata.transaction(write=True) as tx:
        from msg.security.quarantine import active as quarantine_active
        if quarantine_active(tx):
            return {'skipped':'recovery_quarantine'}
        if not scheduled:
            require(principal is not None,'maintenance_authority_required')
            from msg.workers.effects import current_principal
            live=await current_principal(app,principal,tx)
            require(await app.authorizer.has(live,'system.maintenance','system.maintenance@1',ROOT_SPACE,tx),
                    'capability_required')
        if action=='cleanup_expired':return await _cleanup(app,tx,scheduled=scheduled)
        if action=='deliver_due_todos':return await _deliver_due_todos(app,tx)
        if action=='rebuild_search':return await _rebuild(app,tx)
        if scheduled:
            if not tx.setting('runtime_config',{}).get('cleanup_enabled',True):
                return {'skipped':'cleanup_disabled'}
            last=tx.setting('garbage_collection_last')
            if last and (app.clock()-parse_time(last)).total_seconds()<GARBAGE_COLLECTION_INTERVAL_SECONDS:
                return {'skipped':'not_due'}
        result=await _collect(app,tx)
        if scheduled:
            tx.set_setting('garbage_collection_last',wire(app.clock()))
        return result
