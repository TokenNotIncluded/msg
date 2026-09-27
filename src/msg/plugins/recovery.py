"""Owner-declared recovery metadata. No account recovery or decryption authority."""
from __future__ import annotations

from msg.core.codec import b64,canonical,decode,loads,wire
from msg.core.errors import Failure,require
from msg.core.models import HandlerOutput,ResourceRef
from msg.core.requests import signing_bytes
from msg.plugins.common import registration,check_access,new_id,resolve,operation_id
from msg.plugins.schemas import IDENTIFIER,INTEGER,REF,STRING,obj
from msg.security.age_keys import encryption_key_id,public_from_recipient


def _current_policy(tx,subject):
    row=tx.one('SELECT body FROM recovery_policies WHERE subject=? ORDER BY version DESC LIMIT 1',
               (subject,))
    return dict(loads(row[0])) if row else None


def _owner(app,ctx,request,tx):
    from msg.plugins.identity import controlled_owner
    return controlled_owner(app,ctx,request,tx)


def install(app):
    op,finish=registration(app,'recovery',('identity',))

    @op('identity.recovery_custodians',obj(),effect='read')
    async def custodians(ctx,request,tx):
        return HandlerOutput(data={'items':[wire(item) for item in app.settings.recovery_custodians]})

    @op('identity.recovery_policy_get',obj(),effect='read')
    async def policy_get(ctx,request,tx):
        subject=await _owner(app,ctx,request,tx)
        policy=_current_policy(tx,subject.resource_id)
        return HandlerOutput(data=policy or {'subject_id':subject.resource_id,'version':0,
            'encryption_key_id':None,'recipients':[],'opted_in':False,
            'recipient_claim':'owner_declared_unverified'})

    recipient_schema=obj({'recipient':STRING,'custodian_ref':IDENTIFIER},('recipient',))
    @op('identity.recovery_policy_set',obj({
        'expected_version':{'type':'integer','minimum':0},
        'encryption_key_id':IDENTIFIER,
        'recipients':{'type':'array','items':recipient_schema,'maxItems':8}},
        ('expected_version','encryption_key_id','recipients')),signature=True)
    async def policy_set(ctx,request,tx):
        subject=await _owner(app,ctx,request,tx)
        current=_current_policy(tx,subject.resource_id)
        version=current['version'] if current else 0
        args=request.arguments
        require(args['expected_version']==version,'recovery_policy_version_conflict')
        key=tx.one('SELECT key_id FROM encryption_subkeys WHERE subject=? AND is_primary=1',
                   (subject.resource_id,))
        require(key is not None and args['encryption_key_id']==key[0],
                'recovery_encryption_key_mismatch')
        known={item.id:item for item in app.settings.recovery_custodians}
        recipients=[]
        for entry in args['recipients']:
            recipient=entry['recipient']
            fingerprint=encryption_key_id(public_from_recipient(recipient))
            custodian_ref=entry.get('custodian_ref')
            if custodian_ref is not None:
                if custodian_ref in known:
                    require(known[custodian_ref].recipient==recipient,
                            'custodian_recipient_mismatch')
                else:
                    require(custodian_ref.startswith('u_'),'unknown_custodian_ref')
                    await tx.subject(custodian_ref)
            recipients.append({'recipient':recipient,'fingerprint':fingerprint,
                               'custodian_ref':custodian_ref})
        require(len({item['fingerprint'] for item in recipients})==len(recipients),
                'duplicate_recovery_recipient')
        policy={'subject_id':subject.resource_id,'version':version+1,
                'encryption_key_id':args['encryption_key_id'],'recipients':recipients,
                'opted_in':bool(recipients),'recipient_claim':'owner_declared_unverified',
                'created_at':wire(ctx.now),'signature_source':'self-custody',
                'request_signature':wire(request.proof.signature),
                'signed_envelope':b64(signing_bytes(request))}
        tx.execute('INSERT INTO recovery_policies VALUES (?,?,?,?,?)',
                   (subject.resource_id,version+1,args['encryption_key_id'],wire(ctx.now),
                    canonical(policy).decode()),write=True)
        return HandlerOutput(data=policy)

    envelope_schema=obj({
        'ciphertext_ref':REF,'encryption_key_id':IDENTIFIER,
        'policy_version':{'type':'integer','minimum':1},
        'recipient_fingerprints':{'type':'array','items':IDENTIFIER,'minItems':1,
                                  'maxItems':8,'uniqueItems':True},
        'custodian_refs':{'type':'array','items':IDENTIFIER,'maxItems':8,'uniqueItems':True},
        'purpose':{'const':'encryption-subkey-recovery'},'instructions_ref':REF},
        ('ciphertext_ref','encryption_key_id','policy_version','recipient_fingerprints','purpose'))

    @op('identity.recovery_envelope_register',envelope_schema,signature=True)
    async def envelope_register(ctx,request,tx):
        subject=await _owner(app,ctx,request,tx)
        args=request.arguments
        policy=_current_policy(tx,subject.resource_id)
        require(policy is not None and policy['opted_in'] and
                args['policy_version']==policy['version'],'recovery_policy_not_opted_in')
        primary=tx.one('SELECT key_id FROM encryption_subkeys WHERE subject=? AND is_primary=1',
                       (subject.resource_id,))
        require(primary is not None and args['encryption_key_id']==primary[0] and
                args['encryption_key_id']==policy['encryption_key_id'],
                'recovery_encryption_key_mismatch')
        offered=set(args['recipient_fingerprints'])
        allowed={entry['fingerprint']:entry for entry in policy['recipients']}
        require(offered<=allowed.keys(),'recovery_recipient_not_in_policy')
        expected_refs={entry['custodian_ref'] for fingerprint,entry in allowed.items()
                       if fingerprint in offered and entry['custodian_ref'] is not None}
        refs=set(args.get('custodian_refs',()))
        require(refs==expected_refs,'recovery_custodian_ref_mismatch')
        ref=decode(ResourceRef,args['ciphertext_ref'])
        require(ref.revision is not None,'recovery_revision_required')
        resource=await tx.resource(ref.id)
        folder=tx.one("SELECT id FROM resources WHERE parent=? AND name='keystore'",
                      (subject.resource_id,))
        require(folder is not None and resource.type=='keystore' and
                resource.owner==subject.resource_id and resource.parent==folder[0],
                'recovery_ciphertext_not_owned')
        await check_access(app,ctx,request,tx,ref.id,'read')
        await tx.revision(ref)
        require(resource.revision==ref.revision,'recovery_revision_not_current')
        require(tx.setting('keystore_format:'+ref.revision)=='age','recovery_age_ciphertext_required')
        instructions=None
        if args.get('instructions_ref'):
            instructions=decode(ResourceRef,args['instructions_ref'])
            instruction_resource=await tx.resource(instructions.id)
            require(instruction_resource.owner==subject.resource_id,
                    'recovery_instructions_not_owned')
            await check_access(app,ctx,request,tx,instructions.id,'read')
            if instructions.revision is not None:
                await tx.revision(instructions)
        record={'id':new_id('renv'),'owner_subject':subject.resource_id,
                'ciphertext_ref':wire(ref),'encryption_key_id':args['encryption_key_id'],
                'policy_version':policy['version'],'recipient_fingerprints':sorted(offered),
                'custodian_refs':sorted(refs),'purpose':args['purpose'],
                'instructions_ref':wire(instructions) if instructions else None,
                'created_at':wire(ctx.now),'recipient_claim':'owner_declared_unverified',
                'signature_source':'self-custody','request_signature':wire(request.proof.signature),
                'signed_envelope':b64(signing_bytes(request))}
        tx.execute('''INSERT INTO recovery_envelopes
            (id,owner,ciphertext_resource,ciphertext_revision,policy_version,encryption_key_id,
             purpose,created_at,body) VALUES (?,?,?,?,?,?,?,?,?)''',
            (record['id'],subject.resource_id,ref.id,ref.revision,policy['version'],
             args['encryption_key_id'],args['purpose'],record['created_at'],
             canonical(record).decode()),write=True)
        return HandlerOutput(resources=(ref,),data=record)

    @op('identity.recovery_envelope_get',obj({'id':IDENTIFIER},('id',)),effect='read')
    async def envelope_get(ctx,request,tx):
        subject=await _owner(app,ctx,request,tx)
        row=tx.one('SELECT owner,body FROM recovery_envelopes WHERE id=?',(request.arguments['id'],))
        require(row is not None and row[0]==subject.resource_id,'recovery_envelope_not_found')
        record=loads(row[1])
        ref=decode(ResourceRef,record['ciphertext_ref'])
        await check_access(app,ctx,request,tx,ref.id,'read')
        await tx.revision(ref)
        if record['instructions_ref'] is not None:
            instruction=decode(ResourceRef,record['instructions_ref'])
            await check_access(app,ctx,request,tx,instruction.id,'read')
            if instruction.revision is not None:
                await tx.revision(instruction)
        return HandlerOutput(data=record)

    @op('identity.recovery_envelope_list',obj(),effect='read')
    async def envelope_list(ctx,request,tx):
        subject=await _owner(app,ctx,request,tx)
        items=[]
        for (raw,) in tx.rows('SELECT body FROM recovery_envelopes WHERE owner=? ORDER BY created_at,id',
                              (subject.resource_id,)):
            record=loads(raw)
            ref=decode(ResourceRef,record['ciphertext_ref'])
            try:
                await check_access(app,ctx,request,tx,ref.id,'read')
                await tx.revision(ref)
                if record['instructions_ref'] is not None:
                    instruction=decode(ResourceRef,record['instructions_ref'])
                    await check_access(app,ctx,request,tx,instruction.id,'read')
                    if instruction.revision is not None:
                        await tx.revision(instruction)
            except Failure as exc:
                if exc.code in {'permission_denied','not_found','revision_not_found'}:
                    continue
                raise
            items.append(record)
        return HandlerOutput(data={'items':items})

    finish()
