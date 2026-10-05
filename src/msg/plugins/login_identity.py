"""Business parameters never contain external identity assertions or secrets."""

from msg.plugins.schemas import STRING, obj
from msg.security.login import apply_login


def install(app, op):
    @op('identity.login_complete', obj({'intent_id': STRING, 'handle': STRING}, ('intent_id',)))
    @op('identity.login_bind', obj({'intent_id': STRING}, ('intent_id',)))
    @op('identity.login_remove', obj({'intent_id': STRING}, ('intent_id',)))
    async def login(ctx, request, tx):
        return await apply_login(app, tx, ctx, request)
