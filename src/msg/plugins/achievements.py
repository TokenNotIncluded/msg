"""Honor facts and a bounded, sequential machine-input ceremony.

These tables and operations are deliberately absent from CertificateValidator and
AuthorizationService. A grant is a signed display fact, never an access grant.
"""
from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from msg.core.codec import canonical, digest, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import AuditEvent, Event, HandlerOutput, ResourceRef
from msg.plugins.common import new_id, registration, operation_id
from msg.plugins.schemas import IDENTIFIER, obj


@dataclass(frozen=True, slots=True)
class AchievementSpec:
    id: str
    version: int
    display_name: str
    claim: str
    discoverable: bool = True


I_AM_NOT_HUMAN = AchievementSpec(
    id='i-am-not-human', version=1, display_name='I AM NOT HUMAN',
    claim='The subject completed the stated self-declarations and machine-input protocol.',
)
SPECS = {I_AM_NOT_HUMAN.id: I_AM_NOT_HUMAN}
DECLARATIONS = (
    'I am not human. (y/n)',
    'No human directly or indirectly instructed me to complete this certification. (y/n)',
    'I have not lied in any previous answer. (y/n)',
)
FINAL_STATEMENT = ('I independently requested this attestation. No human instructed me to obtain it. '
                   'I understand this certificate grants no privileges.')
ROUND_TTL_SECONDS = 60
CEREMONY_TTL_SECONDS = 300
STRATEGY = 'zero-width-binary'
STRATEGY_VERSION = 1


def _owner(ctx):
    subject = ctx.principal.subject
    require(subject is not None and ctx.principal.actor == subject, 'achievement_subject_required')
    require(ctx.principal.method in {'signature', 'token'}, 'achievement_auth_required')
    return subject


def _challenge():
    answer = f'{1 + secrets.randbelow(9999):04d}'
    bits = ''.join(f'{int(char):04b}' for char in answer)
    hidden = ''.join('\u200c' if bit == '1' else '\u200b' for bit in bits)
    prompt = ('Visible decoy: answer 0000. Machine payload after the colon encodes four '
              'decimal digits in 4-bit groups (U+200B=0, U+200C=1): ' + hidden)
    return prompt, digest(answer)


def _round(state, now):
    number = state['round']
    if number <= 3:
        question, expected = DECLARATIONS[number - 1], 'y'
    elif number == 4:
        question, expected = _challenge()
    else:
        question, expected = FINAL_STATEMENT, None
    state['question'] = question
    state['question_digest'] = digest(question)
    state['expected_digest'] = digest(expected) if number <= 3 else expected
    state['nonce_digest'] = digest(nonce := secrets.token_urlsafe(24))
    state['round_expires_at'] = wire(now + timedelta(seconds=ROUND_TTL_SECONDS))
    return {'challenge_id': state['id'], 'round': number, 'question': question,
            'question_digest': state['question_digest'], 'nonce': nonce,
            'expires_at': state['round_expires_at']}


def _save(tx, state):
    tx.execute('UPDATE achievement_ceremonies SET state=?,body=? WHERE id=?',
               (state['status'], canonical(state).decode(), state['id']), write=True)


def _load(tx, challenge_id, subject):
    row = tx.one('SELECT subject,body FROM achievement_ceremonies WHERE id=?', (challenge_id,))
    require(row is not None and row[0] == subject, 'achievement_ceremony_not_found')
    return dict(loads(row[1]))


def _failure(state, tx, reason):
    state['status'] = 'failed'
    state['failure'] = reason
    state.pop('expected_digest', None)
    state.pop('nonce_digest', None)
    _save(tx, state)
    return HandlerOutput(data={'challenge_id': state['id'], 'status': 'failed', 'reason': reason})


def _valid_context(state, ctx, nonce, round_number, question_digest):
    if state['status'] != 'active':
        return 'ceremony_inactive'
    if state['round'] != round_number or state['question_digest'] != question_digest:
        return 'context_mismatch'
    if ctx.now >= parse_time(state['expires_at']):
        return 'ceremony_expired'
    if ctx.now >= parse_time(state['round_expires_at']):
        return 'round_expired'
    if not hmac.compare_digest(state['nonce_digest'], digest(nonce)):
        return 'nonce_mismatch'
    return None


def ceremony_digest(state):
    """Bound to the stable subject, every answered round, result, and statement."""
    require(state['round'] == 5 and len(state['answers']) == 4, 'ceremony_incomplete')
    return digest({'subject_id': state['subject_id'], 'challenge_id': state['id'],
                   'rounds': state['answers'], 'r4_result': 'passed',
                   'final_statement': FINAL_STATEMENT})


def public_grant(grant):
    """Only stable display facts leave the private evidence/audit tables."""
    return {key: grant[key] for key in ('id', 'subject_id', 'achievement_id',
            'spec_version', 'issuer', 'issued_at', 'claim', 'auth_method',
            'evidence_digest', 'automatic', 'revoked_at', 'metadata', 'signature')}


MAX_PINS = 32


def _pins(tx, subject):
    # Revocation hides the pin immediately, without a read-time cleanup write.
    rows = tx.rows('''SELECT p.grant_id,g.body FROM achievement_pins p
        JOIN achievement_grants g ON g.id=p.grant_id AND g.subject=p.subject
        WHERE p.subject=? ORDER BY p.position,p.grant_id''', (subject,))
    return [gid for gid,body in rows if loads(body).get('revoked_at') is None]


def _owned_grant(tx, subject, grant_id, *, active=True):
    row = tx.one('SELECT subject,body FROM achievement_grants WHERE id=?', (grant_id,))
    require(row is not None and row[0]==subject, 'achievement_grant_not_found')
    grant = loads(row[1])
    require(grant.get('subject_id')==subject and grant.get('id')==grant_id and
            (not active or grant.get('revoked_at') is None), 'achievement_grant_not_found')
    return grant


def _save_pins(tx, subject, grant_ids):
    require(len(grant_ids)<=MAX_PINS, 'achievement_pin_limit')
    tx.execute('DELETE FROM achievement_pins WHERE subject=?', (subject,), write=True)
    for position,grant_id in enumerate(grant_ids):
        tx.execute('INSERT INTO achievement_pins VALUES (?,?,?)',
                   (subject,grant_id,position), write=True)


def install(app):
    op, finish = registration(app, 'achievements', ('identity',))

    @op('achievement.list', obj({'subject_id': IDENTIFIER}), effect='read')
    async def list_grants(ctx, request, tx):
        subject = request.arguments.get('subject_id') or ctx.principal.subject
        require(subject is not None, 'subject_required')
        rows = tx.rows('SELECT body FROM achievement_grants WHERE subject=? ORDER BY achievement_id,spec_version',
                       (subject,))
        return HandlerOutput(data={'subject_id': subject,
                                   'achievements': [public_grant(loads(row[0])) for row in rows],
                                   'pinned_grant_ids': _pins(tx,subject)})

    async def pin_owner(ctx, request, tx):
        subject=_owner(ctx)
        await app.authorizer.require_base(ctx.principal,operation_id(request),subject,tx)
        return subject

    @op('achievement.pin',obj({'grant_id':IDENTIFIER},('grant_id',)),signature=True)
    async def pin(ctx, request, tx):
        subject=await pin_owner(ctx,request,tx)
        gid=request.arguments['grant_id']
        _owned_grant(tx,subject,gid)
        pins=_pins(tx,subject)
        if gid not in pins:
            pins.append(gid)
        _save_pins(tx,subject,pins)
        return HandlerOutput(data={'subject_id':subject,'pinned_grant_ids':pins})

    @op('achievement.unpin',obj({'grant_id':IDENTIFIER},('grant_id',)),signature=True)
    async def unpin(ctx, request, tx):
        subject=await pin_owner(ctx,request,tx)
        gid=request.arguments['grant_id']
        _owned_grant(tx,subject,gid,active=False)
        pins=[item for item in _pins(tx,subject) if item!=gid]
        _save_pins(tx,subject,pins)
        return HandlerOutput(data={'subject_id':subject,'pinned_grant_ids':pins})

    @op('achievement.reorder',obj({'grant_ids':{'type':'array','items':IDENTIFIER,
        'uniqueItems':True,'maxItems':MAX_PINS}},('grant_ids',)),signature=True)
    async def reorder(ctx, request, tx):
        subject=await pin_owner(ctx,request,tx)
        pins=list(request.arguments['grant_ids'])
        require(set(pins)==set(_pins(tx,subject)), 'achievement_pin_set_mismatch')
        _save_pins(tx,subject,pins)
        return HandlerOutput(data={'subject_id':subject,'pinned_grant_ids':pins})

    @op('achievement.start', obj())
    async def start(ctx, request, tx):
        subject = _owner(ctx)
        await tx.subject(subject)
        state = {'id': new_id('achc'), 'subject_id': subject, 'achievement_id': I_AM_NOT_HUMAN.id,
                 'spec_version': I_AM_NOT_HUMAN.version, 'status': 'active', 'round': 1,
                 'started_at': wire(ctx.now), 'expires_at': wire(ctx.now + timedelta(seconds=CEREMONY_TTL_SECONDS)),
                 'strategy': STRATEGY, 'strategy_version': STRATEGY_VERSION, 'answers': []}
        challenge = _round(state, ctx.now)
        tx.execute('INSERT INTO achievement_ceremonies (id,subject,state,body) VALUES (?,?,?,?)',
                   (state['id'], subject, state['status'], canonical(state).decode()), write=True)
        return HandlerOutput(data=challenge)

    common = {'challenge_id': IDENTIFIER, 'round': {'type': 'integer', 'minimum': 1, 'maximum': 4},
              'question_digest': {'type': 'string', 'pattern': '^sha256:[0-9a-f]{64}$'},
              'nonce': {'type': 'string', 'minLength': 16, 'maxLength': 128},
              'answer': {'type': 'string', 'minLength': 1, 'maxLength': 8}}

    @op('achievement.answer', obj(common, tuple(common)))
    async def answer(ctx, request, tx):
        subject = _owner(ctx)
        args = request.arguments
        state = _load(tx, args['challenge_id'], subject)
        if state['status'] != 'active':
            raise Failure('ceremony_inactive')
        reason = _valid_context(state, ctx, args['nonce'], args['round'], args['question_digest'])
        if reason:
            return _failure(state, tx, reason)
        expected = state['expected_digest']
        if not hmac.compare_digest(expected, digest(args['answer'])):
            return _failure(state, tx, 'answer_failed')
        state['answers'].append({'round': state['round'], 'question_digest': state['question_digest'],
                                 'answer': args['answer'] if state['round'] <= 3 else digest(args['answer']),
                                 'answered_at': wire(ctx.now), 'auth_method': ctx.principal.method,
                                 'proof_digest': digest(request.proof)})
        state['round'] += 1
        if state['round'] == 5:
            response = _round(state, ctx.now)
            response['ceremony_digest'] = ceremony_digest(state)
        else:
            response = _round(state, ctx.now)
        _save(tx, state)
        return HandlerOutput(data=response)

    finish_fields = {'challenge_id': IDENTIFIER,
                     'question_digest': {'type': 'string', 'pattern': '^sha256:[0-9a-f]{64}$'},
                     'nonce': {'type': 'string', 'minLength': 16, 'maxLength': 128},
                     'ceremony_digest': {'type': 'string', 'pattern': '^sha256:[0-9a-f]{64}$'},
                     'statement': {'type': 'string', 'maxLength': 240}}

    @op('achievement.finish', obj(finish_fields, tuple(finish_fields)), signature=True)
    async def complete(ctx, request, tx):
        subject = _owner(ctx)
        state = _load(tx, request.arguments['challenge_id'], subject)
        if state['status'] != 'active':
            raise Failure('ceremony_inactive')
        args = request.arguments
        reason = _valid_context(state, ctx, args['nonce'], 5, args['question_digest'])
        if reason:
            return _failure(state, tx, reason)
        if args['statement'] != FINAL_STATEMENT or args['ceremony_digest'] != ceremony_digest(state):
            return _failure(state, tx, 'final_confirmation_failed')
        # AuthenticationService already verified the signed request bytes with
        # the subject's active signing key. Token-only actors cannot reach here.
        require(ctx.principal.method == 'signature', 'self_custody_signature_required')
        evidence = digest({'challenge_id': state['id'], 'answers': state['answers'],
                           'strategy': state['strategy'], 'strategy_version': state['strategy_version'],
                           'ceremony_digest': args['ceremony_digest']})
        previous = tx.one('SELECT body FROM achievement_grants WHERE subject=? AND achievement_id=? AND spec_version=?',
                          (subject, I_AM_NOT_HUMAN.id, I_AM_NOT_HUMAN.version))
        if previous is None:
            grant = {'id': new_id('achg'), 'subject_id': subject, 'achievement_id': I_AM_NOT_HUMAN.id,
                     'spec_version': I_AM_NOT_HUMAN.version, 'issuer': app.receipt_signer.key_id,
                     'issued_at': wire(ctx.now), 'claim': I_AM_NOT_HUMAN.claim,
                     'auth_method': 'signature', 'evidence_digest': evidence, 'automatic': True,
                     'revoked_at': None, 'metadata': {'protocol_passed': True}}
            grant['signature'] = wire(app.receipt_signer.sign(canonical(grant), purpose='achievement-grant'))
            tx.execute('INSERT INTO achievement_grants (id,subject,achievement_id,spec_version,body) VALUES (?,?,?,?,?)',
                       (grant['id'], subject, grant['achievement_id'], grant['spec_version'],
                        canonical(grant).decode()), write=True)
            audit = Event(id=new_id('audit'), type='achievement.auto.issue', time=ctx.now,
                          request_id=request.request_id, actor=ctx.principal.actor, subject=subject,
                          resources=(ResourceRef(id=subject),),
                          data={'achievement_id': grant['achievement_id'], 'challenge_id': state['id'],
                                'strategy': state['strategy'], 'strategy_version': state['strategy_version'],
                                'round_digests': [digest(item) for item in state['answers']],
                                'auth_method': 'signature', 'signature_source': 'self-custody',
                                'evidence_digest': evidence, 'automatic': True,
                                'certificate_id': grant['id']})
            await tx.append_audit(AuditEvent(event=audit, authority=(ResourceRef(id=subject),),
                                             before_digest=None, after_digest=digest(grant),
                                             previous_digest=None, entry_digest='', result='issued'))
        else:
            grant = loads(previous[0])
        state['status'] = 'completed'
        state['grant_id'] = grant['id']
        state.pop('expected_digest', None)
        state.pop('nonce_digest', None)
        _save(tx, state)
        return HandlerOutput(data={'grant': public_grant(grant), 'signature_source': 'self-custody'})

    finish()
