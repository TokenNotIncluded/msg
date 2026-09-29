"""Typed policy restrictions for pinned recovery v2; never a promotion contract."""
from dataclasses import replace

from msg.constants import ROOT_SUBJECT
from msg.core.codec import parse_time
from msg.core.errors import require

FACTS = frozenset({'resource.acl.restrict', 'topic.policy.restrict', 'topic_ban.set'})
DOMAINS = {'resource.acl.restrict': 'resource_acl', 'topic.policy.restrict': 'topic_policy',
           'topic_ban.set': 'topic_ban'}


def validate(fact):
    kind, value = fact['kind'], fact['value']
    require(type(value) is dict, 'recovery_checkpoint_invalid')
    if kind == 'resource.acl.restrict':
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
    """Refuse owner/group changes and permission expansion instead of inventing authority."""
    kind, value, target, subject = (fact[k] for k in ('kind', 'value', 'target', 'subject'))
    resource = await tx.resource(target)
    require(resource.owner == subject, 'recovery_fact_subject_mismatch')
    if kind == 'resource.acl.restrict':
        require(resource.owner == value['owner'] and resource.group == value['group'],
                'recovery_policy_authority_change_unsupported')
        mode = resource.mode & value['mode']
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
