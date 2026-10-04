"""Private OAuth context for the MCP adapter; no browser cookie or delegated signer."""

from dataclasses import dataclass, field
from datetime import datetime

from msg.core.codec import loads, unb64
from msg.core.errors import require
from msg.oauth_config import MCP_SCOPE_OPERATIONS, MCP_SCOPES
from msg.security.oauth import OAuthService, get

MCP_PATHS = frozenset({'/-/mcp', '/-/mcp/raw'})
PROTECTED_RESOURCE_PATHS = frozenset({
    '/.well-known/oauth-protected-resource',
    '/.well-known/oauth-protected-resource/-/mcp',
    '/.well-known/oauth-protected-resource/-/mcp/raw',
})


@dataclass(frozen=True, slots=True)
class MCPIdentity:
    # A transport-only value. Never pass this dataclass to wire(), JSON or logs.
    subject: str
    credential_id: str = field(repr=False)
    token: bytes = field(repr=False)
    operations: frozenset[str]
    expires_at: datetime
    session_binding: str = field(repr=False)
    credential_type: str = 'oauth'


def resource_uri(service):
    return service.settings.service_url + '/-/mcp'


def protected_resource_metadata(service):
    return {
        'resource': resource_uri(service),
        'authorization_servers': [service.settings.service_url],
        'scopes_supported': sorted(MCP_SCOPES),
        'bearer_methods_supported': ['header'],
    }


def challenge(service, code=None, scopes=()):
    # Callers cannot insert arbitrary exception or request text into a header.
    require(code in {None, 'invalid_token', 'insufficient_scope'}, 'invalid_challenge')
    require(set(scopes) <= MCP_SCOPES, 'invalid_challenge')
    value = (
        'Bearer resource_metadata="'
        + service.settings.service_url
        + '/.well-known/oauth-protected-resource/-/mcp"'
    )
    if code:
        value += ', error="' + code + '"'
    if scopes:
        value += ', scope="' + ' '.join(sorted(set(scopes))) + '"'
    return value


async def authenticate_mcp(service, value):
    """Validate the OAuth family and current parent authority on every request."""
    oauth = OAuthService(service)
    require(
        isinstance(value, str) and value[:7].lower() == 'bearer ' and len(value) <= 300,
        'invalid_token',
    )
    bearer = value[7:]
    async with service.metadata.transaction(write=False) as tx:
        oauth.fence(tx)
        credential = await oauth.bearer(tx, bearer, resource=resource_uri(service))
        require(credential.id.startswith('t_oauth_'), 'invalid_token')
        _, binding = get(tx, 'access:' + credential.id, service.clock())
        _, family = get(tx, binding['family'], service.clock())
        require(family['subject'] == credential.subject_id, 'invalid_token')
        allowed = frozenset().union(
            *(MCP_SCOPE_OPERATIONS.get(scope, frozenset()) for scope in family['scopes'])
        )
        operations = frozenset().union(*(g.operations for g in credential.ceiling)) & allowed
        return MCPIdentity(
            subject=credential.subject_id,
            credential_id=credential.id,
            token=unb64(bearer.partition('.')[2], limit=32),
            operations=operations,
            expires_at=credential.expires_at,
            session_binding=binding['family'],
        )


def require_unmixed_mcp(message):
    """Bearer-authenticated JSON-RPC cannot carry a second packet proof."""
    if not isinstance(message, dict):
        return
    params = message.get('params', {})
    arguments = params.get('arguments', {}) if isinstance(params, dict) else {}
    packet = arguments.get('packet') if isinstance(arguments, dict) else None
    if isinstance(packet, str):
        packet = loads(packet)
    if isinstance(packet, dict):
        require(packet.get('proof') is None, 'ambiguous_credentials')
    # A bare operation packet is never silently converted into a new bearer request.
    if 'operation' in message:
        require(message.get('proof') is None, 'ambiguous_credentials')
