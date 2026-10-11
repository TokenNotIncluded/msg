"""Key-approved browser sessions and the source limits of native API credentials.

Cookies only resume an explicitly signed, short-lived browser grant. No external
identity, authorization server, refresh token or third-party callback is trusted.
Only cookie digests persist. All grants recheck their source on each request.
"""

import hashlib
import hmac
import secrets
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from msg.core.codec import canonical, decode, loads, parse_time, wire
from msg.core.errors import require
from msg.core.models import CapabilityGrant, Credential, Scope
from msg.security.browser_actions import BROWSER_POST_WRITES
from msg.security.policy import constraints_subset, operations_cover, scope_subset
from msg.security.quarantine import require_live_authority

CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def secret():
    return secrets.token_urlsafe(32)


def state_id(kind, value):
    return kind + ':' + hashlib.sha256(value.encode()).hexdigest()


def put(tx, id, kind, expires, body):
    tx.execute(
        'INSERT INTO credential_states VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE '
        'SET expires=excluded.expires,body=excluded.body',
        (id, kind, wire(expires), canonical(body).decode()),
        write=True,
    )


def get(tx, id, now, *, expired=False):
    row = tx.one('SELECT expires,body FROM credential_states WHERE id=?', (id,))
    require(row is not None and (expired or parse_time(row[0]) > now), 'invalid_grant')
    return parse_time(row[0]), loads(row[1])


def save(tx, id, body):
    tx.execute(
        'UPDATE credential_states SET body=? WHERE id=?', (canonical(body).decode(), id), write=True
    )


async def require_ceiling(tx, ceiling, parent):
    # A derived credential is a limit, never a durable copy of authority which
    # its signing source has since lost. Reuse the same containment vocabulary
    # as certificate/delegation validation, including scope and constraints.
    for grant in ceiling:
        require(
            any([
                allowed.capability == grant.capability
                and allowed.version == grant.version
                and operations_cover(allowed.operations, grant.operations)
                and await scope_subset(grant.scope, allowed.scope, tx)
                and constraints_subset(grant.constraints, allowed.constraints)
                for allowed in parent
            ]),
            'invalid_grant',
        )


async def require_source(tx, body, now, *, custodial_ceiling):
    parent = await tx.credential(body['parent'])
    subject = await tx.subject(body['subject'])
    require(
        parent.subject_id == subject.resource_id
        and not parent.id.startswith(('t_oauth_', 't_browser_'))
        and not subject.local_only
        and subject.auth_version == body['auth_version']
        and parent.revoked_at is None
        and parent.not_before <= now
        and (parent.expires_at is None or parent.expires_at > now),
        'invalid_grant',
    )
    require(not body.get('login_binding'), 'invalid_grant')
    if body.get('custodial'):
        row = tx.one(
            'SELECT signing_key_id,status FROM custodial_vault WHERE subject=?',
            (subject.resource_id,),
        )
        require(subject.kind == 'custodial' and row == (parent.id, 'active'), 'invalid_grant')
    # Vault keys deliberately carry no request grants. Custodial sessions retain
    # their captured token limits under the current temporary policy, while the
    # vault key above remains only their live identity/revocation binding.
    await require_ceiling(
        tx,
        tuple(decode(CapabilityGrant, raw) for raw in body.get('ceiling', ())),
        custodial_ceiling if body.get('custodial') else parent.ceiling,
    )
    if body.get('session'):
        _, session = get(tx, body['session'], now)
        require(not session.get('revoked'), 'invalid_grant')
        await require_source(tx, session, now, custodial_ceiling=custodial_ceiling)
    return subject


async def require_binding(tx, credential, now, config, *, custodial_ceiling):
    # Revoked protocol families must never fall back to an ordinary token proof,
    # including after restoring a database from before the key-only migration.
    require(not credential.id.startswith(('t_oauth_', 't_browser_')), 'invalid_grant')
    browser = tx.one('SELECT body FROM credential_states WHERE id=?', ('browser:' + credential.id,))
    if browser is not None:
        require(config is not None and config.enabled, 'browser_sessions_disabled')
        source = loads(browser[0])
        require(
            credential.id.startswith('t_session_')
            and credential.source_credential_id == source['parent']
            and credential.subject_id == source['subject']
            and not source.get('custodial')
            and not source.get('login_binding'),
            'invalid_grant',
        )
        await require_source(tx, source, now, custodial_ceiling=custodial_ceiling)
        await require_ceiling(
            tx, credential.ceiling, tuple(decode(CapabilityGrant, raw) for raw in source['ceiling'])
        )
        return
    api = tx.one('SELECT body FROM credential_states WHERE id=?', ('api:' + credential.id,))
    if api is not None:
        source = loads(api[0])
        require(credential.source_credential_id == source['parent'], 'invalid_grant')
        await require_source(tx, source, now, custodial_ceiling=custodial_ceiling)
        await require_ceiling(
            tx, credential.ceiling, (await tx.credential(source['parent'])).ceiling
        )
        return
    require(
        not credential.id.startswith('t_session_') and credential.source_credential_id is None,
        'invalid_grant',
    )


class BrowserSessionService:
    def __init__(self, app):
        self.app = app

    @property
    def config(self):
        return self.app.settings.browser

    def fence(self, tx):
        require(self.config.enabled, 'browser_sessions_disabled')
        self.app.runtime_generation.require_current(tx)
        require_live_authority(tx)
        require(not self.app.executor.recovery_drill_active(), 'recovery_quarantined')

    def rate(self, tx, address):
        now = self.app.clock()
        self.fence(tx)
        tx.execute('DELETE FROM credential_states WHERE expires<=?', (wire(now),), write=True)
        id = state_id('rate', address or 'unknown')
        row = tx.one('SELECT body,expires FROM credential_states WHERE id=?', (id,))
        count = loads(row[0])['count'] if row else 0
        require(count < 120, 'slow_down')
        require(
            tx.one('SELECT COUNT(*) FROM credential_states')[0] < 100000, 'temporarily_unavailable'
        )
        put(
            tx,
            id,
            'rate',
            parse_time(row[1]) if row else now + timedelta(minutes=10),
            {'count': count + 1},
        )

    def pending(self, tx):
        self.fence(tx)
        now = self.app.clock()
        # Never overwrite another pending confirmation on a code collision.
        for _ in range(8):
            code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            id = state_id('user', code)
            if tx.one('SELECT id FROM credential_states WHERE id=?', (id,)) is None:
                break
        else:
            require(False, 'temporarily_unavailable')
        value = secret()
        expiry = now + timedelta(minutes=10)
        body = {
            'kind': 'browser',
            'status': 'pending',
            'poll': state_id('poll', value),
            'interval': 5,
            'next_poll': wire(now),
        }
        put(tx, id, 'pending', expiry, body)
        put(tx, body['poll'], 'poll', expiry, {'user': id})
        return value, code, expiry

    async def source(self, tx, principal):
        require(
            principal.actor == principal.subject and principal.subject is not None,
            'identity_owner_required',
        )
        subject = await tx.subject(principal.subject)
        require(
            subject.kind == 'registered' and not subject.local_only, 'registered_identity_required'
        )
        require(principal.method == 'signature', 'signature_required')
        return {
            'subject': subject.resource_id,
            'parent': principal.credential_id,
            'auth_version': subject.auth_version,
            'custodial': False,
            'ceiling': wire(principal.ceiling),
            'auth_time': int(self.app.clock().timestamp()),
        }

    async def approve(self, tx, principal, code, decision):
        self.fence(tx)
        require(isinstance(code, str) and decision in {'approve', 'deny'}, 'invalid_request')
        code = code.upper().replace('-', '')
        require(len(code) == 8 and all(c in CODE_ALPHABET for c in code), 'invalid_grant')
        id = state_id('user', code)
        _, body = get(tx, id, self.app.clock())
        require(body['status'] == 'pending' and body['kind'] == 'browser', 'invalid_grant')
        source = await self.source(tx, principal)
        body.update(source if decision == 'approve' else {})
        body['status'] = 'approved' if decision == 'approve' else 'denied'
        save(tx, id, body)
        return {'kind': 'browser', 'status': body['status']}

    async def pending_result(self, tx, value):
        now = self.app.clock()
        _, locator = get(tx, state_id('poll', value), now)
        expiry, body = get(tx, locator['user'], now)
        require(body['kind'] == 'browser', 'invalid_grant')
        require(body['status'] != 'consumed', 'invalid_grant')
        if parse_time(body['next_poll']) > now:
            body['interval'] += 5
            body['next_poll'] = wire(now + timedelta(seconds=body['interval']))
            save(tx, locator['user'], body)
            return None, 'slow_down'
        body['next_poll'] = wire(now + timedelta(seconds=body['interval']))
        save(tx, locator['user'], body)
        if body['status'] == 'pending':
            return None, 'authorization_pending'
        if body['status'] == 'denied':
            return None, 'access_denied'
        await require_source(tx, body, now, custodial_ceiling=self.app.temporary_ceiling())
        body['status'] = 'consumed'
        save(tx, locator['user'], body)
        return await self.create_browser_session(tx, body), None

    async def create_browser_session(self, tx, source, *, cookie=None, ttl=None):
        self.fence(tx)
        now = self.app.clock()
        await require_source(tx, source, now, custodial_ceiling=self.app.temporary_ceiling())
        require(ttl is None or type(ttl) is int and 60 <= ttl <= 3600, 'invalid_session_ttl')
        subject = await tx.subject(source['subject'])
        parent = await tx.credential(source['parent'])
        require(
            subject.kind == 'registered'
            and parent.kind == 'signing_key'
            and not source.get('custodial')
            and not source.get('login_binding'),
            'signature_required',
        )
        body = dict(source)
        cookie = cookie or secret()
        session_expiry = now + timedelta(
            seconds=self.config.session_ttl if ttl is None else min(ttl, self.config.session_ttl)
        )
        parent = await tx.credential(body['parent'])
        if parent.expires_at is not None:
            session_expiry = min(session_expiry, parent.expires_at)
        session_id = state_id('session', cookie)
        credential_id = 't_session_' + uuid4().hex
        operations = {
            f'{op.name}@{op.version}'
            for op in self.app.registry.operations()
            if (op.effect == 'read' or (op.name in BROWSER_POST_WRITES and op.version == 1))
            and not op.require_signature
            and not op.anonymous_only
            and not op.name.startswith(('identity.', 'root.', 'system.'))
        }
        wiki_writes = {'content.post_create@1', 'content.post_edit@1'}
        wiki_scope = Scope(resource_id='t_wiki', descendants=True)
        ceiling_items = []
        for raw in body['ceiling']:
            grant = decode(CapabilityGrant, raw)
            general = grant.operations & (operations - wiki_writes)
            if general:
                ceiling_items.append(replace(grant, operations=general))
            shared = grant.operations & operations & wiki_writes
            if shared:
                if await scope_subset(wiki_scope, grant.scope, tx):
                    ceiling_items.append(replace(grant, operations=shared, scope=wiki_scope))
                elif await scope_subset(grant.scope, wiki_scope, tx):
                    ceiling_items.append(replace(grant, operations=shared))
        ceiling = tuple(ceiling_items)
        await tx.save_credential(
            Credential(
                id=credential_id,
                subject_id=body['subject'],
                kind='token',
                verifier=hashlib.sha256(self.browser_secret(cookie)).digest(),
                ceiling=ceiling,
                not_before=now,
                expires_at=session_expiry,
                revoked_at=None,
                source_credential_id=body['parent'],
            ),
            body['auth_version'],
        )
        body['browser_credential'] = credential_id
        put(tx, session_id, 'session', session_expiry, body)
        put(
            tx,
            'browser:' + credential_id,
            'browser',
            session_expiry,
            dict(body, session=session_id),
        )
        return {'cookie': cookie, 'expires': session_expiry}

    async def session(self, tx, cookie):
        self.fence(tx)
        id = state_id('session', cookie)
        expiry, body = get(tx, id, self.app.clock())
        require(not body.get('revoked'), 'invalid_grant')
        await require_source(
            tx, body, self.app.clock(), custodial_ceiling=self.app.temporary_ceiling()
        )
        source = {
            name: body[name]
            for name in ('subject', 'parent', 'auth_version', 'custodial', 'ceiling', 'auth_time')
        }
        return expiry, dict(source, session=id)

    def browser_secret(self, cookie):
        return hmac.digest(
            self.app._token_secret, b'msg-key-browser-v1:' + cookie.encode(), 'sha256'
        )

    async def browser_credentials(self, tx, cookie):
        _, source = await self.session(tx, cookie)
        _, body = get(tx, source['session'], self.app.clock())
        require('browser_credential' in body, 'invalid_grant')
        credential = await tx.credential(body['browser_credential'])
        require(
            credential.revoked_at is None and credential.expires_at > self.app.clock(),
            'invalid_grant',
        )
        await require_binding(
            tx,
            credential,
            self.app.clock(),
            self.config,
            custodial_ceiling=self.app.temporary_ceiling(),
        )
        return source['subject'], credential.id, self.browser_secret(cookie)
