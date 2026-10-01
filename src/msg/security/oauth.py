"""OAuth grants share MSG credentials, ceilings and current authority fences.

Only digests of random authorization codes, refresh tokens and cookies persist.
Token responses never enter operation results, receipts, audit bodies or URLs.
The relational writer fence serializes grant consumption and refresh rotation.
"""

import hashlib
import hmac
import secrets
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from msg.core.codec import b64, canonical, decode, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import CapabilityGrant, Credential
from msg.security.policy import constraints_subset, scope_subset
from msg.security.quarantine import require_live_authority

DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def secret():
    return secrets.token_urlsafe(32)


def state_id(kind, value):
    return kind + ':' + hashlib.sha256(value.encode()).hexdigest()


def put(tx, id, kind, expires, body):
    tx.execute(
        'INSERT INTO oauth_states VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE '
        'SET expires=excluded.expires,body=excluded.body',
        (id, kind, wire(expires), canonical(body).decode()),
        write=True,
    )


def get(tx, id, now, *, expired=False):
    row = tx.one('SELECT expires,body FROM oauth_states WHERE id=?', (id,))
    require(row is not None and (expired or parse_time(row[0]) > now), 'invalid_grant')
    return parse_time(row[0]), loads(row[1])


def save(tx, id, body):
    tx.execute(
        'UPDATE oauth_states SET body=? WHERE id=?', (canonical(body).decode(), id), write=True
    )


def client_for(config, client_id):
    require(config is not None and config.enabled, 'oauth_disabled')
    client = next((c for c in config.clients if c.client_id == client_id), None)
    require(client is not None, 'invalid_client')
    return client


def scopes_for(client, value):
    require(isinstance(value, str) and len(value) <= 200, 'invalid_scope')
    scopes = frozenset(value.split())
    require(bool(scopes) and scopes <= client.scopes, 'invalid_scope')
    return scopes


async def require_ceiling(tx, ceiling, parent):
    # A derived credential is a limit, never a durable copy of authority which
    # its signing source has since lost. Reuse the same containment vocabulary
    # as certificate/delegation validation, including scope and constraints.
    for grant in ceiling:
        require(
            any([
                allowed.capability == grant.capability
                and allowed.version == grant.version
                and grant.operations <= allowed.operations
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
        and not subject.local_only
        and subject.auth_version == body['auth_version']
        and parent.revoked_at is None
        and parent.not_before <= now
        and (parent.expires_at is None or parent.expires_at > now),
        'invalid_grant',
    )
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
    browser = tx.one('SELECT body FROM oauth_states WHERE id=?', ('browser:' + credential.id,))
    if browser is not None:
        require(config is not None and config.enabled, 'oauth_disabled')
        source = loads(browser[0])
        require(
            credential.source_credential_id == source['parent']
            and credential.subject_id == source['subject'],
            'invalid_grant',
        )
        await require_source(tx, source, now, custodial_ceiling=custodial_ceiling)
        await require_ceiling(
            tx, credential.ceiling, tuple(decode(CapabilityGrant, raw) for raw in source['ceiling'])
        )
        return
    api = tx.one('SELECT body FROM oauth_states WHERE id=?', ('api:' + credential.id,))
    if api is not None:
        source = loads(api[0])
        require(credential.source_credential_id == source['parent'], 'invalid_grant')
        await require_source(tx, source, now, custodial_ceiling=custodial_ceiling)
        # API bindings predate OAuth families and store no captured ceiling.
        # Their credential grants remain bounded by the current signing source.
        await require_ceiling(
            tx, credential.ceiling, (await tx.credential(source['parent'])).ceiling
        )
    row = tx.one('SELECT body FROM oauth_states WHERE id=?', ('access:' + credential.id,))
    if row is None:
        require(
            not credential.id.startswith('t_oauth_')
            and not credential.id.startswith('t_browser_')
            and (credential.source_credential_id is None or api is not None),
            'invalid_grant',
        )
        return
    binding = loads(row[0])
    _, family = get(tx, binding['family'], now)
    require(credential.source_credential_id == family['parent'], 'invalid_grant')
    client = client_for(config, family['client_id'])
    require(set(family['scopes']) <= client.scopes and not family.get('revoked'), 'invalid_grant')
    await require_source(tx, family, now, custodial_ceiling=custodial_ceiling)
    await require_ceiling(
        tx, credential.ceiling, tuple(decode(CapabilityGrant, raw) for raw in family['ceiling'])
    )


class OAuthService:
    def __init__(self, app):
        self.app = app

    @property
    def config(self):
        return self.app.settings.oauth

    def fence(self, tx):
        require(self.config.enabled, 'oauth_disabled')
        self.app.runtime_generation.require_current(tx)
        require_live_authority(tx)
        require(not self.app.executor.recovery_drill_active(), 'recovery_quarantined')

    def rate(self, tx, address):
        now = self.app.clock()
        self.fence(tx)
        tx.execute('DELETE FROM oauth_states WHERE expires<=?', (wire(now),), write=True)
        id = state_id('rate', address or 'unknown')
        row = tx.one('SELECT body,expires FROM oauth_states WHERE id=?', (id,))
        count = loads(row[0])['count'] if row else 0
        require(count < 120, 'slow_down')
        require(tx.one('SELECT COUNT(*) FROM oauth_states')[0] < 100000, 'temporarily_unavailable')
        put(
            tx,
            id,
            'rate',
            parse_time(row[1]) if row else now + timedelta(minutes=10),
            {'count': count + 1},
        )

    def pending(
        self,
        tx,
        *,
        kind,
        client_id='msg-cli',
        scope='openid profile msg.read msg.write offline_access',
    ):
        now = self.app.clock()
        client = client_for(self.config, client_id)
        scopes = scopes_for(client, scope)
        code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        value = secret()
        id = state_id('user', code)
        expiry = now + timedelta(minutes=10)
        body = {
            'kind': kind,
            'client_id': client_id,
            'scopes': sorted(scopes),
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
            subject.kind in {'registered', 'custodial'} and not subject.local_only,
            'registered_identity_required',
        )
        require(
            principal.method == 'signature'
            or (subject.kind == 'custodial' and principal.method == 'token'),
            'signature_required',
        )
        parent = principal.credential_id
        if subject.kind == 'custodial':
            row = tx.one(
                'SELECT signing_key_id,status FROM custodial_vault WHERE subject=?',
                (subject.resource_id,),
            )
            require(row is not None and row[1] == 'active', 'custodial_vault_unavailable')
            parent = row[0]
        return {
            'subject': subject.resource_id,
            'parent': parent,
            'auth_version': subject.auth_version,
            'custodial': subject.kind == 'custodial',
            'ceiling': wire(principal.ceiling),
            'auth_time': int(self.app.clock().timestamp()),
        }

    async def approve(self, tx, principal, code, decision):
        self.fence(tx)
        code = code.upper().replace('-', '')
        require(len(code) == 8 and all(c in CODE_ALPHABET for c in code), 'invalid_grant')
        id = state_id('user', code)
        _, body = get(tx, id, self.app.clock())
        require(body['status'] == 'pending', 'invalid_grant')
        client_for(self.config, body['client_id'])
        source = await self.source(tx, principal)
        body.update(source if decision == 'approve' else {})
        body['status'] = 'approved' if decision == 'approve' else 'denied'
        save(tx, id, body)
        return {
            'client_id': body['client_id'],
            'scopes': body['scopes'],
            'kind': body['kind'],
            'status': body['status'],
        }

    async def pending_result(self, tx, value, client_id, kind):
        now = self.app.clock()
        _, locator = get(tx, state_id('poll', value), now)
        expiry, body = get(tx, locator['user'], now)
        require(body['client_id'] == client_id and body['kind'] == kind, 'invalid_grant')
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
        if kind == 'login':
            cookie = secret()
            session_expiry = now + timedelta(seconds=self.config.session_ttl)
            parent = await tx.credential(body['parent'])
            if parent.expires_at is not None:
                session_expiry = min(session_expiry, parent.expires_at)
            session_id = state_id('session', cookie)
            credential_id = 't_browser_' + uuid4().hex
            operations = {
                f'{op.name}@{op.version}'
                for op in self.app.registry.operations()
                if op.effect == 'read'
                and not op.require_signature
                and not op.anonymous_only
                and not op.name.startswith(('identity.', 'root.', 'system.'))
            }
            ceiling = tuple(
                replace(g, operations=g.operations & operations)
                for raw in body['ceiling']
                if (g := decode(CapabilityGrant, raw)).operations & operations
            )
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
            return {'cookie': cookie, 'expires': session_expiry}, None
        return await self.tokens(tx, body), None

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
            self.app._token_secret, b'msg-browser-read-v1:' + cookie.encode(), 'sha256'
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

    def authorization(self, args):
        require(
            set(args)
            <= {
                'response_type',
                'client_id',
                'redirect_uri',
                'scope',
                'state',
                'code_challenge',
                'code_challenge_method',
                'nonce',
            },
            'invalid_request',
        )
        client = client_for(self.config, args.get('client_id'))
        require(args.get('redirect_uri') in client.redirect_uris, 'invalid_redirect_uri')
        require(args.get('response_type') == 'code', 'unsupported_response_type')
        require(args.get('code_challenge_method') == 'S256', 'invalid_request')
        challenge = args.get('code_challenge', '')
        require(
            len(challenge) == 43 and all(c.isalnum() or c in '-_' for c in challenge),
            'invalid_request',
        )
        require(
            isinstance(args.get('state'), str) and 16 <= len(args['state']) <= 512,
            'invalid_request',
        )
        require('nonce' not in args or 16 <= len(args['nonce']) <= 512, 'invalid_request')
        scopes_for(client, args.get('scope', 'openid profile'))
        return client

    async def authorize(self, tx, cookie, args):
        self.authorization(args)
        expiry, source = await self.session(tx, cookie)
        value = secret()
        body = dict(
            source,
            client_id=args['client_id'],
            scopes=sorted(args['scope'].split()),
            redirect_uri=args['redirect_uri'],
            challenge=args['code_challenge'],
            nonce=args.get('nonce'),
        )
        put(
            tx,
            state_id('code', value),
            'code',
            min(expiry, self.app.clock() + timedelta(seconds=60)),
            body,
        )
        return value

    async def exchange(self, tx, args):
        client_for(self.config, args.get('client_id'))
        now = self.app.clock()
        grant = args.get('grant_type')
        if grant == DEVICE_GRANT:
            return await self.pending_result(
                tx, args.get('device_code', ''), args['client_id'], 'device'
            )
        if grant == 'authorization_code':
            id = state_id('code', args.get('code', ''))
            _, body = get(tx, id, now)
            if body.get('consumed') and body['client_id'] == args['client_id']:
                _, family = get(tx, body['family'], now)
                family['revoked'] = True
                save(tx, body['family'], family)
                return None, 'invalid_grant'
            verifier = args.get('code_verifier', '')
            require(
                43 <= len(verifier) <= 128 and all(c.isalnum() or c in '-._~' for c in verifier),
                'invalid_grant',
            )
            require(
                not body.get('consumed')
                and body['client_id'] == args['client_id']
                and body['redirect_uri'] == args.get('redirect_uri')
                and hmac.compare_digest(
                    body['challenge'], b64(hashlib.sha256(verifier.encode()).digest())
                ),
                'invalid_grant',
            )
            await require_source(tx, body, now, custodial_ceiling=self.app.temporary_ceiling())
            body['consumed'] = True
            body['family'] = 'family:' + uuid4().hex
            save(tx, id, body)
            expiry = now + timedelta(seconds=self.config.session_ttl)
            if body.get('session'):
                expiry = min(expiry, get(tx, body['session'], now)[0])
            parent = await tx.credential(body['parent'])
            if parent.expires_at is not None:
                expiry = min(expiry, parent.expires_at)
            put(tx, body['family'], 'family', expiry, body)
            return await self.tokens(tx, body, family_id=body['family'], expiry=expiry), None
        if grant == 'refresh_token':
            id = state_id('refresh', args.get('refresh_token', ''))
            _, old = get(tx, id, now)
            expiry, family = get(tx, old['family'], now)
            require(family['client_id'] == args['client_id'], 'invalid_grant')
            if old.get('used'):
                family['revoked'] = True
                save(tx, old['family'], family)
                return None, 'invalid_grant'
            require(not family.get('revoked'), 'invalid_grant')
            client = client_for(self.config, family['client_id'])
            require(set(family['scopes']) <= client.scopes, 'invalid_scope')
            await require_source(tx, family, now, custodial_ceiling=self.app.temporary_ceiling())
            old['used'] = True
            save(tx, id, old)
            # A refresh does not extend the family's absolute lifetime.
            return await self.tokens(tx, family, family_id=old['family'], expiry=expiry), None
        return None, 'unsupported_grant_type'

    async def tokens(self, tx, source, *, family_id=None, expiry=None):
        now = self.app.clock()
        subject = await require_source(
            tx, source, now, custodial_ceiling=self.app.temporary_ceiling()
        )
        if family_id is None:
            family_id = 'family:' + uuid4().hex
            expiry = now + timedelta(seconds=self.config.session_ttl)
            parent = await tx.credential(source['parent'])
            if parent.expires_at is not None:
                expiry = min(expiry, parent.expires_at)
            if source.get('session'):
                expiry = min(expiry, get(tx, source['session'], now)[0])
            put(tx, family_id, 'family', expiry, source)
        credential_id, value = 't_oauth_' + uuid4().hex, secrets.token_bytes(32)
        operations = {
            f'{op.name}@{op.version}'
            for op in self.app.registry.operations()
            if not op.require_signature
            and not op.name.startswith(('identity.', 'root.', 'system.'))
            and (
                ('msg.read' in source['scopes'] and op.effect == 'read')
                or ('msg.write' in source['scopes'] and op.effect != 'read')
            )
        }
        ceiling = tuple(
            replace(g, operations=g.operations & operations)
            for raw in source['ceiling']
            if (g := decode(CapabilityGrant, raw)).operations & operations
        )
        access_expiry = min(expiry, now + timedelta(seconds=self.config.access_ttl))
        credential = Credential(
            id=credential_id,
            subject_id=subject.resource_id,
            kind='token',
            verifier=hashlib.sha256(value).digest(),
            ceiling=ceiling,
            not_before=now,
            expires_at=access_expiry,
            revoked_at=None,
            source_credential_id=source['parent'],
        )
        await tx.save_credential(credential, subject.auth_version)
        put(tx, 'access:' + credential.id, 'access', access_expiry, {'family': family_id})
        result = {
            'access_token': credential.id + '.' + b64(value),
            'token_type': 'Bearer',
            'expires_in': int((access_expiry - now).total_seconds()),
            'subject_id': subject.resource_id,
            'scope': ' '.join(source['scopes']),
        }
        if 'offline_access' in source['scopes']:
            refresh = secret()
            put(
                tx,
                state_id('refresh', refresh),
                'refresh',
                expiry,
                {'family': family_id, 'used': False},
            )
            result.update(refresh_token=refresh, refresh_expires_at=wire(expiry))
        if 'openid' in source['scopes']:
            result['id_token'] = self.id_token(source, access_expiry)
        return result

    def signing_key(self):
        return Ed25519PrivateKey.from_private_bytes(
            hmac.digest(self.app._token_secret, b'oauth-oidc-signing-v1', 'sha256')
        )

    def jwk(self):
        public = self.signing_key().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return {
            'kty': 'OKP',
            'crv': 'Ed25519',
            'use': 'sig',
            'alg': 'EdDSA',
            'kid': hashlib.sha256(public).hexdigest()[:32],
            'x': b64(public),
        }

    def id_token(self, source, expiry):
        payload = {
            'iss': self.app.settings.service_url,
            'sub': source['subject'],
            'aud': source['client_id'],
            'iat': int(self.app.clock().timestamp()),
            'exp': int(expiry.timestamp()),
            'auth_time': source['auth_time'],
        }
        if source.get('nonce'):
            payload['nonce'] = source['nonce']
        head = b64(canonical({'alg': 'EdDSA', 'typ': 'JWT', 'kid': self.jwk()['kid']}))
        body = b64(canonical(payload))
        value = (head + '.' + body).encode()
        return value.decode() + '.' + b64(self.signing_key().sign(value))

    async def bearer(self, tx, value):
        from msg.core.codec import unb64

        id, sep, token = value.partition('.')
        require(sep and len(value) <= 256, 'invalid_token')
        credential = await tx.credential(id)
        now = self.app.clock()
        require(
            credential.kind == 'token'
            and credential.revoked_at is None
            and credential.not_before <= now
            and credential.expires_at > now
            and hmac.compare_digest(
                credential.verifier, hashlib.sha256(unb64(token, limit=32)).digest()
            ),
            'invalid_token',
        )
        try:
            await require_binding(
                tx, credential, now, self.config, custodial_ceiling=self.app.temporary_ceiling()
            )
        except Failure as exc:
            if exc.code in {'recovery_runtime_stale', 'recovery_quarantined'}:
                raise
            raise Failure('invalid_token') from None
        return credential
