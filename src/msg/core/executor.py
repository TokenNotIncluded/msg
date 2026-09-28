"""The only network business execution pipeline."""
from __future__ import annotations
import logging
import time
from dataclasses import replace
from uuid import uuid4
from msg.core.codec import wire,digest
from msg.core.batch import BatchPolicy
from msg.core.events import event_id
from msg.core.execution_ports import PacketDecoder, ProjectionReader, EventProjector
from msg.core.errors import Failure,require
from msg.core.models import ExecutionContext,HandlerOutput,OperationResult,OperationError,Event,AccessRequirement
from msg.core.requests import SECRET_DELIVERY_MIN_VERSION,receipt_bytes

log=logging.getLogger(__name__)


class OperationExecutor:
    def __init__(self,registry,metadata,contents,authenticator,authorizer,clock,receipt_signer, *,
                 max_request_bytes=1048576, packet_decoder: PacketDecoder | None = None,
                 projection_reader: ProjectionReader | None = None,
                 event_projector: EventProjector | None = None):
        self.registry,self.metadata,self.contents=registry,metadata,contents
        self.authenticator,self.authorizer,self.clock=authenticator,authorizer,clock
        self.receipt_signer=receipt_signer
        self.response_hook=None
        self.recovery_drill_marker=None
        self.recovery_quarantined=False
        self.batch_policy=BatchPolicy(registry,packet_decoder,max_request_bytes)
        self.projection_reader=projection_reader
        self.event_projector=event_projector

    def recovery_drill_active(self):
        marker=self.recovery_drill_marker
        return self.recovery_quarantined or (marker is not None and (marker.exists() or marker.is_symlink()))

    async def execute(self,request, *, entry='network'):
        principal=None
        try:
            spec=self.registry.operation(request.operation,request.contract_version)
            min_version=SECRET_DELIVERY_MIN_VERSION.get(spec.name)
            if spec.name=='identity.temporary' and spec.version<3:
                raise Failure('temporary_dual_keys_required',
                              details={'contract_version':3})
            require(min_version is None or spec.version>=min_version,
                    'credential_delivery_upgrade_required',
                    details={'contract_version':min_version} if min_version is not None else None)
            require(entry in spec.entries,'entry_not_allowed')
            self.registry.validate(spec.input_schema,request.arguments)
            if self.recovery_drill_active():
                require(spec.effect=='read','writes_paused')
                raise Failure('recovery_quarantined')
            if spec.name=='batch.independent':
                return await self._independent(request,spec,entry)
            async with self.metadata.transaction(write=spec.effect!='read') as session:
                principal=await self.authenticator.authenticate(request,session,entry=entry)
                if spec.name=='batch.atomic':
                    # Validate the child set before an old cached parent result
                    # can bypass the handler's secret-delivery exclusion.
                    self.batch_policy.packets(request,principal.subject)
                context=ExecutionContext(request_id=request.request_id,principal=principal,entry=entry,
                                         now=self.clock(),deadline_monotonic=time.monotonic()+30)
                checks=await spec.requirements(request,session)
                await self.authorizer.require(context,request,checks,session)
                previous=None
                if spec.effect!='read':
                    previous=await session.request_result(principal.subject,request.request_id,request.payload_digest)
                if previous is not None:
                    # Idempotency is not a cached authorization decision. Results
                    # can contain references to content whose access changed.
                    visible=tuple(AccessRequirement(resource_id=ref.id,operation=f'{spec.name}@{spec.version}',check='read')
                                  for ref in previous.resources)
                    await self.authorizer.require(context,request,visible,session)
                    result=replace(previous,replayed=True)
                else:
                    capacity_writes={'identity.register','identity.temporary','identity.custodial_create','content.topic_create',
                        'content.post_create','content.post_edit','content.file_put','content.attach',
                        'content.template_put','discussion.reply','discussion.quote','discussion.repost',
                        'transfer.part_put','git.create','git.push','git.receive','hosting.deploy','hosting.preview','keystore.put','achievement.start'}
                    if spec.name in capacity_writes:
                        require(session.setting('runtime_config',{}).get('accept_writes',True),'writes_paused')
                    for rid,generation in request.expected_generations:
                        current=await session.resource(rid)
                        if rid not in {check.resource_id for check in checks}:
                            check='manage' if current.owner==principal.subject else 'read'
                            await self.authorizer.require(context,request,(AccessRequirement(resource_id=rid,
                                operation=f'{spec.name}@{spec.version}',check=check),),session)
                        require(current.generation==generation,'generation_conflict',
                                details={'id':rid,'generation':current.generation,'revision':current.revision})
                    audited=(spec.name in {'content.chmod','content.chgrp','content.chown','content.purge','content.move',
                        'identity.key_add','identity.key_revoke','identity.recover','identity.delegate','identity.delegation_revoke',
                        'identity.ssh_key_add','identity.ssh_key_revoke','identity.ssh_certificates','cert.publish','cert.request'}
                        or (spec.name.startswith('group.') and spec.effect!='read'))
                    before=[]
                    if audited:
                        ids={c.resource_id for c in checks}|{principal.subject}
                        for resource_id in sorted(ids):
                            try:before.append(wire(await session.resource(resource_id)))
                            except Failure as exc:
                                if exc.code not in {'not_found'}:raise
                    output=await spec.handler(context,request,session)
                    if request.return_fields:
                        require(output.resources and self.projection_reader is not None,'projection_unavailable')
                        projections=[await self.projection_reader(context,request,session,ref)
                                     for ref in output.resources]
                        output=replace(output,data={**(output.data or {}),'projection':projections})
                    if audited:
                        from msg.core.models import AuditEvent,ResourceRef
                        audit_event=Event(id='a_'+uuid4().hex,type=spec.name,time=self.clock(),request_id=request.request_id,
                            actor=principal.actor,subject=principal.subject,resources=output.resources,
                            data={'request_digest':request.payload_digest})
                        await session.append_audit(AuditEvent(event=audit_event,
                            authority=tuple(ResourceRef(id=c) for c in principal.certificates),before_digest=digest(before),
                            after_digest=digest(output),previous_digest=None,entry_digest='',result='committed'))
                    require(isinstance(output,HandlerOutput),'invalid_handler_output')
                    self.registry.validate(spec.output_schema,wire(output))
                    result=OperationResult(request_id=request.request_id,operation=spec.name,
                        status='accepted' if spec.effect=='external' else 'ok',actor=principal.actor,subject=principal.subject,
                        resources=output.resources,committed_at=self.clock() if spec.effect!='read' else None,
                        data=output.data,output=output.output)
                    if spec.effect!='read':
                        event=Event(id=event_id(request,principal.subject),type=spec.name,time=result.committed_at,request_id=request.request_id,
                                    actor=principal.actor,subject=principal.subject,resources=output.resources,
                                    data={'operation':spec.name})
                        await session.append_event(event)
                        if self.event_projector is not None:
                            await self.event_projector(session,event)
                        result=replace(result,receipt=self.receipt_signer.sign(receipt_bytes(result),purpose='receipt'))
                        await session.save_result(principal.subject,request.payload_digest,result)
            # Only after the enclosing transaction commits may success reach the adapter.
            result=replace(result,prefer_cli=request.source!='msg',
                           cli_url='/AGENTS.md' if request.source!='msg' else None)
            if self.response_hook is not None:
                result=await self.response_hook(request,result)
            return result
        except Failure as exc:
            return OperationResult(request_id=request.request_id,operation=request.operation,status='error',
                actor=principal.actor if principal else None,subject=principal.subject if principal else None,
                error=OperationError(code=exc.code,retryable=exc.retryable,field_path=exc.field),
                data=exc.details)
        except Exception:
            # Neither exception text/tracebacks nor caller-selected IDs are safe
            # log fields. The stable event remains countable without secrets.
            log.error('operation_failed')
            return OperationResult(request_id=request.request_id,operation=request.operation,status='error',
                actor=principal.actor if principal else None,subject=principal.subject if principal else None,
                error=OperationError(code='internal_error',retryable=False))


    async def _independent(self,request,spec,entry):
        """Reserve an idempotency key, commit children separately, then the result.

        Interrupted batches are resumed by resending the original signed children.
        No child is reported rolled back merely because its sibling failed.
        """
        async with self.metadata.transaction(write=True) as tx:
            principal=await self.authenticator.authenticate(request,tx,entry=entry)
            await self.authorizer._ceiling(principal,f'{spec.name}@{spec.version}',principal.subject,tx)
            children=self.batch_policy.packets(request,principal.subject,bounded=False)
            existing=tx.one('SELECT digest FROM batches WHERE subject=? AND request_id=?',(principal.subject,request.request_id))
            require(existing is None or existing[0]==request.payload_digest,'idempotency_conflict')
            previous=await tx.request_result(principal.subject,request.request_id,request.payload_digest)
            if previous is not None:
                context=ExecutionContext(request_id=request.request_id,principal=principal,entry=entry,now=self.clock(),deadline_monotonic=time.monotonic()+30)
                await self.authorizer.require(context,request,tuple(AccessRequirement(resource_id=ref.id,
                    operation=f'{spec.name}@{spec.version}',check='read') for ref in previous.resources),tx)
                return replace(previous,replayed=True,prefer_cli=request.source!='msg',cli_url='/AGENTS.md' if request.source!='msg' else None)
            tx.execute('INSERT OR IGNORE INTO batches VALUES (?,?,?)',(principal.subject,request.request_id,request.payload_digest),write=True)
        results=[];resources=[]
        for child in children:
            result=await self.execute(child,entry=entry)
            results.append(result_wire(result))
            resources.extend(result.resources)
        async with self.metadata.transaction(write=True) as tx:
            current=await self.authenticator.authenticate(request,tx,entry=entry)
            await self.authorizer._ceiling(current,f'{spec.name}@{spec.version}',current.subject,tx)
            previous=await tx.request_result(principal.subject,request.request_id,request.payload_digest)
            if previous is not None:
                context=ExecutionContext(request_id=request.request_id,principal=current,entry=entry,now=self.clock(),deadline_monotonic=time.monotonic()+30)
                await self.authorizer.require(context,request,tuple(AccessRequirement(resource_id=ref.id,
                    operation=f'{spec.name}@{spec.version}',check='read') for ref in previous.resources),tx)
                return replace(previous,replayed=True,prefer_cli=request.source!='msg',cli_url='/AGENTS.md' if request.source!='msg' else None)
            result=OperationResult(request_id=request.request_id,operation=spec.name,status='ok',actor=principal.actor,
                subject=principal.subject,resources=tuple(dict.fromkeys(resources)),data={'results':results,'atomic':False},committed_at=self.clock())
            result=replace(result,receipt=self.receipt_signer.sign(receipt_bytes(result),purpose='receipt'))
            await tx.append_event(Event(id=event_id(request,principal.subject),type=spec.name,time=result.committed_at,
                request_id=request.request_id,actor=principal.actor,subject=principal.subject,resources=result.resources,data={'operation':spec.name}))
            await tx.save_result(principal.subject,request.payload_digest,result)
        return replace(result,prefer_cli=request.source!='msg',cli_url='/AGENTS.md' if request.source!='msg' else None)


def result_wire(result):
    value=wire(result,compact=True)
    for name in ('replayed','prefer_cli'):
        if not value.get(name):
            value.pop(name,None)
    return value
