"""Owner-declared recovery metadata. No account recovery or decryption authority."""
from __future__ import annotations

from msg.core.codec import b64,canonical,decode,loads,unb64,wire
from msg.core.errors import Failure,require
from msg.core.models import HandlerOutput,ResourceRef,Event,AuditEvent,Signature
from msg.core.requests import signing_bytes
from msg.plugins.common import (registration,check_access,new_id,resolve,operation_id,
                                revise_resource,assert_generation)
from msg.plugins.schemas import IDENTIFIER,INTEGER,REF,STRING,obj
from msg.security.age_keys import encryption_key_id,public_from_recipient
from msg.security.vault import open_age_identity,rewrap_owned_age_ciphertext
from msg.core.codec import digest
from msg.security.crypto import verify


def _current_policy(tx,subject):
    row=tx.one('SELECT body FROM recovery_policies WHERE subject=? ORDER BY version DESC LIMIT 1',
               (subject,))
    return dict(loads(row[0])) if row else None


def _owner(app,ctx,request,tx):
    from msg.plugins.identity import controlled_owner
    return controlled_owner(app,ctx,request,tx)


def install(app):
    op,finish=registration(app,'recovery',('identity',))

    @op('identity.custodial_upgrade_inventory',obj({'challenge_id':IDENTIFIER},
        ('challenge_id',)),effect='read')
    async def custodial_upgrade_inventory(ctx,request,tx):
        subject=await _owner(app,ctx,request,tx)
        require(subject.kind=='custodial' and ctx.principal.method=='token',
                'custodial_token_required')
        row=tx.one('''SELECT credential_id,status,challenge,body FROM custodial_upgrades
            WHERE id=? AND subject=?''',(request.arguments['challenge_id'],subject.resource_id))
        require(row is not None and row[0]==ctx.principal.credential_id and
                row[1] in {'pending','pending_rewrap'},'custodial_upgrade_not_pending')
        from msg.plugins.identity import custodial_age_inventory
        details=loads(row[3])
        frozen=details.get('age_inventory')
        require(frozen is not None,'custodial_inventory_required')
        current=await custodial_age_inventory(tx,subject.resource_id)
        mappings=details.get('rewrap_mappings',{})
        acks=details.get('rewrap_acks',{})
        expected=frozen+[mapping['new'] for mapping in mappings.values()]
        drift=current!=sorted(expected,key=lambda item:(item['id'],item['revision']))
        return HandlerOutput(data={'challenge_id':request.arguments['challenge_id'],
            'status':row[1],'recipient':loads(row[2])['encryption_recipient'],
            'frozen':frozen,'frozen_digest':digest(frozen),
            'mappings':mappings,'acks':acks,'inventory_changed':drift,
            'historical_revisions_require_migration':bool(frozen),
            'finalize_ready':False,'vault_remains_active':True})

    @op('identity.custodial_rewrap_entry',obj({
        'challenge_id':IDENTIFIER,'ciphertext_ref':REF,
        'old_encryption_key_id':IDENTIFIER,'new_recipient':STRING},
        ('challenge_id','ciphertext_ref','old_encryption_key_id','new_recipient')))
    async def custodial_rewrap_entry(ctx,request,tx):
        """Convert exactly one currently owned age revision during a pending exit."""
        subject=await _owner(app,ctx,request,tx)
        require(subject.kind=='custodial' and ctx.principal.method=='token',
                'custodial_token_required')
        args=request.arguments
        row=tx.one('''SELECT credential_id,status,challenge,body FROM custodial_upgrades
            WHERE id=? AND subject=?''',(args['challenge_id'],subject.resource_id))
        require(row is not None and row[0]==ctx.principal.credential_id and
                row[1]=='pending_rewrap','custodial_upgrade_not_pending_rewrap')
        challenge=loads(row[2])
        details=loads(row[3])
        frozen=details.get('age_inventory')
        require(frozen is not None,'custodial_inventory_required')
        from msg.plugins.identity import custodial_age_inventory
        expected=frozen+[mapping['new'] for mapping in
                         details.get('rewrap_mappings',{}).values()]
        current=await custodial_age_inventory(tx,subject.resource_id)
        require(current==sorted(expected,key=lambda item:(item['id'],item['revision'])),
                'custodial_inventory_changed')
        require(challenge['encryption_recipient']==args['new_recipient'] and
                details['old_encryption_key_id']==args['old_encryption_key_id'],
                'custodial_rewrap_key_mismatch')
        primary=tx.one('''SELECT key_id FROM encryption_subkeys WHERE subject=?
            AND is_primary=1''',(subject.resource_id,))
        vault=tx.one('''SELECT encryption_key_id,status FROM custodial_vault WHERE subject=?''',
                     (subject.resource_id,))
        require(primary is not None and vault is not None and vault[1]=='active' and
                primary[0]==vault[0]==args['old_encryption_key_id'],
                'custodial_rewrap_unknown_old_key')
        ref=decode(ResourceRef,args['ciphertext_ref'])
        require(ref.revision is not None,'custodial_rewrap_revision_required')
        resource=await tx.resource(ref.id)
        folder=tx.one("SELECT id FROM resources WHERE parent=? AND name='keystore'",
                      (subject.resource_id,))
        require(folder is not None and resource.parent==folder[0] and
                resource.type=='keystore' and resource.owner==subject.resource_id and
                resource.state=='active','custodial_rewrap_not_owned')
        await assert_generation(request,resource)
        require(resource.revision==ref.revision,'revision_conflict')
        await check_access(app,ctx,request,tx,resource.id,'read')
        await check_access(app,ctx,request,tx,resource.id,'write')
        revision=await tx.revision(ref)
        frozen_entry={'id':resource.id,'revision':revision.id,
                      'ciphertext_digest':revision.content.digest}
        require(frozen_entry in frozen,'custodial_rewrap_not_in_frozen_inventory')
        require(revision.id not in details.get('rewrap_mappings',{}),
                'custodial_rewrap_already_mapped')
        require(tx.setting('keystore_format:'+revision.id)=='age' and
                revision.content.size<=1048576,'custodial_rewrap_age_required')
        old_ciphertext=await app.contents.read_bytes(revision.content,limit=1048576)
        new_ciphertext=rewrap_owned_age_ciphertext(
            open_age_identity(app,tx,subject.resource_id),args['new_recipient'],old_ciphertext)
        updated=await revise_resource(app,ctx,request,tx,resource,new_ciphertext,
                                      'application/octet-stream')
        tx.set_setting('keystore_format:'+updated.revision,'age')
        before={'resource_id':resource.id,'revision':revision.id,
                'ciphertext_digest':revision.content.digest,'key_id':args['old_encryption_key_id']}
        after={'resource_id':updated.id,'revision':updated.revision,
               'ciphertext_digest':digest(new_ciphertext),
               'recipient':args['new_recipient']}
        mapping={'old':frozen_entry,'new':{'id':updated.id,
            'revision':updated.revision,'ciphertext_digest':digest(new_ciphertext)},
            'recipient':args['new_recipient'],'created_at':wire(ctx.now)}
        details.setdefault('rewrap_mappings',{})[revision.id]=mapping
        tx.execute('UPDATE custodial_upgrades SET body=? WHERE id=?',
                   (canonical(details).decode(),args['challenge_id']),write=True)
        event=Event(id=new_id('audit'),type='identity.custodial_rewrap_entry',time=ctx.now,
            request_id=request.request_id,actor=ctx.principal.actor,subject=subject.resource_id,
            resources=(ref,ResourceRef(id=updated.id,revision=updated.revision)),data={
                'challenge_id':args['challenge_id'],'old':before,'new':after,
                'plaintext_exposed':False})
        await tx.append_audit(AuditEvent(event=event,
            authority=(ResourceRef(id=subject.resource_id),),
            before_digest=digest(before),after_digest=digest(after),
            previous_digest=None,entry_digest='',result='rewrapped'))
        return HandlerOutput(resources=(ResourceRef(id=updated.id,revision=updated.revision),),
            data={'id':updated.id,'revision':updated.revision,'generation':updated.generation,
                  'old_revision':revision.id,'new_recipient':args['new_recipient'],
                  'ciphertext_digest':digest(new_ciphertext),
                  'mapping':mapping,
                  'client_decryption_verified':False,'upgrade_status':'pending_rewrap'})

    @op('identity.custodial_rewrap_ack',obj({
        'challenge_id':IDENTIFIER,'old_revision':IDENTIFIER,
        'new_revision':IDENTIFIER,'ciphertext_digest':IDENTIFIER,
        'plaintext_digest':IDENTIFIER,'decryption_ack':{'type':'object'}},
        ('challenge_id','old_revision','new_revision','ciphertext_digest',
         'plaintext_digest','decryption_ack')))
    async def custodial_rewrap_ack(ctx,request,tx):
        """Bind a new-key signature to one client-decrypted immutable mapping."""
        subject=await _owner(app,ctx,request,tx)
        require(subject.kind=='custodial' and ctx.principal.method=='token',
                'custodial_token_required')
        args=request.arguments
        row=tx.one('''SELECT credential_id,status,challenge,body FROM custodial_upgrades
            WHERE id=? AND subject=?''',(args['challenge_id'],subject.resource_id))
        require(row is not None and row[0]==ctx.principal.credential_id and
                row[1]=='pending_rewrap','custodial_upgrade_not_pending_rewrap')
        challenge=loads(row[2])
        details=loads(row[3])
        from msg.plugins.identity import custodial_age_inventory
        frozen=details.get('age_inventory')
        require(frozen is not None,'custodial_inventory_required')
        expected=frozen+[item['new'] for item in details.get('rewrap_mappings',{}).values()]
        current=await custodial_age_inventory(tx,subject.resource_id)
        require(current==sorted(expected,key=lambda item:(item['id'],item['revision'])),
                'custodial_inventory_changed')
        mapping=details.get('rewrap_mappings',{}).get(args['old_revision'])
        require(mapping is not None and mapping['new']['revision']==args['new_revision'] and
                mapping['new']['ciphertext_digest']==args['ciphertext_digest'],
                'custodial_rewrap_mapping_mismatch')
        new=await tx.revision(ResourceRef(id=mapping['new']['id'],
                                        revision=args['new_revision']))
        require(new.content.digest==args['ciphertext_digest'],
                'custodial_rewrap_mapping_mismatch')
        require(args['plaintext_digest'].startswith('sha256:') and
                len(args['plaintext_digest'])==71 and
                all(char in '0123456789abcdef' for char in args['plaintext_digest'][7:]),
                'invalid_plaintext_digest')
        signed={'subject_id':subject.resource_id,'challenge_id':args['challenge_id'],
            'old':mapping['old'],'new':mapping['new'],
            'plaintext_digest':args['plaintext_digest'],
            'request_id':request.request_id}
        verify(unb64(challenge['public_key'],limit=32),canonical(signed),
               decode(Signature,args['decryption_ack']),purpose='custodial-rewrap-ack')
        require(args['old_revision'] not in details.get('rewrap_acks',{}),
                'custodial_rewrap_already_acked')
        ack={'old_revision':args['old_revision'],'new_revision':args['new_revision'],
            'ciphertext_digest':args['ciphertext_digest'],
            'plaintext_digest':args['plaintext_digest'],
            'signature':args['decryption_ack'],'request_id':request.request_id,
            'verified_at':wire(ctx.now)}
        details.setdefault('rewrap_acks',{})[args['old_revision']]=ack
        tx.execute('UPDATE custodial_upgrades SET body=? WHERE id=?',
                   (canonical(details).decode(),args['challenge_id']),write=True)
        event=Event(id=new_id('audit'),type='identity.custodial_rewrap_ack',time=ctx.now,
            request_id=request.request_id,actor=ctx.principal.actor,
            subject=subject.resource_id,resources=(ResourceRef(id=new.resource_id,
                revision=new.id),),data={'challenge_id':args['challenge_id'],
                'old_revision':args['old_revision'],'new_revision':args['new_revision'],
                'ciphertext_digest':args['ciphertext_digest'],
                'plaintext_digest':args['plaintext_digest'],
                'signature':args['decryption_ack'],'vault_remains_active':True})
        await tx.append_audit(AuditEvent(event=event,
            authority=(ResourceRef(id=subject.resource_id),),
            before_digest=digest(mapping),after_digest=digest(ack),
            previous_digest=None,entry_digest='',result='client_acked'))
        return HandlerOutput(data={'challenge_id':args['challenge_id'],
            'old_revision':args['old_revision'],'new_revision':args['new_revision'],
            'client_decryption_acked':True,'vault_remains_active':True,
            'finalize_ready':False})

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
