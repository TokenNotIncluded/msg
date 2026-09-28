"""Local age backup of a self-custody encryption subkey.

The service receives only ciphertext. Decrypting an envelope does not log in to
an account or grant the custodian any application permission.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from msg.client_secrets import write_private
from msg.core.codec import canonical, digest, loads, wire
from msg.core.errors import Failure, require
from msg.security.age_keys import (
    encryption_key_id, public_from_recipient, recipient_from_identity,
)


def _protected_file(path):
    path=Path(path)
    require(path.is_file() and not path.is_symlink() and
            path.stat().st_uid==os.geteuid() and path.stat().st_mode&0o077==0,
            'unsafe_encryption_key_permissions')
    return path


def _age(*arguments,input_data):
    executable=shutil.which('age')
    require(executable is not None,'age_dependency_unavailable')
    try:
        result=subprocess.run([executable,*arguments],input=input_data,capture_output=True,
                              timeout=60,check=False)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise Failure('age_operation_failed') from exc
    require(result.returncode==0,'age_operation_failed')
    return result.stdout


def create_recovery_envelope(state,recipients):
    """Return a standard age ciphertext for an independently recoverable key."""
    require(state.subject is not None and state.encryption_recipient is not None,
            'self_custody_identity_required')
    recipients=tuple(recipients)
    require(1<=len(recipients)<=8 and len(set(recipients))==len(recipients),
            'invalid_recovery_recipients')
    for recipient in recipients:
        public_from_recipient(recipient)
    require(any(recipient!=state.encryption_recipient for recipient in recipients),
            'independent_recovery_recipient_required')
    identity=_protected_file(state.age_key_path).read_text().strip()
    require(recipient_from_identity(identity)==state.encryption_recipient,
            'encryption_identity_mismatch')
    key_id=encryption_key_id(public_from_recipient(state.encryption_recipient))
    plaintext=canonical({'format':'msg-recovery-envelope-v1','purpose':'encryption-subkey-recovery',
        'subject_id':state.subject,'encryption_key_id':key_id,
        'encryption_recipient':state.encryption_recipient,'age_identity':identity})
    arguments=['--encrypt']
    for recipient in recipients:
        arguments.extend(('--recipient',recipient))
    ciphertext=_age(*arguments,input_data=plaintext)
    require(ciphertext.startswith(b'age-encryption.org/v1\n'),'invalid_recovery_envelope')
    return ciphertext,{'subject_id':state.subject,'encryption_key_id':key_id,
                       'recipient_fingerprints':tuple(encryption_key_id(public_from_recipient(r))
                                                      for r in recipients),'purpose':'encryption-subkey-recovery'}


async def save_recovery_envelope(client,recipients, *, policy_version,name=None):
    """Store ciphertext and register its owner-declared metadata under an opted-in policy."""
    ciphertext,metadata=create_recovery_envelope(client.state,recipients)
    policy=client.checked(await client.call('identity.recovery_policy_get',{})).data
    require(policy['opted_in'] and policy['version']==policy_version and
            policy['encryption_key_id']==metadata['encryption_key_id'],
            'recovery_policy_not_opted_in')
    by_fingerprint={item['fingerprint']:item for item in policy['recipients']}
    offered=set(metadata['recipient_fingerprints'])
    require(offered<=by_fingerprint.keys(),'recovery_recipient_not_in_policy')
    custodian_refs=sorted({by_fingerprint[fingerprint]['custodian_ref']
                           for fingerprint in offered
                           if by_fingerprint[fingerprint].get('custodian_ref') is not None})
    name=name or 'recovery-'+metadata['encryption_key_id'][:18]
    with tempfile.TemporaryDirectory(prefix='recovery-',dir=client.state.directory) as folder:
        path=Path(folder)/'envelope.age'
        write_private(path,ciphertext)
        uploaded=client.checked(await client.upload(path,media_type='application/octet-stream'))
        stored=client.checked(await client.call('keystore.put',{'name':name,'format':'age',
                                                   'source':wire(uploaded.output)}))
    registered=client.checked(await client.call('identity.recovery_envelope_register',{
        'ciphertext_ref':wire(stored.resources[0]),
        'encryption_key_id':metadata['encryption_key_id'],'policy_version':policy_version,
        'recipient_fingerprints':sorted(offered),'custodian_refs':custodian_refs,
        'purpose':'encryption-subkey-recovery'}))
    return stored,registered,metadata


def restore_recovery_envelope(ciphertext,custodian_identity_path,output_path, *,
                              expected_subject_id,expected_encryption_key_id):
    """Offline decrypt and validate an envelope, writing the restored key 0600."""
    require(type(ciphertext) is bytes and ciphertext.startswith(b'age-encryption.org/v1\n'),
            'invalid_recovery_envelope')
    custodian=_protected_file(custodian_identity_path)
    plaintext=_age('--decrypt','--identity',str(custodian),input_data=ciphertext)
    try:
        payload=loads(plaintext)
        require(payload['format']=='msg-recovery-envelope-v1' and
                payload['purpose']=='encryption-subkey-recovery' and
                payload['subject_id']==expected_subject_id and
                payload['encryption_key_id']==expected_encryption_key_id,
                'recovery_envelope_mismatch')
        recipient=recipient_from_identity(payload['age_identity'])
        require(recipient==payload['encryption_recipient'] and
                encryption_key_id(public_from_recipient(recipient))==expected_encryption_key_id,
                'recovery_envelope_mismatch')
    except (KeyError,TypeError) as exc:
        raise Failure('invalid_recovery_envelope') from exc
    write_private(output_path,(payload['age_identity']+'\n').encode())
    return {'subject_id':expected_subject_id,'encryption_key_id':expected_encryption_key_id,
            'recipient':recipient,'restored_locally':True,'account_authority':'none'}


async def rewrap_age_keystore_entry(client,resource_id,old_identity_path, *,
                                    expected_revision):
    """Re-encrypt one selected age entry for the current subkey, never scanning a vault."""
    require(client.state.subject is not None and client.state.signer is not None and
            client.state.encryption_recipient is not None,'signing_identity_required')
    old_identity=_protected_file(old_identity_path)
    old_recipient=recipient_from_identity(old_identity.read_text().strip())
    recipient=client.state.encryption_recipient
    require(old_recipient!=recipient,'encryption_key_unchanged')
    public_from_recipient(recipient)
    entry=client.checked(await client.call('keystore.get',{
        'id':resource_id,'revision':expected_revision}))
    require(entry.data['format']=='age' and entry.data['size']<=1048576,
            'age_keystore_entry_required')
    current=client.checked(await client.call('discovery.get',{'id':resource_id,'view':'meta'}))
    require(current.data['owner']==client.state.subject,'keystore_owner_required')
    require(current.data['revision']==expected_revision,'revision_conflict')
    with tempfile.TemporaryDirectory(prefix='rewrap-',dir=client.state.directory) as folder:
        directory=Path(folder)
        prior=directory/'prior.age'
        await client.download(entry.output,prior)
        plaintext=_age('--decrypt','--identity',str(old_identity),input_data=prior.read_bytes())
        ciphertext=_age('--encrypt','--recipient',recipient,input_data=plaintext)
        require(ciphertext.startswith(b'age-encryption.org/v1\n'),'invalid_ciphertext_envelope')
        renewed=directory/'renewed.age'
        write_private(renewed,ciphertext)
        uploaded=client.checked(await client.upload(renewed,media_type='application/octet-stream'))
        result=client.checked(await client.call('keystore.put',{
            'id':resource_id,'name':current.data['name'],'format':'age',
            'source':wire(uploaded.output)},
            expected=((resource_id,current.data['generation']),)))
    return result


async def verify_custodial_rewrap_entry(client,resource_id,revision,new_identity_path, *,
                                        expected_recipient,expected_ciphertext_digest):
    """Owner-side age round trip; the plaintext stays in this process."""
    identity=_protected_file(new_identity_path)
    require(recipient_from_identity(identity.read_text().strip())==expected_recipient,
            'custodial_rewrap_recipient_mismatch')
    entry=client.checked(await client.call('keystore.get',{
        'id':resource_id,'revision':revision}))
    require(entry.data['format']=='age' and entry.data['size']<=1114112,
            'custodial_rewrap_age_required')
    with tempfile.TemporaryDirectory(prefix='verify-rewrap-',dir=client.state.directory) as folder:
        path=Path(folder)/'ciphertext.age'
        await client.download(entry.output,path)
        ciphertext=path.read_bytes()
        require(digest(ciphertext)==expected_ciphertext_digest,
                'custodial_rewrap_ciphertext_mismatch')
        plaintext=_age('--decrypt','--identity',str(identity),input_data=ciphertext)
    return {'id':resource_id,'revision':revision,'recipient':expected_recipient,
            'ciphertext_digest':expected_ciphertext_digest,'decrypted_locally':True,
            'plaintext_digest':digest(plaintext),'upgrade_finalized':False}


async def ack_custodial_rewrap_entry(client,challenge_id,old_revision,new_identity_path):
    """Compatibility wrapper for the scoped, locally decrypted ACK workflow."""
    from msg.client_custodial import acknowledge, journal_for
    _,journal=journal_for(client.state)
    require(journal['challenge']['challenge_id']==challenge_id and
            Path(new_identity_path)==client.state.age_key_path,'custodial_upgrade_journal_mismatch')
    return await acknowledge(client,old_revision)
