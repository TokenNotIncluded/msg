"""Finite online administration: no paths, root keys, plugin code or shell commands."""
from __future__ import annotations
from msg.constants import ROOT_SPACE
from msg.core.codec import canonical, digest, wire
from msg.core.errors import require
from msg.core.models import AuditEvent, Event, EffectJob, HandlerOutput, ResourceRef
from msg.plugins.common import new_id, registration, operation_id
from msg.plugins.communication import event_id
from msg.plugins.schemas import obj, BOOLEAN, STRING


MAINTENANCE_ACTIONS_V1 = ('cleanup_expired', 'rebuild_search', 'collect_garbage')
MAINTENANCE_ACTIONS = (*MAINTENANCE_ACTIONS_V1, 'deliver_due_todos')
RUNTIME_SCHEMA = obj({'accept_writes':BOOLEAN, 'status_message':{'type':'string','maxLength':300},
    'cleanup_enabled':BOOLEAN})


def install(app):
    op, finish = registration(app, 'system', ('identity','content'))

    async def authorized(ctx, request, tx, capability):
        require(await app.authorizer.has(ctx.principal, capability, operation_id(request), ROOT_SPACE, tx),
                'capability_required')

    @op('system.inspect', obj(), effect='read', signature=True)
    async def inspect(ctx, request, tx):
        await authorized(ctx, request, tx, 'system.inspect')
        # Deliberately omits environment, credentials, filesystem paths and keys.
        return HandlerOutput(data={'schema_version':tx.one('SELECT version FROM schema_version')[0],
            'resources':tx.one('SELECT COUNT(*) FROM resources')[0],
            'pending_jobs':tx.one("SELECT COUNT(*) FROM jobs WHERE state='pending'")[0],
            'uncertain_jobs':tx.one("SELECT COUNT(*) FROM jobs WHERE state='uncertain'")[0],
            'runtime':tx.setting('runtime_config',{'accept_writes':True,'cleanup_enabled':True})})

    @op('system.config', obj({'values':RUNTIME_SCHEMA},('values',)), signature=True)
    async def configure(ctx, request, tx):
        await authorized(ctx, request, tx, 'system.config')
        before = tx.setting('runtime_config', {'accept_writes':True,'cleanup_enabled':True})
        after = {**before, **request.arguments['values']}
        tx.set_setting('runtime_config', after)
        event = Event(id=new_id('audit'), type='system.config', time=ctx.now, request_id=request.request_id,
            actor=ctx.principal.actor, subject=ctx.principal.subject, resources=(ResourceRef(id=ROOT_SPACE),), data={})
        await tx.append_audit(AuditEvent(event=event, authority=tuple(ResourceRef(id=c) for c in ctx.principal.certificates),
            before_digest=digest(before), after_digest=digest(after), previous_digest=None, entry_digest='', result='configured'))
        return HandlerOutput(data={'runtime':after})

    @op('system.share_links_set',obj({'enabled':BOOLEAN},('enabled',)),signature=True)
    async def share_links_set(ctx,request,tx):
        await authorized(ctx,request,tx,'system.config')
        before=bool(tx.setting('share_links_enabled',False))
        after=request.arguments['enabled']
        tx.set_setting('share_links_enabled',after)
        event=Event(id=new_id('audit'),type='system.share_links_set',time=ctx.now,
            request_id=request.request_id,actor=ctx.principal.actor,subject=ctx.principal.subject,
            resources=(ResourceRef(id=ROOT_SPACE),),data={'enabled':after})
        await tx.append_audit(AuditEvent(event=event,
            authority=tuple(ResourceRef(id=cid) for cid in ctx.principal.certificates),
            before_digest=digest({'enabled':before}),after_digest=digest({'enabled':after}),
            previous_digest=None,entry_digest='',result='configured'))
        return HandlerOutput(data={'enabled':after})

    @op('system.maintenance', obj({'action':{'enum':list(MAINTENANCE_ACTIONS_V1)}},('action',)),
        effect='external', signature=True)
    @op('system.maintenance', obj({'action':{'enum':list(MAINTENANCE_ACTIONS)}},('action',)),
        effect='external', signature=True, version=2)
    async def maintenance(ctx, request, tx):
        await authorized(ctx, request, tx, 'system.maintenance')
        eid = event_id(request, ctx.principal.subject)
        job = EffectJob(id=new_id('job'), event_id=eid, kind='maintenance', dedupe_key='maintenance:'+eid,
            principal=ctx.principal, operation=request.operation,
            arguments={'contract_version':request.contract_version,'action':request.arguments['action'],'request_id':request.request_id},
            state='pending', attempts=0, next_attempt_at=ctx.now, lease_until=None)
        await tx.enqueue(job)
        return HandlerOutput(data={'job_id':job.id,'state':'pending'})
    finish()
