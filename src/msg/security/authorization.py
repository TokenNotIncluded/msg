"""Current owner/group/mode/ShareGrant/credential/certificate authorization path."""
from __future__ import annotations
from msg.constants import ROOT_SUBJECT,TOOLS_SPACE
from msg.core.errors import Failure,require
from msg.core.codec import parse_time
from msg.core.models import ResourceRef
from msg.security.policy import allows,grant_covers,scope_contains,CERTGATE

_WRITE_CHECKS={'write','create','remove','chmod','chgrp','chown','manage','certgate','purge','tool_use'}
_OVERRIDE={'read':'resource.read_override','list':'resource.read_override','traverse':'resource.read_override',
           'write':'resource.write_override','chmod':'resource.chmod_override','chgrp':'resource.chgrp_override',
           'chown':'resource.chown','purge':'resource.purge','tool_use':'tool.use'}


class AuthorizationService:
    def __init__(self,registry,certificates):
        self.registry,self.certificates=registry,certificates

    async def grants(self,principal,session):
        grants=[]
        for cid in principal.certificates:
            certificate=await self.certificates.validate(cid,session)
            require(certificate.key_id==principal.credential_id and certificate.subject_id==principal.actor,
                    'certificate_subject_mismatch')
            grants.extend(certificate.grants)
        return tuple(grants)

    async def has(self,principal,capability,operation,resource,session):
        if principal.method=='local' and principal.subject==ROOT_SUBJECT:
            return True
        if not any([await grant_covers(g,capability,operation,resource,session) for g in principal.ceiling]):
            return False
        return any([await grant_covers(g,capability,operation,resource,session)
                    for g in await self.grants(principal,session)])

    async def _ceiling(self,principal,operation,resource,session):
        if principal.method=='anonymous':
            return
        allowed=any([operation in g.operations and await scope_contains(g.scope,resource,session)
                     for g in principal.ceiling])
        if not allowed and operation in {'discovery.get@1','discovery.raw@1','job.get@1',
                'transfer.open@1','transfer.part_get@1','transfer.status@1','transfer.seal@1','transfer.cancel@1'}:
            output=session.setting('tool_output:'+resource)
            if output and output['subject']==principal.subject:
                allowed=await self.has(principal,'tool.use',operation,output['tool_id'],session)
        require(allowed,'credential_ceiling')
        if principal.actor!=principal.subject:
            delegated=[]
            for cid in principal.certificates:
                cert=await self.certificates.validate(cid,session)
                if cert.kind=='delegation':
                    for source in cert.authority_sources:
                        fact=await self.certificates.validate_authority(source,cert,session)
                        if fact['grantor']==principal.subject:
                            delegated.extend(cert.grants)
            require(any([operation in g.operations and await scope_contains(g.scope,resource,session)
                         for g in delegated]),'delegation_scope')

    async def ordinary(self,principal,operation,resource,session):
        if principal.method in {'anonymous','local'}:
            return True
        from msg.security.capabilities import BASE_FAMILIES
        return any([g.capability in BASE_FAMILIES and operation in g.operations and
                    await scope_contains(g.scope,resource,session) for g in principal.ceiling])

    def shared_read(self,principal,resource,chain,now,session):
        """A direct, live source for exactly one resource's read check."""
        if principal.subject is None or principal.actor != principal.subject:
            return False
        if any(item.id in {'r_agents','r_rules','t_last_will'} or
               item.type in {'tool','csr','certificate','credential','legacy_directive'}
               for item in chain):
            return False
        if any(parent.type=='user' and child.name in {'SOUL.md','AGENTS.md','todos'}
               for parent,child in zip(chain,chain[1:])):
            return False
        row=session.one('''SELECT grantor,expires_at FROM share_grants
            WHERE resource_id=? AND grantee=? AND revoked_at IS NULL''',
                        (resource.id,principal.subject))
        return bool(row and row[0]==resource.owner and parse_time(row[1])>now
                    and resource.state=='active')

    async def share_link_read_allowed(self,grantor,credential_id,resource,chain,now,session):
        """Recheck one bearer source without creating a principal or granting traversal.

        The original signed owner's credential is a revocation boundary. A link
        cannot borrow that principal for other reads or outlive its current scope.
        """
        if resource.owner!=grantor or resource.state!='active' or any(
                item.state!='active' for item in chain):
            return False
        if self.registry.resource_type(resource.type,resource.type_version).container:
            return False
        if any(item.id in {'r_agents','r_rules','t_last_will'} or
               item.type in {'tool','csr','certificate','credential','legacy_directive',
                             'dm_conversation'} for item in chain):
            return False
        if any(parent.type=='user' and child.name in {'SOUL.md','AGENTS.md','todos'}
               for parent,child in zip(chain,chain[1:])):
            return False
        if any(session.setting('hosting_preview:'+item.id) or
               session.setting('hosting_preview_file:'+item.id) for item in chain):
            return False
        if session.one('''SELECT 1 FROM dm_conversations WHERE resource_id IN ('''+
                       ','.join('?' for _ in chain)+') LIMIT 1',
                       tuple(item.id for item in chain)) is not None:
            return False
        memberships={m.organization_id for m in await session.memberships(grantor)}
        if not allows(resource,grantor,memberships,'read') or any(
                not allows(ancestor,grantor,memberships,'traverse') for ancestor in chain[:-1]):
            return False
        try:
            credential=await session.credential(credential_id)
        except Failure as exc:
            if exc.code=='credential_not_found':
                return False
            raise
        if (credential.subject_id!=grantor or credential.kind!='signing_key' or
                credential.revoked_at is not None or credential.not_before>now or
                (credential.expires_at is not None and credential.expires_at<=now)):
            return False
        return any([('sharing.link_create@1' in grant.operations and
                     await scope_contains(grant.scope,resource.id,session))
                    for grant in credential.ceiling])

    async def require_base(self,principal,operation,resource,session):
        await self._ceiling(principal,operation,resource,session)
        require(await self.ordinary(principal,operation,resource,session),'credential_ceiling')

    async def require(self,context,request,checks,session):
        principal=context.principal
        if principal.subject==ROOT_SUBJECT or principal.actor==ROOT_SUBJECT:
            require(context.entry=='local_admin' and principal.method=='local','local_only')
        memberships={m.organization_id for m in await session.memberships(principal.subject)} if principal.subject else set()
        for check in checks:
            resource=await session.resource(check.resource_id)
            operation=check.operation
            await self._ceiling(principal,operation,resource.id,session)
            chain=(*await session.ancestors(resource.id),resource)
            preview=next((marker for item in chain
                for marker in (session.setting('hosting_preview:'+item.id),
                               session.setting('hosting_preview_file:'+item.id)) if marker),None)
            if preview is not None:
                require(principal.subject==preview['owner'] or
                        (principal.subject==ROOT_SUBJECT and context.entry=='local_admin'
                         and principal.method=='local'),'permission_denied')
            if check.check in _WRITE_CHECKS and any(
                    item.id in {'r_agents','r_rules'} for item in chain):
                require(False,'system_managed_resource')
            if check.check in _WRITE_CHECKS and any(item.id=='t_last_will' for item in chain):
                require(operation in {'identity.legacy_put@1','identity.legacy_archive@1'} and
                        principal.subject==resource.owner,'legacy_directive_only')
            if check.check in _WRITE_CHECKS and any(
                    parent.type=='user' and child.name in {'SOUL.md','AGENTS.md','notes','todos'}
                    for parent,child in zip(chain,chain[1:])):
                sharing_notes=(operation in {'sharing.grant@1','sharing.revoke@1',
                                             'sharing.link_create@1','sharing.link_revoke@1'} and
                    all(child.name not in {'SOUL.md','AGENTS.md','todos'}
                        for parent,child in zip(chain,chain[1:]) if parent.type=='user'))
                require((sharing_notes or operation in {'identity.personal_put@1','identity.note_put@1',
                                      'identity.soul_visibility@1','identity.note_archive@1',
                                      'identity.note_restore@1','identity.todo_put@1',
                                      'identity.todo_archive@1','identity.todo_restore@1'}) and
                        principal.subject==resource.owner,'personal_managed_resource')
            if check.check in _WRITE_CHECKS and principal.subject is not None:
                for ancestor in reversed(chain):
                    if ancestor.type!='topic':
                        continue
                    ban=session.one("SELECT expires_at FROM topic_bans WHERE topic=? AND subject=? AND status='active'",
                                    (ancestor.id,principal.subject))
                    if ban is not None and (ban[0] is None or parse_time(ban[0])>context.now):
                        require(False,'topic_banned')
                    break
            direct=None
            for ancestor in chain:
                direct=session.one('SELECT participant_a,participant_b,state,resource_id FROM dm_conversations WHERE resource_id=?',
                                   (ancestor.id,))
                if direct is not None:
                    break
            if direct is not None:
                # A direct conversation has two fixed participants. Neither file
                # mode nor an override certificate may reveal it to a third party.
                require(principal.subject in direct[:2], 'permission_denied')
                if check.check in {'read','list','traverse'}:
                    continue
                if (check.check=='create' and resource.id==direct[3] and
                        operation=='communication.dm_send@1' and direct[2]=='active'):
                    blocked=session.one('SELECT 1 FROM dm_blocks WHERE (blocker=? AND blocked=?) OR (blocker=? AND blocked=?)',
                                        (direct[0],direct[1],direct[1],direct[0]))
                    require(blocked is None,'dm_blocked')
                    continue
                if (check.check=='write' and resource.type=='post' and resource.owner==principal.subject and
                        operation=='content.post_edit@1'):
                    blocked=session.one('SELECT 1 FROM dm_blocks WHERE (blocker=? AND blocked=?) OR (blocker=? AND blocked=?)',
                                        (direct[0],direct[1],direct[1],direct[0]))
                    require(direct[2]=='active' and blocked is None,'dm_blocked')
                    continue
                require(False,'dm_controlled_resource')
            # tool.use only reveals its scoped tool and the ancestors needed to reach it.
            tool_access=resource.type=='tool' and await self.has(principal,'tool.use',operation,resource.id,session)
            if resource.type=='tool':
                require(tool_access,'tool_certificate_required')
            shared_read=(check.check=='read' and self.shared_read(
                principal,resource,chain,context.now,session))
            for ancestor in chain[:-1]:
                readable=allows(ancestor,principal.subject,memberships,'traverse')
                override=await self.has(principal,'resource.read_override',operation,ancestor.id,session)
                minimal_tool=tool_access and ancestor.id==TOOLS_SPACE
                require(readable or override or minimal_tool or shared_read,'permission_denied')
                require(ancestor.state=='active','ancestor_inactive')
            if check.check in _WRITE_CHECKS:
                require(principal.subject is not None,'authentication_required')
                for ancestor in chain:
                    if ancestor.mode&CERTGATE:
                        require(await self.has(principal,'resource.certified_write',operation,resource.id,session),
                                'certificate_gate')
            if resource.type=='tool':
                require(tool_access,'tool_certificate_required')
                require(check.check in {'read','tool_use'},'tool_read_only')
                continue
            if resource.type=='csr' and check.check=='read' and principal.subject!=resource.owner:
                csr=await session.csr(resource.id)
                require(csr.requested_issuer==principal.subject and await self.has(principal,'cert.issue',operation,resource.id,session),
                        'permission_denied')
                continue
            if check.check=='certgate':
                allowed=await self.has(principal,'resource.certified_write',operation,resource.id,session)
            elif check.check in {'chmod','chgrp'}:
                allowed=principal.subject==resource.owner
            elif check.check=='manage':
                allowed=principal.subject==resource.owner
            elif check.check=='create':
                allowed=allows(resource,principal.subject,memberships,'write') and allows(resource,principal.subject,memberships,'traverse')
            elif check.check=='remove':
                allowed=allows(resource,principal.subject,memberships,'write') and allows(resource,principal.subject,memberships,'traverse')
            elif check.check in {'chown','purge','tool_use'}:
                allowed=False
            else:
                allowed=allows(resource,principal.subject,memberships,check.check)
                if not allowed and check.check=='read':
                    allowed=shared_read
            ordinary=await self.ordinary(principal,operation,resource.id,session)
            if not ordinary and check.check=='read':
                output=session.setting('tool_output:'+resource.id)
                ordinary=bool(output and output['subject']==principal.subject and
                    await self.has(principal,'tool.use',operation,output['tool_id'],session))
            allowed=allowed and ordinary
            capability=_OVERRIDE.get(check.check)
            if not allowed and capability is not None:
                allowed=await self.has(principal,capability,operation,resource.id,session)
            require(allowed,'permission_denied')
        return tuple(ResourceRef(id=c) for c in principal.certificates)
