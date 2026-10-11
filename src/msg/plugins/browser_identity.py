"""Only the existing local signing key can approve a browser session."""

from msg.core.errors import require
from msg.core.models import HandlerOutput
from msg.plugins.schemas import STRING, obj
from msg.security.sessions import CODE_ALPHABET, BrowserSessionService, get, state_id


def install(app, op):
    # Signed certificates retain these operation names. Declarations preserve
    # verification of their original bytes; the executor rejects every call.
    for name, effect in (
        ('identity.oauth_request', 'read'),
        ('identity.oauth_approve', 'write'),
        ('identity.login_complete', 'write'),
        ('identity.login_bind', 'write'),
        ('identity.login_remove', 'write'),
    ):

        @op(name, obj(), effect=effect, enabled=False)
        async def retired(ctx, request, tx):
            require(False, 'operation_disabled')

    @op('identity.session_request', obj({'user_code': STRING}, ('user_code',)), effect='read')
    async def inspect(ctx, request, tx):
        sessions = BrowserSessionService(app)
        sessions.fence(tx)
        code = request.arguments['user_code'].upper().replace('-', '')
        require(len(code) == 8 and all(c in CODE_ALPHABET for c in code), 'invalid_grant')
        _, body = get(tx, state_id('user', code), ctx.now)
        require(body['kind'] == 'browser', 'invalid_grant')
        return HandlerOutput(
            data={
                'kind': 'browser',
                'status': body['status'],
                'service': app.settings.service_url,
                'purpose': 'Approve only the browser sign-in that you started.',
            }
        )

    @op(
        'identity.session_approve',
        obj(
            {'user_code': STRING, 'decision': {'enum': ['approve', 'deny']}},
            ('user_code', 'decision'),
        ),
        signature=True,
    )
    async def approve(ctx, request, tx):
        return HandlerOutput(
            data=await BrowserSessionService(app).approve(
                tx, ctx.principal, request.arguments['user_code'], request.arguments['decision']
            )
        )
