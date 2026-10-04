"""Registered public OAuth clients and bounded login lifetimes."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from msg.core.errors import require

LEGACY_SCOPES = frozenset({'openid', 'profile', 'offline_access', 'msg.read', 'msg.write'})
MCP_SCOPES = frozenset({'msg.mcp.read', 'msg.mcp.message', 'msg.mcp.post'})
SCOPES = LEGACY_SCOPES | MCP_SCOPES

# These are intentionally fixed operation versions, not a registry-wide effect filter.
MCP_SCOPE_OPERATIONS = {
    'msg.mcp.read': frozenset({
        'discovery.get@1',
        'discovery.resolve@1',
        'discovery.read_segment@1',
        'discovery.read_query@3',
        'discovery.lexical_search@5',
        'communication.inbox@1',
        'communication.dm_list@1',
        'communication.conversation_get@1',
        'communication.changes@1',
    }),
    'msg.mcp.message': frozenset({
        'communication.dm_request@3',
        'communication.dm_send@2',
        'communication.dm_accept@2',
        'communication.dm_reject@2',
        'discussion.ack@1',
    }),
    'msg.mcp.post': frozenset({'content.post_create@2', 'discussion.reply@2'}),
}


@dataclass(frozen=True, slots=True)
class OAuthClient:
    client_id: str
    name: str
    redirect_uris: tuple[str, ...] = ()
    scopes: frozenset[str] = LEGACY_SCOPES


CHATGPT_CLIENT = OAuthClient(
    'msg-chatgpt',
    'MSG for ChatGPT',
    ('https://chatgpt.com/connector_platform_oauth_redirect',),
    frozenset({'openid', 'profile', 'offline_access'}) | MCP_SCOPES,
)
DEFAULT_CLIENTS = (OAuthClient('msg-cli', 'MSG CLI'), CHATGPT_CLIENT)


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    enabled: bool = False
    access_ttl: int = 900
    session_ttl: int = 2592000
    clients: tuple[OAuthClient, ...] = DEFAULT_CLIENTS


def load_oauth(data, service):
    require(
        set(data) <= {'enabled', 'access_ttl', 'session_ttl', 'clients'}, 'invalid_oauth_config'
    )
    require(type(data.get('enabled', False)) is bool, 'invalid_oauth_config')
    values = {}
    for name, default, maximum in (('access_ttl', 900, 3600), ('session_ttl', 2592000, 7776000)):
        value = data.get(name, default)
        require(type(value) is int and 60 <= value <= maximum, 'invalid_oauth_config')
        values[name] = value
    clients = list(DEFAULT_CLIENTS)
    raw = data.get('clients', [])
    require(isinstance(raw, list) and len(raw) <= 100, 'invalid_oauth_config')
    for item in raw:
        require(
            isinstance(item, dict)
            and set(item) <= {'client_id', 'name', 'redirect_uris', 'scopes'},
            'invalid_oauth_client',
        )
        cid, name = item.get('client_id'), item.get('name')
        require(
            isinstance(cid, str)
            and 1 <= len(cid) <= 100
            and all(c.isalnum() or c in '-_' for c in cid)
            and isinstance(name, str)
            and 1 <= len(name) <= 100,
            'invalid_oauth_client',
        )
        redirects = item.get('redirect_uris', [])
        scopes = item.get('scopes', sorted(LEGACY_SCOPES))
        require(isinstance(redirects, list) and len(redirects) <= 20, 'invalid_oauth_client')
        require(
            isinstance(scopes, list)
            and all(isinstance(s, str) for s in scopes)
            and set(scopes) <= SCOPES,
            'invalid_oauth_client',
        )
        for uri in redirects:
            require(isinstance(uri, str) and len(uri) <= 2000, 'invalid_redirect_uri')
            try:
                parsed = urlsplit(uri)
                valid = (
                    parsed.hostname
                    and not parsed.username
                    and not parsed.password
                    and not parsed.fragment
                    and not parsed.query
                    and (
                        parsed.scheme == 'https'
                        or (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', '::1'})
                    )
                    and not any(ord(c) <= 32 for c in uri)
                    and '\\' not in uri
                )
                valid = valid and (parsed.port is None or parsed.port > 0)
            except ValueError:
                valid = False
            require(valid, 'invalid_redirect_uri')
        clients.append(OAuthClient(cid, name, tuple(redirects), frozenset(scopes)))
    require(len({c.client_id for c in clients}) == len(clients), 'duplicate_oauth_client')
    require(
        not data.get('enabled', False)
        or urlsplit(service).scheme == 'https'
        or urlsplit(service).hostname in {'localhost', '127.0.0.1', '::1', 'testserver'},
        'secure_channel_required',
    )
    return OAuthConfig(data.get('enabled', False), clients=tuple(clients), **values)
