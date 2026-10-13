"""Native browser sessions are approved by the account's local signing key."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from msg.core.errors import require


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    enabled: bool = True
    session_ttl: int = 3600


def load_browser(data, service):
    require(
        isinstance(data, dict) and set(data) <= {'enabled', 'session_ttl'}, 'invalid_browser_config'
    )
    enabled = data.get('enabled', True)
    ttl = data.get('session_ttl', 3600)
    require(
        type(enabled) is bool and type(ttl) is int and 60 <= ttl <= 86400, 'invalid_browser_config'
    )
    origin = urlsplit(service)
    require(
        not enabled
        or origin.scheme == 'https'
        or origin.hostname in {'localhost', '127.0.0.1', '::1', 'testserver'},
        'secure_channel_required',
    )
    return BrowserConfig(enabled, ttl)
