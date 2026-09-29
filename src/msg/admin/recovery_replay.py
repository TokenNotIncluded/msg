"""Local recovery's partial authority replay, never a promotion or trust bootstrap.

A pin MUST arrive through an independently authenticated operator channel outside
this database and its backup/rollback set. Neither a database watermark nor an
audit hash chain supplies that pin. There is deliberately no default trust key,
network operation, configuration override, or quarantine-clearing operation here.
Unknown fact kinds require a newer reviewed replay implementation and stay blocked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from msg.admin import recovery_policy
from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, decode, digest, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import AuditEvent, Certificate, Credential, Event, Membership, Signature
from msg.security.crypto import key_id, verify
from msg.security.quarantine import SETTING, active

FORMAT = 'msg-revocation-checkpoint-v1'
POLICY_FORMAT = 'msg-revocation-checkpoint-v2'
SUPPORTED_FACTS = frozenset({'credential.revoke', 'certificate.revoke', 'share_grant.revoke',
    'share_grant_v2.revoke', 'share_link.revoke', 'membership.remove',
    'topic_membership.remove', 'topic_ban.apply', 'topic_ban.lift', 'identity_key.retire',
    'encryption_key.retire', 'vault.destroy'})
PROMOTION_BLOCKED_REASONS = (
    'quarantine_remains',
    'supported_fact_inventory_incomplete',
    'backup_retirement_not_attested',
)
_SHARE_TABLES = {'share_grant.revoke': 'share_grants', 'share_grant_v2.revoke': 'share_grants_v2',
                 'share_link.revoke': 'share_links'}
_MAX_ENTRIES = 10000


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedCheckpointPin:
    """Independently approved exact head, not a value extracted from the packet.

    Creating this internal value is the trusted local caller's responsibility.
    It does not assert that an offline source was current or operator-approved.
    Production remains blocked without evidence for that separate trust ceremony.
    """
    service: str
    public_key: bytes
    digest: str
    sequence: int


def verify_checkpoint(packet, *, pin):
    require(isinstance(pin, TrustedCheckpointPin), 'recovery_checkpoint_pin_required')
    try:
        require(type(packet) is dict and set(packet) == {'checkpoint', 'signature'},
                'recovery_checkpoint_invalid')
        body = packet['checkpoint']
        policy = type(body) is dict and body.get('format') == POLICY_FORMAT
        fields = {'format', 'service', 'source_backup_sha256', 'sequence', 'entries'}
        if policy:
            fields.add('coverage')
        require(type(body) is dict and set(body) == fields,
            'recovery_checkpoint_invalid')
        require(type(pin.public_key) is bytes and len(pin.public_key) == 32 and
                type(pin.sequence) is int and 0 <= pin.sequence <= _MAX_ENTRIES and
                type(body['sequence']) is int and body['sequence'] == pin.sequence and
                body['format'] in {FORMAT, POLICY_FORMAT} and body['service'] == pin.service and
                isinstance(pin.service, str) and bool(pin.service) and
                isinstance(body['source_backup_sha256'], str) and
                re.fullmatch('[0-9a-f]{64}', body['source_backup_sha256']) is not None and
                digest(body) == pin.digest, 'recovery_checkpoint_invalid')
        entries = body['entries']
        require(type(entries) is list and len(entries) == pin.sequence,
                'recovery_checkpoint_invalid')
        domains = set()
        authority_chains = {}
        for sequence, fact in enumerate(entries, 1):
            typed = policy and type(fact) is dict and fact.get('kind') in recovery_policy.FACTS
            fact_fields = {'sequence', 'kind', 'subject', 'target', 'at'}
            if typed:
                fact_fields.add('value')
            require(type(fact) is dict and set(fact) == fact_fields and
                type(fact['sequence']) is int and fact['sequence'] == sequence and
                fact['kind'] in (SUPPORTED_FACTS | recovery_policy.FACTS if policy else SUPPORTED_FACTS) and
                all(isinstance(fact[name], str) and 0 < len(fact[name]) <= 256
                    for name in ('subject', 'target')), 'recovery_checkpoint_invalid')
            require(parse_time(fact['at']) <= datetime.now(UTC), 'recovery_checkpoint_invalid')
            if typed:
                recovery_policy.validate(fact)
            if fact['kind'] == 'resource.authority.reconcile':
                chain = authority_chains.setdefault(fact['target'], [])
                require(not chain or chain[-1]['value']['current'] == fact['value']['previous'],
                        'recovery_authority_chain_gap')
                chain.append(fact)
            domains.add(recovery_policy.DOMAINS[fact['kind']] if typed else 'revocations')
        if policy:
            require(body['coverage'] == {'complete': False, 'domains': sorted(domains)} and
                    body['coverage'].get('complete') is False, 'recovery_checkpoint_incomplete_contract')
        verify(pin.public_key, canonical(body), decode(Signature, packet['signature']),
               purpose='recovery-checkpoint-v2' if policy else 'recovery-checkpoint-v1')
        # Own the verified bytes across the subsequent await/transaction boundary.
        return loads(canonical(body))
    except (Failure, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise Failure('recovery_checkpoint_invalid') from exc


def _owned(row, subject):
    require(row is not None, 'recovery_fact_missing')
    require(row[0] == subject, 'recovery_fact_subject_mismatch')
    return row


def _earlier(previous, at):
    return at if previous is None or parse_time(at) < parse_time(previous) else previous


def _revoke_credential(tx, subject, target, at):
    row = _owned(tx.one('SELECT subject,body FROM credentials WHERE id=?', (target,)), subject)
    credential = decode(Credential, loads(row[1]))
    require(credential.id == target and credential.subject_id == subject,
            'recovery_fact_subject_mismatch')
    when = parse_time(at)
    changed = credential.revoked_at is None or credential.revoked_at > when
    if changed:
        revoked = replace(credential, revoked_at=when)
        tx.execute('UPDATE credentials SET body=? WHERE id=?',
                   (canonical(revoked).decode(), target), write=True)
    if credential.kind == 'token':
        delivery = tx.one('SELECT subject,consumed_at FROM token_deliveries WHERE credential_id=?',
                          (target,))
        if delivery is not None:
            _owned(delivery, subject)
            earliest = _earlier(delivery[1], at)
            if earliest != delivery[1]:
                tx.execute('UPDATE token_deliveries SET consumed_at=? WHERE credential_id=?',
                           (earliest, target), write=True)
                changed = True
    return changed


async def _apply(tx, fact):
    subject, target, at, kind = (fact[name] for name in ('subject', 'target', 'at', 'kind'))
    if kind in recovery_policy.FACTS - {'topic_ban.set'}:
        return await recovery_policy.apply(tx, fact)
    if kind == 'credential.revoke':
        return _revoke_credential(tx, subject, target, at)
    if kind == 'certificate.revoke':
        row = _owned(tx.one('SELECT subject,revoked,body FROM certificates WHERE id=?',
                            (target,)), subject)
        certificate = decode(Certificate, loads(row[2]))
        require(certificate.resource_id == target and certificate.subject_id == subject,
                'recovery_fact_subject_mismatch')
        if row[1] != 1:
            tx.execute('UPDATE certificates SET revoked=1 WHERE id=?', (target,), write=True)
            return True
        return False
    if kind in _SHARE_TABLES:
        table = _SHARE_TABLES[kind]  # Fixed closed vocabulary, never caller SQL.
        row = _owned(tx.one(f'SELECT grantor,revoked_at FROM {table} WHERE id=?', (target,)), subject)
        earliest = _earlier(row[1], at)
        if row[1] != earliest:
            tx.execute(f'UPDATE {table} SET revoked_at=? WHERE id=?', (earliest, target), write=True)
            return True
        return False
    if kind == 'membership.remove':
        require(target != 'g_public', 'recovery_virtual_membership_requires_identity_policy')
        row = tx.one('SELECT generation,body FROM memberships WHERE org=? AND subject=?',
                     (target, subject))
        require(row is not None, 'recovery_fact_missing')
        member = decode(Membership, loads(row[1]))
        require(member.subject_id == subject and member.organization_id == target and
                member.version == row[0], 'recovery_fact_subject_mismatch')
        if member.status != 'rejected':
            await tx.update_identity(replace(member, status='rejected', version=member.version + 1),
                                     member.version)
            return True
        return False
    if kind == 'topic_membership.remove':
        row = tx.one('SELECT status FROM topic_memberships WHERE topic=? AND subject=?', (target, subject))
        require(row is not None, 'recovery_fact_missing')
        if row[0] != 'removed':
            tx.execute("UPDATE topic_memberships SET status='removed' WHERE topic=? AND subject=?",
                       (target, subject), write=True)
            return True
        return False
    if kind in {'topic_ban.apply', 'topic_ban.set'}:
        expires = fact['value']['expires_at'] if kind == 'topic_ban.set' else None
        if kind == 'topic_ban.set':
            await tx.subject(subject)
            require((await tx.resource(target)).type == 'topic', 'not_a_topic')
        require(tx.one('SELECT id FROM resources WHERE id=?', (target,)) is not None,
                'recovery_fact_missing')
        # A live ban removes membership as well as denying topic participation.
        # Preserve that effect when replaying an old snapshot, including when a
        # later fact lifts the ban: lifting never restores an old admin role.
        member = tx.one('SELECT role,status FROM topic_memberships WHERE topic=? AND subject=?',
                        (target, subject))
        member_changed = member is not None and member != ('member', 'removed')
        if member_changed:
            tx.execute("UPDATE topic_memberships SET role='member',status='removed' "
                       'WHERE topic=? AND subject=?', (target, subject), write=True)
        row = tx.one('SELECT status,expires_at FROM topic_bans WHERE topic=? AND subject=?', (target, subject))
        if row is None:
            tx.execute('''INSERT INTO topic_bans
                (topic,subject,actor,created_at,expires_at,reason,status)
                VALUES (?,?,?,?,?,?,?)''',
                (target, subject, ROOT_SUBJECT, at, expires, 'recovery-replay', 'active'), write=True)
            return True
        # Restrictions never shorten an existing active ban. A permanent ban
        # dominates finite expiries; only a separate explicit lift can remove it.
        if kind == 'topic_ban.set' and row[0] == 'active':
            if row[1] is None or expires is None:
                expires = None
            elif parse_time(row[1]) > parse_time(expires):
                expires = row[1]
        # V1 has no expiry and must not inherit a stale snapshot expiry.
        if row[0] != 'active' or row[1] != expires:
            tx.execute("UPDATE topic_bans SET status='active',expires_at=? WHERE topic=? AND subject=?",
                       (expires, target, subject), write=True)
            return True
        return member_changed
    if kind == 'topic_ban.lift':
        row = tx.one('SELECT status FROM topic_bans WHERE topic=? AND subject=?', (target, subject))
        require(row is not None, 'recovery_fact_missing')
        if row[0] != 'lifted':
            tx.execute("UPDATE topic_bans SET status='lifted' WHERE topic=? AND subject=?",
                       (target, subject), write=True)
            return True
        return False
    if kind in {'identity_key.retire', 'encryption_key.retire'}:
        table = 'identity_keys' if kind == 'identity_key.retire' else 'encryption_subkeys'
        row = _owned(tx.one(f'SELECT subject,retired_at,is_primary FROM {table} WHERE key_id=?',
                            (target,)), subject)
        earliest = _earlier(row[1], at)
        changed = row[1] != earliest or row[2] != 0
        if changed:
            tx.execute(f'UPDATE {table} SET retired_at=?,is_primary=0 WHERE key_id=?',
                       (earliest, target), write=True)
        if kind == 'identity_key.retire':
            changed = _revoke_credential(tx, subject, target, at) or changed
            vault = tx.one('SELECT signing_key_id,signing_nonce,signing_ciphertext,age_ciphertext '
                           'FROM custodial_vault WHERE subject=?', (subject,))
            if vault is not None and vault[0] == target and (vault[1] is not None or vault[2] is not None):
                status = 'decrypt_only' if vault[3] is not None else 'destroyed'
                tx.execute('UPDATE custodial_vault SET signing_nonce=NULL,signing_ciphertext=NULL,'
                           'status=? WHERE subject=?', (status, subject), write=True)
                changed = True
        return changed
    require(kind == 'vault.destroy', 'recovery_checkpoint_invalid')
    row = tx.one('SELECT encryption_key_id,signing_nonce,signing_ciphertext,age_nonce,'
                 'age_ciphertext,status FROM custodial_vault WHERE subject=?', (subject,))
    require(row is not None, 'recovery_fact_missing')
    require(row[0] == target, 'recovery_fact_subject_mismatch')
    if any(value is not None for value in row[1:5]) or row[5] != 'destroyed':
        tx.execute("UPDATE custodial_vault SET signing_nonce=NULL,signing_ciphertext=NULL,"
                   "age_nonce=NULL,age_ciphertext=NULL,status='destroyed',destroyed_at=? WHERE subject=?",
                   (at, subject), write=True)
        return True
    return False


async def replay(store, packet, *, pin, config_dir):
    """Physical-console entry; neither a TTY flag nor an HTTP caller can authorize it."""
    from msg.admin.root import require_local_console
    operator = require_local_console(config_dir)
    return await _replay(store, packet, pin=pin, operator=operator)


async def _replay(store, packet, *, pin, operator):
    """Atomically apply explicitly pinned partial authority facts to quarantine.

    Always re-apply facts, including after a repeated invocation/restart: cached
    watermarks are not evidence that an authorization row has not been restored.
    This neither imports keys, emits effects, nor clears quarantine. topic_ban.apply
    removes restored membership; explicit topic_ban.lift relaxes the ban without
    restoring membership. V2 also reconciles typed policy/authority facts. No
    receipt asserts complete authority or authorizes promotion.
    """
    require(isinstance(operator, str) and bool(operator), 'recovery_operator_required')
    body = verify_checkpoint(packet, pin=pin)
    async with store.transaction(write=True) as tx:
        require(active(tx), 'recovery_quarantine_required')
        gate = tx.setting(SETTING)
        require(isinstance(gate, dict) and gate.get('format') == 'msg-recovery-quarantine-v1' and
                gate.get('outbound_enabled') is False and gate.get('authority') == 'health_only',
                'recovery_quarantine_invalid')
        require(gate.get('source_backup_sha256') == body['source_backup_sha256'],
                'recovery_checkpoint_backup_mismatch')
        previous = tx.setting('recovery_replay')
        if previous is not None:
            require(isinstance(previous, dict) and previous.get('format') in {FORMAT, body['format']} and
                    type(previous.get('sequence')) is int and 0 <= previous['sequence'] <= pin.sequence and
                    previous.get('prefix_digest') == digest(body['entries'][:previous['sequence']]),
                    'recovery_checkpoint_regression')
        changed = 0
        reconciled = set()
        authority_chains = {}
        for item in body['entries']:
            if item['kind'] == 'resource.authority.reconcile':
                authority_chains.setdefault(item['target'], []).append(item)
        for fact in body['entries']:
            if fact['kind'] == 'resource.authority.reconcile':
                if fact['target'] in reconciled:
                    continue
                chain = authority_chains[fact['target']]
                actual_resource = await tx.resource(fact['target'])
                actual = {field: getattr(actual_resource, field) for field in ('owner', 'group', 'parent')}
                require(actual in [chain[0]['value']['previous']] +
                        [item['value']['current'] for item in chain], 'recovery_authority_state_mismatch')
                # Collapse only a verified contiguous chain; an old or intermediate
                # snapshot converges once without transiently restoring old scope.
                fact = dict(chain[-1], subject=actual['owner'],
                            value={'previous': actual, 'current': chain[-1]['value']['current']})
                reconciled.add(fact['target'])
            if fact['kind'] in {'resource.acl.restrict', 'topic.policy.restrict'} and fact['target'] in authority_chains:
                chain = authority_chains[fact['target']]
                states = [chain[0]['value']['previous']] + [item['value']['current'] for item in chain]
                actual_resource = await tx.resource(fact['target'])
                actual = {field: getattr(actual_resource, field) for field in ('owner', 'group', 'parent')}
                require(actual in states, 'recovery_authority_state_mismatch')
                bound = chain[0]['value']['previous']
                for transition in chain:
                    if transition['sequence'] < fact['sequence']:
                        bound = transition['value']['current']
                if fact['kind'] == 'resource.acl.restrict':
                    require(fact['subject'] == fact['value']['owner'] == bound['owner'] and
                            fact['value']['group'] == bound['group'], 'recovery_fact_subject_mismatch')
                    fact = dict(fact, subject=actual['owner'],
                                value=dict(fact['value'], owner=actual['owner'], group=actual['group']))
                else:
                    require(fact['subject'] == bound['owner'], 'recovery_fact_subject_mismatch')
                    fact = dict(fact, subject=actual['owner'])
            changed += bool(await _apply(tx, fact))
        receipt = {'format': body['format'], 'checkpoint_digest': pin.digest, 'sequence': pin.sequence,
                   'prefix_digest': digest(body['entries']), 'scope': 'supported_authority_reconciliation_facts',
                   'promotion': 'blocked',
                   'promotion_blocked_reasons': list(PROMOTION_BLOCKED_REASONS),
                   'backup_retired': False}
        if body['format'] == POLICY_FORMAT:
            receipt['scope'] = 'supported_policy_reconciliation_only'
            receipt['coverage'] = body['coverage']
            staged = sorted({fact['target'] for fact in body['entries']
                             if fact['kind'] == 'resource.authority.reconcile'})
            if staged:
                receipt['scope'] = 'supported_policy_reconciliation_only'
                receipt['authority_rebuild_required'] = staged
                receipt['promotion_blocked_reasons'].append('derived_authority_inventory_incomplete')
        if changed:
            tx.set_setting('authorization_epoch', tx.setting('authorization_epoch', 0) + 1)
        if changed or previous != receipt:
            tx.set_setting('recovery_replay', receipt)
            event = Event(id='audit_' + uuid4().hex, type='recovery.revocation_replay',
                          time=datetime.now(UTC), request_id=pin.digest, actor=ROOT_SUBJECT, subject=ROOT_SUBJECT,
                          resources=(), data=dict(receipt, operator=operator,
                              checkpoint_key_id=key_id(pin.public_key)))
            await tx.append_audit(AuditEvent(event=event, authority=(), before_digest=digest(previous),
                after_digest=digest(receipt), previous_digest=None, entry_digest='', result='quarantined'))
        return dict(receipt, changed=changed)


def selftest():
    """Ephemeral signer test only; does not inspect or change any installation."""
    from msg.security.crypto import Ed25519Signer
    signer = Ed25519Signer.generate()
    body = {'format': FORMAT, 'service': 'https://isolated.invalid',
            'source_backup_sha256': '0' * 64, 'sequence': 0, 'entries': []}
    packet = {'checkpoint': body, 'signature': wire(signer.sign(
        canonical(body), purpose='recovery-checkpoint-v1'))}
    pin = TrustedCheckpointPin(service=body['service'], public_key=signer.public_key,
                               digest=digest(body), sequence=0)
    require(verify_checkpoint(packet, pin=pin) == body, 'recovery_selftest_failed')
    rejected = []
    for invalid in (None, replace(pin, public_key=Ed25519Signer.generate().public_key)):
        try:
            verify_checkpoint(packet, pin=invalid)
        except Failure as exc:
            rejected.append(exc.code)
        else:
            raise Failure('recovery_selftest_failed')
    require(rejected == ['recovery_checkpoint_pin_required', 'recovery_checkpoint_invalid'],
            'recovery_selftest_failed')
    return {'format': FORMAT, 'scope': 'isolated_signature_contract_only',
            'checks': 3, 'status': 'passed', 'database_used': False,
            'production_evidence': False, 'promotion': 'blocked'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selftest', action='store_true', required=True)
    parser.parse_args()
    print(canonical(selftest()).decode())
