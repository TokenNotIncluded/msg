"""Verified external factors enter exact, audited executor transactions."""

import hashlib
import hmac
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import timedelta
from uuid import uuid4

from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.models import (
    AuditEvent,
    CapabilityGrant,
    Credential,
    Event,
    HandlerOutput,
    Principal,
    ResourceRef,
    Scope,
)
from msg.core.requests import request_for
from msg.login_config import REGISTER_PROVIDERS, LoginConfig
from msg.security.age_keys import encryption_key_id, generate_age_key, public_from_recipient
from msg.security.crypto import Ed25519Signer
from msg.security.oauth import (
    OAuthService,
    get,
    put,
    require_binding,
    require_source,
    save,
    state_id,
)
from msg.security.policy import scope_contains
from msg.security.vault import store_keys

LOGIN_ACTIONS = {
    'complete': 'identity.login_complete',
    'bind': 'identity.login_bind',
    'remove': 'identity.login_remove',
}
_LOGIN = ContextVar('msg_verified_login', default=None)
_SOURCE_FIELDS = ('subject', 'parent', 'auth_version', 'custodial', 'ceiling', 'auth_time')


@dataclass(frozen=True, slots=True)
class VerifiedLogin:
    provider: str
    sub: str = field(repr=False)

    def __post_init__(self):
        require(
            isinstance(self.provider, str)
            and 1 <= len(self.provider) <= 32
            and self.provider.isascii()
            and self.provider.replace('_', '').isalnum()
            and self.provider == self.provider.lower()
            and isinstance(self.sub, str)
            and 0 < len(self.sub.encode()) <= 2048,
            'invalid_verified_login',
        )

    @property
    def subject_hash(self):
        return hashlib.sha256(
            canonical({
                'domain': 'msg-login-subject-v1',
                'provider': self.provider,
                'sub': self.sub,
            })
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class Binding:
    id: str
    provider: str
    subject_hash: str = field(repr=False)
    subject: str
    parent: str
    auth_version: int
    custodial: bool
    ceiling: tuple = field(repr=False)
    source: dict = field(repr=False)
    provider_data: dict = field(repr=False)
    generation: int
    revoked_at: str | None


@dataclass(slots=True)
class LoginCompletion:
    result: object
    cookie: str = field(default='', repr=False)
    expires: object = None


@dataclass(frozen=True, slots=True)
class LoginApproval:
    intent_id: str
    user_code: str
    poll_cookie: str = field(repr=False)
    expires: object


@dataclass(frozen=True, slots=True)
class _LoginContext:
    app: object = field(repr=False)
    verified: VerifiedLogin | None = field(repr=False)
    intent_id: str
    operation: str
    request_id: str
    payload_digest: str
    cookie: str = field(repr=False)


@contextmanager
def trusted_login(app, verified, *, intent_id, request, cookie=''):
    require(verified is None or type(verified) is VerifiedLogin, 'invalid_verified_login')
    value = _LoginContext(
        app,
        verified,
        intent_id,
        request.operation,
        request.request_id,
        request.payload_digest,
        cookie,
    )
    token = _LOGIN.set(value)
    try:
        yield
    finally:
        _LOGIN.reset(token)


def config_for(app):
    config = getattr(app.settings, 'login', LoginConfig())
    require(config.enabled, 'login_disabled')
    return config


def require_provider(app, verified):
    provider = config_for(app).provider(verified.provider)
    require(provider is not None and provider.enabled, 'login_provider_disabled')


def lookup_binding(tx, provider, sub, *, include_revoked=False):
    verified = VerifiedLogin(provider, sub)
    row = tx.one(
        'SELECT id,provider,external_subject_hash,subject,source_credential_id,auth_version,'
        'custodial,ceiling,source_data,provider_data,generation,revoked_at '
        'FROM login_bindings WHERE provider=? AND external_subject_hash=?',
        (provider, verified.subject_hash),
    )
    if row is None or row[11] is not None and not include_revoked:
        return None
    return Binding(
        *row[:6],
        bool(row[6]),
        tuple(decode(CapabilityGrant, raw) for raw in loads(row[7])),
        loads(row[8]),
        loads(row[9]),
        row[10],
        row[11],
    )


def binding_by_id(tx, binding_id):
    full = tx.one(
        'SELECT id,provider,external_subject_hash,subject,source_credential_id,auth_version,'
        'custodial,ceiling,source_data,provider_data,generation,revoked_at '
        'FROM login_bindings WHERE id=?',
        (binding_id,),
    )
    require(full is not None, 'login_binding_not_found')
    return Binding(
        *full[:6],
        bool(full[6]),
        tuple(decode(CapabilityGrant, raw) for raw in loads(full[7])),
        loads(full[8]),
        loads(full[9]),
        full[10],
        full[11],
    )


async def list_bindings(tx, subject):
    return [
        {'binding_id': binding_id, 'provider': provider}
        for binding_id, provider in tx.rows(
            'SELECT id,provider FROM login_bindings WHERE subject=? AND revoked_at IS NULL '
            'ORDER BY provider,id',
            (subject,),
        )
    ]


def source_for_binding(binding, now):
    return dict(
        binding.source,
        auth_time=int(now.timestamp()),
        login_binding=binding.id,
        login_generation=binding.generation,
    )


def _new_subject(app, verified):
    raw = hmac.digest(
        app._token_secret,
        b'msg-login-account-v1:' + verified.subject_hash.encode(),
        'sha256',
    )
    return 'u_cust_' + raw.hex()[:32]


def _arguments(intent_id, handle):
    return {'intent_id': intent_id, **({'handle': handle} if handle is not None else {})}


def _request(app, action, intent_id, request_id, *, handle=None):
    return request_for(
        LOGIN_ACTIONS[action],
        _arguments(intent_id, handle),
        app.settings.service_url,
        request_id=request_id,
        expires_at=app.clock() + timedelta(minutes=3),
    )


async def create_login_intent(
    app, tx, verified, *, action, request_id, owner=None, binding_id=None, handle=None
):
    config_for(app)
    oauth = OAuthService(app)
    oauth.fence(tx)
    require(action in LOGIN_ACTIONS, 'invalid_login_action')
    require(action == 'remove' or type(verified) is VerifiedLogin, 'invalid_verified_login')
    if verified is not None:
        require_provider(app, verified)
    require(action == 'complete' or owner is not None, 'login_owner_required')
    locator = {
        'action': action,
        'request_id': request_id,
        'owner': owner,
        'binding_id': binding_id,
        'provider': verified.provider if verified else None,
        'subject_hash': verified.subject_hash if verified else None,
    }
    intent_id = 'login-intent:' + hmac.digest(app._token_secret, canonical(locator), 'sha256').hex()
    request = _request(app, action, intent_id, request_id, handle=handle)
    old = tx.one('SELECT body FROM oauth_states WHERE id=?', (intent_id,))
    if old is not None:
        _, previous = get(tx, intent_id, app.clock())
        require(previous['payload_digest'] == request.payload_digest, 'request_id_conflict')
        return intent_id, previous
    binding = (
        binding_by_id(tx, binding_id)
        if action == 'remove'
        else lookup_binding(tx, verified.provider, verified.sub, include_revoked=True)
    )
    if action == 'complete':
        require(binding is None or binding.revoked_at is None, 'login_binding_revoked')
        require(
            binding is not None or verified.provider in REGISTER_PROVIDERS, 'login_binding_required'
        )
        require(binding is not None or bool(handle), 'login_registration_required')
        owner = binding.subject if binding else _new_subject(app, verified)
    if action == 'remove':
        require(binding.subject == owner and binding.revoked_at is None, 'login_owner_required')
    body = dict(
        locator,
        subject=owner,
        binding_id=binding.id if binding else None,
        binding_generation=binding.generation if binding else None,
        payload_digest=request.payload_digest,
        status='approved' if action == 'complete' else 'pending',
    )
    put(tx, intent_id, 'login_intent', app.clock() + timedelta(minutes=5), body)
    return intent_id, body


async def request_login_approval(
    app, verified, owner, *, action='bind', binding_id=None, request_id
):
    async with app.metadata.transaction(write=True) as tx:
        intent_id, intent = await create_login_intent(
            app,
            tx,
            verified,
            action=action,
            owner=owner,
            binding_id=binding_id,
            request_id=request_id,
        )
        require(intent['status'] == 'pending', 'invalid_grant')
        oauth = OAuthService(app)
        poll, user_code, expiry = oauth.pending(tx, kind='login_' + action)
        _, body = get(tx, state_id('user', user_code), app.clock())
        body.update(login_intent=intent_id, expected_subject=owner)
        save(tx, state_id('user', user_code), body)
        return LoginApproval(
            intent_id, user_code, poll, min(expiry, app.clock() + timedelta(minutes=5))
        )


async def approve_login_intent(app, tx, principal, intent_id):
    _, intent = get(tx, intent_id, app.clock())
    require(
        intent['status'] == 'pending' and intent['action'] in {'bind', 'remove'}, 'invalid_grant'
    )
    require(principal.actor == principal.subject == intent['subject'], 'login_owner_required')
    require(
        any([
            'identity.oauth_approve@1' in grant.operations
            and await scope_contains(grant.scope, principal.subject, tx)
            for grant in principal.ceiling
        ]),
        'credential_ceiling',
    )
    source = await OAuthService(app).source(tx, principal)
    intent.update(source=source, status='approved')
    save(tx, intent_id, intent)


async def approve_browser_login_intent(app, tx, session_cookie, intent_id):
    oauth = OAuthService(app)
    oauth.fence(tx)
    require(isinstance(session_cookie, str) and bool(session_cookie), 'invalid_grant')
    _, source = await oauth.session(tx, session_cookie)
    _, session = get(tx, source['session'], app.clock())
    if session.get('login_management') is not True:
        return False
    require(
        type(session.get('auth_time')) is int
        and 0 <= int(app.clock().timestamp()) - session['auth_time'] <= 300,
        'login_management_expired',
    )
    require(session.get('login_binding') is not None, 'invalid_grant')
    credential = await tx.credential(session.get('browser_credential'))
    require(
        credential.subject_id == source['subject']
        and credential.revoked_at is None
        and credential.not_before <= app.clock()
        and credential.expires_at is not None
        and credential.expires_at > app.clock(),
        'invalid_grant',
    )
    await require_binding(
        tx, credential, app.clock(), oauth.config, custodial_ceiling=app.temporary_ceiling()
    )
    binding = binding_by_id(tx, session['login_binding'])
    require(
        binding.subject == source['subject']
        and binding.revoked_at is None
        and binding.generation == session.get('login_generation'),
        'invalid_grant',
    )
    require(
        any([
            'identity.oauth_approve@1' in grant.operations
            and await scope_contains(grant.scope, source['subject'], tx)
            for grant in binding.ceiling
        ]),
        'credential_ceiling',
    )
    _, intent = get(tx, intent_id, app.clock())
    require(
        intent['action'] in {'bind', 'remove'} and intent['subject'] == source['subject'],
        'login_owner_required',
    )
    # HTTP 入口先验证同源 CSRF；只有刚核验过绑定因子的专用会话能批准这一个 intent。
    if intent['status'] == 'pending':
        intent.update(source=dict(source), status='approved')
        save(tx, intent_id, intent)
    else:
        require(
            intent['status'] in {'approved', 'consumed'}
            and canonical(intent.get('source')) == canonical(source),
            'login_intent_mismatch',
        )
    return True


async def _intent(app, tx, request):
    context = _LOGIN.get()
    require(
        context is not None
        and context.intent_id == request.arguments['intent_id']
        and context.operation == request.operation
        and context.request_id == request.request_id
        and context.payload_digest == request.payload_digest
        and request.contract_version == 1
        and request.proof is None
        and request.subject is None,
        'verified_login_required',
    )
    _, intent = get(tx, context.intent_id, app.clock())
    require(
        request.operation == LOGIN_ACTIONS.get(intent['action'])
        and intent['request_id'] == request.request_id
        and intent['payload_digest'] == request.payload_digest
        and intent['status'] in {'approved', 'consumed'},
        'login_intent_mismatch',
    )
    if intent['action'] != 'remove':
        verified = context.verified
        require(
            verified is not None
            and verified.provider == intent['provider']
            and verified.subject_hash == intent['subject_hash'],
            'login_intent_mismatch',
        )
        require_provider(app, verified)
    else:
        require(context.verified is None, 'login_intent_mismatch')
        config_for(app)
    if intent['action'] in {'bind', 'remove'}:
        await require_source(
            tx, intent['source'], app.clock(), custodial_ceiling=app.temporary_ceiling()
        )
        require(intent['source']['subject'] == intent['subject'], 'login_owner_required')
    return context, intent


async def authenticate_login(authenticator, request, tx):
    context = _LOGIN.get()
    require(
        context is not None and context.app.authenticator is authenticator,
        'verified_login_required',
    )
    app = context.app
    _, intent = await _intent(app, tx, request)
    subject = intent['subject']
    if intent['action'] == 'complete':
        binding = lookup_binding(
            tx, _LOGIN.get().verified.provider, _LOGIN.get().verified.sub, include_revoked=True
        )
        if binding:
            require(
                binding.subject == subject and binding.revoked_at is None, 'login_binding_revoked'
            )
            require(
                intent['binding_generation'] is None
                or intent['binding_generation'] == binding.generation,
                'login_binding_changed',
            )
            await require_source(
                tx,
                source_for_binding(binding, app.clock()),
                app.clock(),
                custodial_ceiling=app.temporary_ceiling(),
            )
        else:
            require(_LOGIN.get().verified.provider in REGISTER_PROVIDERS, 'login_binding_required')
    # 此 principal 只执行本次已核验事务，不能作为可保存的 MSG token 使用。
    return Principal(
        actor=subject,
        subject=subject,
        credential_id=None,
        method='token',
        certificates=(),
        ceiling=(
            CapabilityGrant(
                capability='identity.basic',
                version=1,
                scope=Scope(resource_id=subject),
                operations=frozenset({request.operation + '@1'}),
                constraints={},
            ),
        ),
    )


async def _create_custodial(app, tx, ctx, verified, handle):
    from msg.plugins.identity import make_user

    require(tx.setting('runtime_config', {}).get('accept_writes', True), 'writes_paused')
    signer = Ed25519Signer.generate()
    age_identity, recipient = generate_age_key()
    public = public_from_recipient(recipient)
    age_id = encryption_key_id(public)
    subject = ctx.principal.subject
    await make_user(app, tx, ctx, subject, handle or 'user-' + subject[-16:], 'custodial')
    await tx.save_credential(
        Credential(
            id=signer.key_id,
            subject_id=subject,
            kind='signing_key',
            verifier=signer.public_key,
            ceiling=(),
            not_before=ctx.now,
            expires_at=None,
            revoked_at=None,
        ),
        0,
    )
    tx.execute(
        'INSERT INTO identity_keys VALUES (?,?,?,?,?,?)',
        (signer.key_id, subject, b64(signer.public_key), wire(ctx.now), None, 1),
        write=True,
    )
    tx.execute(
        'INSERT INTO encryption_subkeys VALUES (?,?,?,?,?,?,?)',
        (age_id, subject, recipient, b64(public), wire(ctx.now), None, 1),
        write=True,
    )
    store_keys(app, tx, subject, signer, age_identity, age_id, ctx.now)
    return {
        'subject': subject,
        'parent': signer.key_id,
        'auth_version': 0,
        'custodial': True,
        'ceiling': wire(app.temporary_ceiling()),
        'auth_time': int(ctx.now.timestamp()),
    }


def _write_binding(tx, verified, source, now, *, previous=None, provider_data=None):
    binding_id = previous.id if previous else 'lb_' + uuid4().hex
    generation = previous.generation + 1 if previous else 0
    base_source = {key: source[key] for key in _SOURCE_FIELDS}
    values = (
        verified.provider,
        verified.subject_hash,
        source['subject'],
        source['parent'],
        source['auth_version'],
        int(source['custodial']),
        canonical(source['ceiling']).decode(),
        canonical(base_source).decode(),
        canonical(provider_data or {}).decode(),
        generation,
        wire(now),
    )
    if previous:
        tx.execute(
            'UPDATE login_bindings SET provider=?,external_subject_hash=?,subject=?,'
            'source_credential_id=?,auth_version=?,custodial=?,ceiling=?,source_data=?,'
            'provider_data=?,generation=?,updated_at=?,revoked_at=NULL WHERE id=?',
            (*values, binding_id),
            write=True,
        )
    else:
        tx.execute(
            'INSERT INTO login_bindings (id,provider,external_subject_hash,subject,'
            'source_credential_id,auth_version,custodial,ceiling,source_data,provider_data,'
            'generation,updated_at,created_at,revoked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
            (binding_id, *values, wire(now)),
            write=True,
        )
    return binding_by_id(tx, binding_id)


async def _audit(tx, ctx, request, binding, *, before=None):
    await tx.append_audit(
        AuditEvent(
            event=Event(
                id='a_' + uuid4().hex,
                type=request.operation,
                time=ctx.now,
                request_id=request.request_id,
                actor=ctx.principal.actor,
                subject=binding.subject,
                resources=(ResourceRef(id=binding.subject),),
                data={'binding_id': binding.id, 'provider': binding.provider},
            ),
            authority=(),
            before_digest=digest(before),
            after_digest=digest({
                'id': binding.id,
                'subject': binding.subject,
                'provider': binding.provider,
                'generation': binding.generation,
                'revoked_at': binding.revoked_at,
                'source': binding.source,
            }),
            previous_digest=None,
            entry_digest='',
            result='committed',
        )
    )


async def apply_login(app, tx, ctx, request):
    context, intent = await _intent(app, tx, request)
    verified = context.verified
    action = intent['action']
    if action == 'remove':
        binding = binding_by_id(tx, intent['binding_id'])
        require(binding.subject == ctx.principal.subject, 'login_owner_required')
        require(
            binding.revoked_at is None and binding.generation == intent['binding_generation'],
            'login_binding_changed',
        )
        tx.execute(
            'UPDATE login_bindings SET revoked_at=?,generation=generation+1,updated_at=? WHERE id=?',
            (wire(ctx.now), wire(ctx.now), binding.id),
            write=True,
        )
        before = {'generation': binding.generation, 'revoked_at': binding.revoked_at}
        binding = replace(binding, generation=binding.generation + 1, revoked_at=wire(ctx.now))
        await _audit(tx, ctx, request, binding, before=before)
        intent['status'] = 'consumed'
        save(tx, context.intent_id, intent)
        return HandlerOutput(
            data={'subject_id': binding.subject, 'binding_id': binding.id, 'removed': True}
        )
    binding = lookup_binding(tx, verified.provider, verified.sub, include_revoked=True)
    if action == 'bind':
        source = intent['source']
        require(binding is None or binding.subject == source['subject'], 'login_binding_conflict')
        provider_data = {}
        if verified.provider == 'passkey':
            require(binding is None or binding.revoked_at is not None, 'login_binding_exists')
            from msg.security.passkey import finalize_registration

            provider_data = finalize_registration(tx, verified, source['subject'], now=ctx.now)
        binding = _write_binding(
            tx, verified, source, ctx.now, previous=binding, provider_data=provider_data
        )
    elif binding is None:
        require(verified.provider in REGISTER_PROVIDERS, 'login_binding_required')
        source = await _create_custodial(app, tx, ctx, verified, request.arguments.get('handle'))
        binding = _write_binding(tx, verified, source, ctx.now)
    else:
        require(
            binding.revoked_at is None and binding.subject == ctx.principal.subject,
            'login_binding_revoked',
        )
        if verified.provider == 'passkey':
            from msg.security.passkey import finalize_authentication

            provider_data = finalize_authentication(
                tx, verified, binding.subject, binding.provider_data, now=ctx.now
            )
            tx.execute(
                'UPDATE login_bindings SET provider_data=?,updated_at=? WHERE id=?',
                (canonical(provider_data).decode(), wire(ctx.now), binding.id),
                write=True,
            )
    source = source_for_binding(binding, ctx.now)
    await OAuthService(app).create_browser_session(
        tx,
        dict(source, scopes=['openid', 'profile', 'msg.read', 'msg.write'], login_management=True),
        cookie=context.cookie,
        ttl=config_for(app).session_ttl,
    )
    await _audit(tx, ctx, request, binding)
    intent['status'] = 'consumed'
    save(tx, context.intent_id, intent)
    return HandlerOutput(
        resources=(ResourceRef(id=binding.subject),),
        data={
            'subject_id': binding.subject,
            'binding_id': binding.id,
            'kind': (await tx.subject(binding.subject)).kind,
            'provider': binding.provider,
        },
    )


async def _execute_login(app, verified, *, action, request_id, intent_id=None, handle=None):
    if intent_id is None:
        require(action == 'complete', 'login_approval_required')
        async with app.metadata.transaction(write=True) as tx:
            intent_id, _ = await create_login_intent(
                app, tx, verified, action=action, request_id=request_id, handle=handle
            )
    request = _request(app, action, intent_id, request_id, handle=handle)
    cookie = (
        b64(
            hmac.digest(
                app._token_secret,
                b'msg-login-browser-v1:'
                + intent_id.encode()
                + b':'
                + request.payload_digest.encode(),
                'sha256',
            )
        )
        if action != 'remove'
        else ''
    )
    with trusted_login(app, verified, intent_id=intent_id, request=request, cookie=cookie):
        result = await app.executor.execute(request)
    completion = LoginCompletion(result)
    if result.status == 'ok' and cookie:
        async with app.metadata.transaction(write=False) as tx:
            expiry, source = await OAuthService(app).session(tx, cookie)
            require(source['subject'] == result.subject, 'login_owner_required')
        completion.cookie, completion.expires = cookie, expiry
    return completion


async def complete_login(app, verified, *, request_id, handle=None, intent_id=None):
    return await _execute_login(
        app, verified, action='complete', request_id=request_id, handle=handle, intent_id=intent_id
    )


async def bind_login(
    app, verified, source=None, *, request_id, intent_id=None, session_cookie=None
):
    if source is not None:
        async with app.metadata.transaction(write=True) as tx:
            prepared, _ = await create_login_intent(
                app, tx, verified, action='bind', request_id=request_id, owner=source.get('subject')
            )
            require(intent_id is None or prepared == intent_id, 'login_intent_mismatch')
            intent_id = prepared
            require(session_cookie is not None, 'login_approval_required')
            require(
                await approve_browser_login_intent(app, tx, session_cookie, intent_id),
                'login_approval_required',
            )
    return await _execute_login(
        app, verified, action='bind', request_id=request_id, intent_id=intent_id
    )


async def remove_login(
    app, source=None, binding_id=None, *, request_id, intent_id=None, session_cookie=None
):
    if source is not None:
        async with app.metadata.transaction(write=True) as tx:
            prepared, _ = await create_login_intent(
                app,
                tx,
                None,
                action='remove',
                request_id=request_id,
                owner=source.get('subject'),
                binding_id=binding_id,
            )
            require(intent_id is None or prepared == intent_id, 'login_intent_mismatch')
            intent_id = prepared
            require(session_cookie is not None, 'login_approval_required')
            require(
                await approve_browser_login_intent(app, tx, session_cookie, intent_id),
                'login_approval_required',
            )
    return (
        await _execute_login(app, None, action='remove', request_id=request_id, intent_id=intent_id)
    ).result
