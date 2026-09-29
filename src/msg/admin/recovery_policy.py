"""Typed policy restrictions for pinned recovery v2; never a promotion contract."""
from dataclasses import replace
import re

from msg.constants import ROOT_SUBJECT
from msg.core.codec import decode, digest, loads, parse_time
from msg.core.errors import require
from msg.core.models import Certificate

FACTS = frozenset({'resource.acl.restrict', 'topic.policy.restrict', 'topic_ban.set', 'certificate.current', 'resource.authority.reconcile'})
DOMAINS = {'resource.acl.restrict': 'resource_acl', 'topic.policy.restrict': 'topic_policy',
           'topic_ban.set': 'topic_ban', 'certificate.current': 'certificates',
           'resource.authority.reconcile': 'resource_authority'}


def validate(fact):
    kind, value = fact['kind'], fact['value']
    require(type(value) is dict, 'recovery_checkpoint_invalid')
    if kind == 'resource.authority.reconcile':
        require(set(value) == {'previous', 'current'}, 'recovery_checkpoint_invalid')
        for state in value.values():
            require(type(state) is dict and set(state) == {'owner', 'group', 'parent'} and
                    all(type(state[k]) is str and 0 < len(state[k]) <= 256 for k in ('owner', 'group')) and
                    (state['parent'] is None or
                     (type(state['parent']) is str and 0 < len(state['parent']) <= 256)),
                    'recovery_checkpoint_invalid')
        require(fact['subject'] == value['previous']['owner'], 'recovery_checkpoint_invalid')
    elif kind == 'certificate.current':
        require(set(value) == {'body_digest'} and
                (value['body_digest'] is None or
                 (type(value['body_digest']) is str and
                  re.fullmatch(r'sha256:[0-9a-f]{64}', value['body_digest']) is not None)),
                'recovery_checkpoint_invalid')
    elif kind == 'resource.acl.restrict':
        require(set(value) == {'owner', 'group', 'mode'} and
                all(type(value[k]) is str and 0 < len(value[k]) <= 256 for k in ('owner', 'group')) and
                type(value['mode']) is int and 0 <= value['mode'] <= 0o777,
                'recovery_checkpoint_invalid')
    elif kind == 'topic.policy.restrict':
        require(set(value) == {'membership_policy'} and
                value['membership_policy'] in ('open', 'approval', 'invite', 'closed'),
                'recovery_checkpoint_invalid')
    else:
        require(kind == 'topic_ban.set' and set(value) == {'expires_at'} and
                (value['expires_at'] is None or parse_time(value['expires_at']) > parse_time(fact['at'])),
                'recovery_checkpoint_invalid')


async def apply(tx, fact):
    """Reconcile pinned facts without granting live authority or rewriting signatures."""
    kind, value, target, subject = (fact[k] for k in ('kind', 'value', 'target', 'subject'))
    if kind == 'certificate.current':
        row = tx.one('SELECT subject,revoked,body FROM certificates WHERE id=?', (target,))
        require(row is not None, 'recovery_fact_missing')
        original = decode(Certificate, loads(row[2]))
        require(row[0] == original.subject_id == subject and original.resource_id == target,
                'recovery_fact_subject_mismatch')
        # The exact signed body includes grants, constraints, issuance policy,
        # issuer/parent/source bindings and validity. Never rewrite it to invent
        # a constrained signature, or reactivate an already revoked certificate.
        if row[1] != 1 and digest(original) != value['body_digest']:
            tx.execute('UPDATE certificates SET revoked=1 WHERE id=?', (target,), write=True)
            return True
        return False
    resource = await tx.resource(target)
    if kind == 'resource.authority.reconcile':
        from msg.security.quarantine import active
        require(active(tx), 'recovery_quarantine_required')
        previous, current = value['previous'], value['current']
        actual = {field: getattr(resource, field) for field in ('owner', 'group', 'parent')}
        require(actual in (previous, current), 'recovery_authority_state_mismatch')
        await tx.subject(current['owner'])
        await tx.organization(current['group'])
        if current['parent'] is not None:
            require(current['parent'] != target, 'parent_cycle')
            await tx.resource(current['parent'])
            require(all(item.id != target for item in await tx.ancestors(current['parent'])), 'parent_cycle')
        if actual == current:
            return False
        # These are staged current facts, not authority to reopen the service.
        # Old and new scope-derived rights remain blocked globally until a later
        # complete, independently verified authority reconstruction is possible.
        await tx.replace(replace(resource, **current, generation=resource.generation + 1,
                                 modified_at=max(resource.modified_at, parse_time(fact['at'])),
                                 modified_by=ROOT_SUBJECT), resource.generation)
        return True
    require(resource.owner == subject, 'recovery_fact_subject_mismatch')
    if kind == 'resource.acl.restrict':
        require(resource.owner == value['owner'] and resource.group == value['group'],
                'recovery_policy_authority_change_unsupported')
        mode = (resource.mode & 0o7000) | (resource.mode & value['mode'])
        if resource.mode == mode:
            return False
        await tx.replace(replace(resource, mode=mode, generation=resource.generation + 1,
                                 modified_at=max(resource.modified_at, parse_time(fact['at'])),
                                 modified_by=ROOT_SUBJECT),
                         resource.generation)
        return True
    require(resource.type == 'topic', 'not_a_topic')
    if kind == 'topic.policy.restrict':
        row = tx.one('SELECT membership_policy FROM topic_settings WHERE topic=?', (target,))
        previous = row[0] if row is not None else 'open'
        desired = value['membership_policy']
        require(previous in {'open', 'approval', 'invite', 'closed'}, 'recovery_policy_invalid')
        # Approval and invite are incomparable; their conservative intersection
        # is closed. Replaying an earlier broader ceiling never reopens a topic.
        if desired == 'open' or previous == desired:
            return False
        if previous != 'open':
            desired = 'closed'
        if previous == desired:
            return False
        tx.execute('INSERT INTO topic_settings (topic,membership_policy) VALUES (?,?) '
                   'ON CONFLICT(topic) DO UPDATE SET membership_policy=excluded.membership_policy',
                   (target, desired), write=True)
        return True
    raise AssertionError('topic_ban.set uses the existing replay membership-removal path')
