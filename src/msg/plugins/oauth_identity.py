"""OAuth approval requires the existing identity proof, never a device code alone."""

from msg.core.models import HandlerOutput
from msg.plugins.schemas import STRING, obj
from msg.security.oauth import OAuthService, client_for, get, state_id


def install(app, op):
    @op('identity.oauth_request', obj({'user_code': STRING}, ('user_code',)), effect='read')
    async def inspect(ctx, request, tx):
        oauth = OAuthService(app)
        oauth.fence(tx)
        code = request.arguments['user_code'].upper().replace('-', '')
        _, body = get(tx, state_id('user', code), ctx.now)
        client = client_for(oauth.config, body['client_id'])
        return HandlerOutput(
            data={
                'client_id': client.client_id,
                'client_name': client.name,
                'scopes': body['scopes'],
                'kind': body['kind'],
                'status': body['status'],
            }
        )

    @op(
        'identity.oauth_approve',
        obj(
            {'user_code': STRING, 'decision': {'enum': ['approve', 'deny']}},
            ('user_code', 'decision'),
        ),
    )
    async def approve(ctx, request, tx):
        oauth = OAuthService(app)
        return HandlerOutput(
            data=await oauth.approve(
                tx, ctx.principal, request.arguments['user_code'], request.arguments['decision']
            )
        )
