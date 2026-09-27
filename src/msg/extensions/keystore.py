"""Opaque encrypted records reuse the same resource, revision and ACL machinery."""
from msg.core.codec import decode, unb64, wire, loads
from msg.core.errors import require
from msg.core.models import HandlerOutput, ResourceRef
from msg.plugins.common import check_access, create_resource, revise_resource, resolve, output_for, assert_generation
from msg.plugins.schemas import obj,STRING,IDENTIFIER,BYTES,REF


def register(app,op):
    @op('keystore.put',obj({'name':STRING,'format':{'enum':['msg-x25519-v1','age','openpgp']},
        'ciphertext':BYTES,'source':REF,'id':IDENTIFIER,'encryption_key_id':IDENTIFIER},('name','format')),signature=True)
    async def put(ctx,request,tx):
        a=request.arguments
        parent=tx.one("SELECT id FROM resources WHERE parent=? AND name='keystore'",(ctx.principal.subject,))
        require(parent is not None,'keystore_missing')
        await check_access(app,ctx,request,tx,parent[0],'create')
        require(('ciphertext' in a)!=('source' in a),'one_content_source_required')
        if 'ciphertext' in a:
            content=unb64(a['ciphertext'],limit=app.settings.server.limits.max_request_bytes)
            body=content
        else:
            ref=decode(ResourceRef,a['source'])
            require(ref.revision is not None,'source_revision_required')
            await check_access(app,ctx,request,tx,ref.id,'read')
            body=(await tx.revision(ref)).content
            # An envelope is inspected only up to the bounded validation window.
            # Larger pre-encrypted age/OpenPGP objects are validated by their header.
            content=b''.join([part async for part in app.contents.read(body,(0,min(body.size,65536)))])
        if a['format']=='msg-x25519-v1':
            from msg.security.sealed_box import validate_envelope
            if 'source' in a:
                require(body.size<=app.settings.server.limits.max_request_bytes,'ciphertext_envelope_too_large')
                content=await app.contents.read_bytes(body,limit=app.settings.server.limits.max_request_bytes)
            validate_envelope(content)
        elif a['format']=='age':
            active=tx.one('''SELECT u.body FROM custodial_upgrades u JOIN custodial_vault v
                ON v.subject=u.subject WHERE u.subject=? AND
                (u.status='pending_rewrap' OR (u.status='completed' AND v.status='decrypt_only'))''',
                (ctx.principal.subject,))
            if active:
                target=loads(active[0])['new_encryption_key_id']
                require(a.get('encryption_key_id')==target,'custodial_new_encryption_key_required')
            require(content.startswith((b'age-encryption.org/v1\n',b'-----BEGIN AGE ENCRYPTED FILE-----')), 'invalid_ciphertext_envelope')
        else:
            require(content.startswith(b'-----BEGIN PGP MESSAGE-----') or bool(content and content[0]&0x80),
                    'invalid_ciphertext_envelope')
        if 'id' in a:
            resource=await tx.resource(await resolve(tx,a['id']))
            require(resource.type=='keystore' and resource.parent==parent[0],'not_a_keystore_entry')
            await assert_generation(request,resource)
            await check_access(app,ctx,request,tx,resource.id,'write')
            resource=await revise_resource(app,ctx,request,tx,resource,body,'application/octet-stream')
        else:
            resource=await create_resource(app,ctx,request,tx,parent=parent[0],type='keystore',name=a['name'],
                body=body,media_type='application/octet-stream',mode=0o600)
        tx.set_setting('keystore_format:'+resource.revision,a['format'])
        if a['format']=='age' and a.get('encryption_key_id'):
            key=tx.one('SELECT subject,is_primary FROM encryption_subkeys WHERE key_id=?',
                       (a['encryption_key_id'],))
            require(key==(ctx.principal.subject,1),'keystore_encryption_key_mismatch')
            # Recipient metadata is an owner declaration; migration completion
            # still requires a fresh, locally decrypted inventory ACK.
            tx.set_setting('keystore_key:'+resource.revision,a['encryption_key_id'])
        return output_for(resource,format=a['format'])

    @op('keystore.get',obj({'id':IDENTIFIER,'revision':IDENTIFIER},('id',)),effect='read')
    async def get(ctx,request,tx):
        rid=await resolve(tx,request.arguments['id'])
        await check_access(app,ctx,request,tx,rid,'read')
        resource=await tx.resource(rid)
        require(resource.type=='keystore' and resource.state!='purged','not_a_keystore_entry')
        rev=await tx.revision(ResourceRef(id=rid,revision=request.arguments.get('revision')))
        ref=ResourceRef(id=rid,revision=rev.id)
        return HandlerOutput(data={'id':rid,'revision':rev.id,'format':tx.setting('keystore_format:'+rev.id),
            'size':rev.content.size,'digest':rev.content.digest,'raw_url':f'/_id/{rid}/revisions/{rev.id}/raw'},output=ref)

    @op('keystore.list',obj({'cursor':STRING,'limit':{'type':'integer','minimum':1,'maximum':200}}),effect='read')
    async def listing(ctx,request,tx):
        require(ctx.principal.subject is not None,'authentication_required')
        parent=tx.one("SELECT id FROM resources WHERE parent=? AND name='keystore'",(ctx.principal.subject,))
        require(parent is not None,'keystore_missing')
        await check_access(app,ctx,request,tx,parent[0],'list')
        cursor=request.arguments.get('cursor')
        position=app.cursors.decode(cursor,'keystore',parent[0]) if cursor else None
        page=await tx.children(parent[0],position,request.arguments.get('limit',50))
        items=[]
        for resource in page.items:
            if resource.type=='keystore' and resource.state=='active':
                await check_access(app,ctx,request,tx,resource.id,'read')
                items.append({'id':resource.id,'name':resource.name,'revision':resource.revision,'generation':resource.generation})
        return HandlerOutput(data={'items':items,'cursor':app.cursors.encode('keystore',parent[0],page.next_cursor) if page.next_cursor else None})
