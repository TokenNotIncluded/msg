"""Local identity, automatic signing, resumable file exchange, and bounded retries."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import os
from pathlib import Path
import stat
from uuid import uuid4

import httpx

from msg.core.codec import b64, unb64, canonical, digest, loads, wire, decode
from msg.core.errors import Failure, require
from msg.core.models import ResourceRef, Certificate, Signature
from msg.core.requests import request_for
from msg.security.crypto import Ed25519Signer, subject_id, key_id
from msg.security.age_keys import (generate_age_key,recipient_from_identity,
    public_from_recipient,encryption_key_id)
from msg.security.vault import client_upgrade_proof
from msg.storage.git import durable_write


def hash_file(path):
    hashed = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as stream:
        while data := stream.read(65536):
            size += len(data)
            hashed.update(data)
    return size, 'sha256:'+hashed.hexdigest()


class ClientState:
    """Owned files only; a failed registration never loses its private key."""
    def __init__(self, directory=None, *, server=None):
        self.directory = Path(directory or Path.home()/'.config'/'msg')
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        require(not self.directory.is_symlink(),'unsafe_client_directory')
        require(self.directory.stat().st_uid==os.geteuid(),'client_directory_not_owned')
        self.directory.chmod(0o700)
        self.path = self.directory/'client.json'
        self.key_path = self.directory/'identity.key'
        self.age_key_path = self.directory/'encryption.agekey'
        self.pending_path = self.directory/'registration.json'
        self.data = loads(self.path.read_bytes()) if self.path.exists() else {'version':1}
        require(self.data.get('version')==1,'unknown_client_state_version')
        require(server is None or self.data.get('server',server)==server.rstrip('/'),'client_server_mismatch')
        self.server = self.data.get('server',server or 'https://msg.lmm.best').rstrip('/')
        self.data['server'] = self.server
        self.signer = None
        self.encryption_recipient = None
        if self.key_path.exists():
            require(self.key_path.is_file() and not self.key_path.is_symlink() and
                    self.key_path.stat().st_uid==os.geteuid() and self.key_path.stat().st_mode&0o077==0,
                    'unsafe_client_key_permissions')
            self.signer = Ed25519Signer.from_bytes(self.key_path.read_bytes())
        if self.age_key_path.exists():
            require(self.age_key_path.is_file() and not self.age_key_path.is_symlink() and
                    self.age_key_path.stat().st_uid==os.geteuid() and
                    self.age_key_path.stat().st_mode&0o077==0,'unsafe_client_key_permissions')
            self.encryption_recipient=recipient_from_identity(self.age_key_path.read_text().strip())
        self._save()

    @property
    def subject(self):
        return self.data.get('subject_id')

    @property
    def certificates(self):
        return tuple(self.data.get('certificates',()))

    @property
    def token(self):
        saved = self.data.get('token')
        return (saved['credential_id'],unb64(saved['value'])) if saved else None

    def _save(self):
        durable_write(self.path,canonical(self.data),mode=0o600)

    def save_signer(self, signer):
        require(not self.key_path.exists(),'identity_key_already_exists')
        durable_write(self.key_path,signer.private_bytes(),mode=0o600)
        self.signer = signer

    def ensure_encryption_key(self):
        if self.encryption_recipient is None:
            require(not self.age_key_path.exists(),'invalid_age_identity')
            identity,recipient=generate_age_key()
            durable_write(self.age_key_path,(identity+'\n').encode(),mode=0o600)
            self.encryption_recipient=recipient
        return self.encryption_recipient

    def accept_identity(self, result):
        require(result.status=='ok','identity_operation_failed')
        if result.data.get('encryption_recipient'):
            if result.operation=='identity.custodial_create':
                require(self.signer is None and self.encryption_recipient is None,
                        'custodial_client_key_conflict')
                self.data['custodial_encryption_recipient']=result.data['encryption_recipient']
            else:
                require(result.data['encryption_recipient']==self.encryption_recipient,
                        'encryption_recipient_mismatch')
                self.data.pop('custodial_encryption_recipient',None)
        self.data['subject_id'] = result.subject
        if result.data.get('certificate_id'):
            self.data['certificates'] = [result.data['certificate_id']]
        if result.data.get('token'):
            self.data['token'] = {'credential_id':result.data['credential_id'], 'value':result.data['token'],
                                  'expires_at':result.data['expires_at']}
        elif self.signer is not None:
            self.data.pop('token',None)
        self._save()


class MsgClient:
    def __init__(self, state, transport, *, clock=None, retries=2):
        self.state, self.transport = state, transport
        self.clock = clock or (lambda:datetime.now(UTC))
        self.retries = retries
        require(state.server==transport.server,'client_server_mismatch')

    def prepare(self, operation, arguments, *, expected=(), request_id=None, return_fields=(), subject=None,
                signer=None, certificates=None, anonymous=False, contract_version=1):
        require(not operation.startswith('root.'),'local_only')
        selected_signer = signer or self.state.signer
        selected_subject = subject or self.state.subject
        # An explicit temporary token remains authoritative until upgrade succeeds.
        token = self.state.token if signer is None else None
        if token:
            selected_signer = None
        return request_for(operation,arguments,self.state.server,
            subject=None if anonymous else selected_subject,signer=None if anonymous else selected_signer,
            token=None if anonymous else token, certificates=() if anonymous else
            (self.state.certificates if certificates is None else certificates),request_id=request_id,
            expires_at=self.clock()+timedelta(seconds=180),source='msg',expected=expected,
            return_fields=return_fields,contract_version=contract_version)

    async def send(self, packet):
        result = None
        for attempt in range(self.retries+1):
            try:
                result = await self.transport.call(packet)
                if result.status!='error' or not result.error.retryable or attempt==self.retries:
                    return result
            except (httpx.TimeoutException,httpx.NetworkError) as exc:
                if attempt==self.retries:
                    # Transport failure is not evidence of a rollback. The caller
                    # must retry this request ID, not invent another operation.
                    raise Failure('transport_uncertain',retryable=True,
                                  details={'request_id':packet.request_id}) from exc
            except Failure as exc:
                if not exc.retryable or attempt==self.retries:
                    raise
            await asyncio.sleep(min(2,0.2*(2**attempt)))
        return result

    async def call(self, operation, arguments=None, **kwargs):
        return await self.send(self.prepare(operation,arguments or {},**kwargs))

    @staticmethod
    def checked(result):
        if result.status=='error':
            raise Failure(result.error.code,result.error.field_path,retryable=result.error.retryable,
                          details=dict(result.data or {}))
        return result

    async def renew_certificate(self):
        require(self.state.signer is not None and self.state.token is None,'signing_identity_required')
        # Renewal must remain possible after expiry or a root trust rotation.
        result=await self.call('identity.certificate_renew',certificates=())
        if result.status=='ok':
            self.state.data['certificates']=[result.data['certificate_id']]
            self.state._save()
        return result

    async def register(self, handle):
        require(self.state.subject is None,'identity_already_configured')
        if self.state.pending_path.exists():
            pending=loads(self.state.pending_path.read_bytes())
            require(pending['handle']==handle,'registration_pending_for_other_handle')
        else:
            if self.state.signer is None:
                self.state.save_signer(Ed25519Signer.generate())
            self.state.ensure_encryption_key()
            pending={'handle':handle,'request_id':uuid4().hex}
            durable_write(self.state.pending_path,canonical(pending),mode=0o600)
        signer=self.state.signer
        recipient=self.state.ensure_encryption_key()
        result=await self.call('identity.register',{'handle':handle,'public_key':b64(signer.public_key),
            'encryption_recipient':recipient},contract_version=2,
            subject=subject_id(signer.public_key),signer=signer,certificates=(),request_id=pending['request_id'])
        if result.status=='ok':
            self.state.accept_identity(result)
            self.state.pending_path.unlink(missing_ok=True)
        return result

    async def temporary(self):
        require(self.state.subject is None,'identity_already_configured')
        pending=self.state.directory/'temporary.json'
        if pending.exists():
            data=loads(pending.read_bytes())
        else:
            data={'nonce':b64(os.urandom(32)),'request_id':uuid4().hex}
            durable_write(pending,canonical(data),mode=0o600)
        result=await self.call('identity.temporary',{'nonce':data['nonce']},request_id=data['request_id'],anonymous=True)
        if result.status=='ok':
            self.state.accept_identity(result)
            pending.unlink(missing_ok=True)
        return result

    async def custodial(self,handle):
        require(self.state.subject is None and self.state.signer is None and
                self.state.encryption_recipient is None,'identity_already_configured')
        pending=self.state.directory/'custodial-bootstrap.json'
        if pending.exists():
            data=loads(pending.read_bytes())
            require(data['handle']==handle,'registration_pending_for_other_handle')
        else:
            data={'handle':handle,'nonce':b64(os.urandom(32)),'request_id':uuid4().hex}
            durable_write(pending,canonical(data),mode=0o600)
        result=await self.call('identity.custodial_create',{'handle':handle,'nonce':data['nonce']},
                               request_id=data['request_id'],anonymous=True)
        if result.status=='ok':
            self.state.accept_identity(result)
            pending.unlink(missing_ok=True)
        return result

    async def rotate_token(self):
        require(self.state.token is not None,'token_required')
        pending=self.state.directory/'token-rotation.json'
        data=loads(pending.read_bytes()) if pending.exists() else {'nonce':b64(os.urandom(32)),'request_id':uuid4().hex}
        durable_write(pending,canonical(data),mode=0o600)
        result=await self.call('identity.token_rotate',{'nonce':data['nonce']},request_id=data['request_id'])
        if result.status=='ok':
            self.state.accept_identity(result)
            pending.unlink(missing_ok=True)
        return result

    async def upgrade(self,handle):
        require(self.state.token is not None,'temporary_identity_required')
        if self.state.signer is None:
            self.state.save_signer(Ed25519Signer.generate())
        signer=self.state.signer
        recipient=self.state.ensure_encryption_key()
        signed={'subject_id':self.state.subject,'handle':handle,'public_key':b64(signer.public_key),
                'encryption_recipient':recipient}
        result=await self.call('identity.upgrade',{'handle':handle,'public_key':b64(signer.public_key),
            'encryption_recipient':recipient,
            'possession_proof':wire(signer.sign(canonical(signed),purpose='upgrade'))},contract_version=2)
        if result.status=='ok':
            self.state.accept_identity(result)
        return result

    async def upgrade_custodial(self,handle, *, external_ciphertexts_migrated: bool):
        require(type(external_ciphertexts_migrated) is bool,'migration_statement_required')
        require(self.state.subject is not None and self.state.token is not None,
                'custodial_token_required')
        journal=self.state.directory/'custodial-upgrade.json'
        if self.state.signer is None:
            self.state.save_signer(Ed25519Signer.generate())
        recipient=self.state.ensure_encryption_key()
        public=b64(self.state.signer.public_key)
        if journal.exists():
            pending=loads(journal.read_bytes())
            require(pending['handle']==handle and pending['public_key']==public and
                    pending['encryption_recipient']==recipient,'custodial_upgrade_journal_mismatch')
        else:
            pending={'handle':handle,'public_key':public,'encryption_recipient':recipient,
                     'start_request_id':uuid4().hex,'finish_request_id':uuid4().hex,
                     'challenge':None}
            durable_write(journal,canonical(pending),mode=0o600)
        if pending['challenge'] is None:
            signed={'subject_id':self.state.subject,'handle':handle,'public_key':public,
                    'encryption_recipient':recipient,'request_id':pending['start_request_id']}
            proof=self.state.signer.sign(canonical(signed),purpose='custodial-upgrade-start')
            started=await self.call('identity.custodial_upgrade_start',{
                'handle':handle,'public_key':public,'encryption_recipient':recipient,
                'possession_proof':wire(proof)},request_id=pending['start_request_id'])
            if started.status!='ok':
                return started
            pending['challenge']=dict(started.data)
            durable_write(journal,canonical(pending),mode=0o600)
        challenge=pending['challenge']
        age_identity=self.state.age_key_path.read_text().strip()
        require(recipient_from_identity(age_identity)==recipient,'encryption_identity_mismatch')
        age_proof=client_upgrade_proof(age_identity,challenge)
        acknowledgement={'subject_id':self.state.subject,'challenge_id':challenge['challenge_id'],
            'age_proof':age_proof,'external_ciphertexts_migrated':external_ciphertexts_migrated,
            'request_id':pending['finish_request_id']}
        signed_ack=self.state.signer.sign(canonical(acknowledgement),
                                          purpose='custodial-upgrade-finish')
        async def recover_with_new_key():
            return await self.call('identity.custodial_upgrade_result',
                {'challenge_id':challenge['challenge_id']},signer=self.state.signer,
                subject=self.state.subject,certificates=())
        try:
            result=await self.call('identity.custodial_upgrade_finish',{
                'challenge_id':challenge['challenge_id'],'age_proof':age_proof,
                'external_ciphertexts_migrated':external_ciphertexts_migrated,
                'migration_ack':wire(signed_ack)},request_id=pending['finish_request_id'])
        except Failure as exc:
            if exc.code!='transport_uncertain':
                raise
            recovered=await recover_with_new_key()
            if recovered.status!='ok':
                raise
            result=recovered
        if result.status=='error' and result.error.code in {
                'credential_expired','credential_revoked','request_expired','idempotency_conflict'}:
            recovered=await recover_with_new_key()
            if recovered.status=='ok':
                result=recovered
        if result.status=='ok' and result.data['status']=='completed':
            self.state.accept_identity(result)
            journal.unlink(missing_ok=True)
        elif result.status=='ok' and result.data['status']=='pending_rewrap':
            # A verified challenge is consumed; keep the new private keys but
            # start a fresh proof after the client migrates its ciphertexts.
            pending['challenge']=None
            pending['start_request_id']=uuid4().hex
            pending['finish_request_id']=uuid4().hex
            durable_write(journal,canonical(pending),mode=0o600)
        return result

    async def rotate_encryption_key(self):
        require(self.state.subject is not None and self.state.signer is not None and
                self.state.token is None,'signing_identity_required')
        journal=self.state.directory/'encryption-rotation.json'
        pending_key=self.state.directory/'encryption.pending.agekey'
        if journal.exists():
            pending=loads(journal.read_bytes())
            require(pending_key.is_file() and not pending_key.is_symlink() and
                    pending_key.stat().st_uid==os.geteuid() and pending_key.stat().st_mode&0o077==0,
                    'unsafe_client_key_permissions')
            require(recipient_from_identity(pending_key.read_text().strip())==pending['recipient'],
                    'encryption_rotation_mismatch')
        else:
            require(self.state.encryption_recipient is not None,'encryption_key_not_found')
            identity,recipient=generate_age_key()
            durable_write(pending_key,(identity+'\n').encode(),mode=0o600)
            pending={'recipient':recipient,'request_id':uuid4().hex}
            durable_write(journal,canonical(pending),mode=0o600)
        result=await self.call('identity.encryption_key_rotate',
                               {'encryption_recipient':pending['recipient']},
                               request_id=pending['request_id'])
        if result.status=='ok':
            require(result.data['recipient']==pending['recipient'],
                    'encryption_rotation_mismatch')
            previous=result.data['previous_key_id']
            history=self.state.directory/('encryption-'+previous+'.agekey')
            if self.state.age_key_path.exists() and self.state.encryption_recipient!=pending['recipient']:
                require(not history.exists(),'encryption_history_conflict')
                os.replace(self.state.age_key_path,history)
            if not self.state.age_key_path.exists():
                os.replace(pending_key,self.state.age_key_path)
            self.state.encryption_recipient=pending['recipient']
            pending_key.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
        return result

    async def upload(self, path, *, transfer_id=None, part_bytes=65536, media_type='application/octet-stream',target=None):
        path=Path(path)
        size, hashed=await asyncio.to_thread(hash_file,path)
        journal=self.state.directory/('upload-'+digest((str(path.resolve()),hashed))[7:39]+'.json')
        saved=loads(journal.read_bytes()) if journal.exists() else None
        if transfer_id is None and saved:
            transfer_id=saved.get('transfer_id')
        if transfer_id is None:
            limits=await self.transport.discover()
            arguments={'direction':'upload','size':size,'digest':hashed,'media_type':media_type,
                'requested_part_bytes':part_bytes,'max_request_bytes':limits.max_request_bytes,
                'max_response_bytes':limits.max_response_bytes,'max_path_bytes':limits.max_path_bytes}
            if target:
                arguments['target']=wire(target if isinstance(target,ResourceRef) else ResourceRef(id=target))
            saved=saved or {'request_id':uuid4().hex,'size':size,'digest':hashed}
            durable_write(journal,canonical(saved),mode=0o600)
            result=self.checked(await self.call('transfer.open',arguments,request_id=saved['request_id']))
            transfer_id=result.data['transfer_id']
            part_bytes=result.data['part_bytes']
            saved.update(transfer_id=transfer_id,part_bytes=part_bytes)
            durable_write(journal,canonical(saved),mode=0o600)
        elif saved and saved.get('transfer_id')==transfer_id:
            part_bytes=min(part_bytes,saved['part_bytes'])
        with path.open('rb') as stream:
            cursor=None
            while True:
                args={'transfer_id':transfer_id,'limit':50}
                if cursor: args['cursor']=cursor
                status=self.checked(await self.call('transfer.status',args))
                require(status.data['size']==size and status.data['digest']==hashed,'transfer_source_mismatch')
                if status.data['state']=='sealed':
                    break
                require(status.data['state']=='open','transfer_not_open')
                for start,end in status.data['missing']:
                    stream.seek(start)
                    while start<end:
                        piece=stream.read(min(part_bytes,end-start))
                        require(bool(piece),'source_changed_during_upload')
                        self.checked(await self.call('transfer.part_put',{'transfer_id':transfer_id,
                            'offset':start,'data':b64(piece),'digest':digest(piece)}))
                        start+=len(piece)
                cursor=status.data.get('next_cursor')
                if cursor is None: break
        result=self.checked(await self.call('transfer.seal',{'transfer_id':transfer_id,'final_size':size,'final_digest':hashed}))
        journal.unlink(missing_ok=True)
        return result

    async def download(self, resource, path, *, transfer_id=None, part_bytes=65536):
        ref=resource if isinstance(resource,ResourceRef) else ResourceRef(id=resource)
        path=Path(path)
        journal=self.state.directory/('download-'+digest((wire(ref),str(path.resolve())))[7:39]+'.json')
        part=path.with_name(path.name+'.msg-part')
        saved=loads(journal.read_bytes()) if journal.exists() else None
        if transfer_id is None and saved:
            transfer_id=saved['transfer_id']
        if transfer_id is None:
            limits=await self.transport.discover()
            result=self.checked(await self.call('transfer.open',{'direction':'download','target':wire(ref),
                'requested_part_bytes':part_bytes,'max_request_bytes':limits.max_request_bytes,
                'max_response_bytes':limits.max_response_bytes,'max_path_bytes':limits.max_path_bytes}))
            saved={'transfer_id':result.data['transfer_id'],'size':result.data['size'],'digest':result.data['digest'],
                   'part_bytes':result.data['part_bytes'],'offset':0,'prefix_digest':digest(b'')}
            transfer_id=saved['transfer_id']
            durable_write(journal,canonical(saved),mode=0o600)
        require(saved is not None,'download_journal_required')
        require(not path.exists() and not part.is_symlink(),'download_target_exists')
        path.parent.mkdir(parents=True,exist_ok=True)
        offset=saved['offset']
        hasher=hashlib.sha256()
        if offset:
            require(part.is_file() and part.stat().st_size>=offset,'download_partial_missing')
            with part.open('rb') as source:
                remaining=offset
                while remaining:
                    piece=source.read(min(65536,remaining));require(bool(piece),'download_partial_missing')
                    hasher.update(piece);remaining-=len(piece)
            require('sha256:'+hasher.hexdigest()==saved['prefix_digest'],'download_partial_modified')
        mode='r+b' if part.exists() else 'w+b'
        with part.open(mode) as out:
            part.chmod(0o600);out.truncate(offset);out.seek(offset)
            while offset<saved['size']:
                amount=min(part_bytes,saved['part_bytes'],saved['size']-offset)
                result=self.checked(await self.call('transfer.part_get',{'transfer_id':transfer_id,'offset':offset,'length':amount}))
                piece=unb64(result.data['data'])
                require(len(piece)==amount and digest(piece)==result.data['chunk']['content']['digest'],'download_chunk_digest_mismatch')
                out.write(piece);out.flush();os.fsync(out.fileno())
                hasher.update(piece);offset+=len(piece)
                saved.update(offset=offset,prefix_digest='sha256:'+hasher.hexdigest())
                durable_write(journal,canonical(saved),mode=0o600)
        require('sha256:'+hasher.hexdigest()==saved['digest'],'download_digest_mismatch')
        # Link avoids overwriting a target created concurrently.
        os.link(part,path);part.unlink();journal.unlink(missing_ok=True)
        return {'path':str(path),'size':saved['size'],'digest':saved['digest'],'transfer_id':transfer_id}

    async def ack(self,ref):
        require(ref.revision is not None,'ack_revision_required')
        value=self.checked(await self.call('discovery.get',{'id':ref.id,'revision':ref.revision,
            'fields':['id','revision','digest']}))
        return await self.call('discussion.ack',{'target':wire(ref),'digest':value.data['digest']})

    async def contract_registry(self):
        from types import SimpleNamespace
        from msg.core.cursors import CursorCodec
        result=self.checked(await self.call('discovery.operations',{},anonymous=True))
        return RemoteRegistry(self,result.data)


class RemoteRegistry:
    """Read-only contract cache; never loads code or trusts server principals."""
    def __init__(self,client,catalog):
        from types import SimpleNamespace
        self.client=client
        self._catalog=dict(catalog)
        self._specs={}
        self._schemas={}
        self.directory=client.state.directory/'cache'/catalog['digest'].replace(':','-')
        self.directory.mkdir(parents=True,exist_ok=True)
        for item in catalog['operations']:
            key=(item['name'],item['version'])
            require(key not in self._specs,'duplicate_operation')
            require('network' in item['entries'] and not item['name'].startswith('root.'),'invalid_network_contract')
            self._specs[key]=SimpleNamespace(**{**item,'entries':frozenset(item['entries']),
                'input_schema':decode(ResourceRef,item['input_schema']),'output_schema':decode(ResourceRef,item['output_schema'])})

    def catalog(self):
        return self._catalog

    def operations(self,entry='network'):
        return tuple(self._specs[key] for key in sorted(self._specs) if entry in self._specs[key].entries)

    def operation(self,name,version=1):
        require((name,version) in self._specs,'unknown_operation')
        return self._specs[(name,version)]

    def schema(self,ref):
        require(ref.id in self._schemas,'schema_not_cached')
        return self._schemas[ref.id]

    async def load_schemas(self,specs):
        async def load(spec):
            path=self.directory/(digest((spec.name,spec.version))[7:39]+'.json')
            if path.is_file():
                value=loads(path.read_bytes())
            else:
                identity=spec.name if spec.version==1 else f'{spec.name}@{spec.version}'
                result=self.client.checked(await self.client.call('discovery.schema',{'operation':identity},anonymous=True))
                value=dict(result.data)
                durable_write(path,canonical(value),mode=0o600)
            require(value['operation']['name']==spec.name and value['operation']['version']==spec.version,'schema_contract_mismatch')
            self._schemas[spec.input_schema.id]=value['input']
            self._schemas[spec.output_schema.id]=value['output']
        await asyncio.gather(*(load(s) for s in specs if s.input_schema.id not in self._schemas))
