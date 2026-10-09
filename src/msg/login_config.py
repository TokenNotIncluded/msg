"""Optional login providers and an explicit account-registration policy."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from msg.core.errors import require

OAUTH_PROVIDERS = frozenset({'google', 'github', 'chatgpt', 'agentid'})
PROVIDERS = OAUTH_PROVIDERS | {'email', 'passkey'}
REGISTER_PROVIDERS = frozenset({'google', 'email'})


@dataclass(frozen=True, slots=True)
class LoginProvider:
    name: str
    enabled: bool = False
    client_id: str = ''
    credential_file: str = ''
    token_auth_method: str = 'none'
    transport: str = 'sequenzy'
    sender: str = ''


@dataclass(frozen=True, slots=True)
class LoginConfig:
    enabled: bool = False
    registration: str = 'legacy'
    session_ttl: int = 900
    providers: tuple[LoginProvider, ...] = tuple(LoginProvider(name) for name in sorted(PROVIDERS))

    def provider(self, name):
        return next((item for item in self.providers if item.name == name), None)


def load_login(data, service):
    require(
        isinstance(data, dict)
        and set(data) <= {'enabled', 'registration', 'session_ttl', 'providers'},
        'invalid_login_config',
    )
    enabled = data.get('enabled', False)
    require(type(enabled) is bool, 'invalid_login_config')
    registration = data.get('registration', 'provider_only' if enabled else 'legacy')
    require(
        isinstance(registration, str) and registration in {'legacy', 'provider_only'},
        'invalid_login_config',
    )
    ttl = data.get('session_ttl', 900)
    require(type(ttl) is int and 60 <= ttl <= 3600, 'invalid_login_config')
    raw = data.get('providers', {})
    require(isinstance(raw, dict) and set(raw) <= PROVIDERS, 'invalid_login_provider')
    providers = []
    for name in sorted(PROVIDERS):
        item = raw.get(name, {})
        allowed = (
            {'enabled', 'client_id', 'credential_file', 'token_auth_method'}
            if name in OAUTH_PROVIDERS
            else {'enabled', 'transport', 'credential_file', 'sender'}
            if name == 'email'
            else {'enabled'}
        )
        require(isinstance(item, dict) and set(item) <= allowed, 'invalid_login_provider')
        active = item.get('enabled', bool(item))
        require(type(active) is bool, 'invalid_login_provider')
        client_id, credential_file = item.get('client_id', ''), item.get('credential_file', '')
        token_auth_method = item.get(
            'token_auth_method',
            {
                'google': 'client_secret_post',
                'github': 'client_secret_post',
                'agentid': 'client_secret_basic',
            }.get(name, 'none'),
        )
        transport, sender = item.get('transport', 'sequenzy'), item.get('sender', '')
        require(
            isinstance(client_id, str)
            and len(client_id) <= 512
            and not any(ord(c) < 33 for c in client_id)
            and isinstance(credential_file, str)
            and len(credential_file) <= 4096
            and (not credential_file or Path(credential_file).is_absolute())
            and not any(ord(c) < 32 for c in credential_file),
            'invalid_login_provider',
        )
        require(
            isinstance(token_auth_method, str)
            and token_auth_method in {'none', 'client_secret_basic', 'client_secret_post'}
            and isinstance(transport, str)
            and transport in {'sequenzy', 'smtp'}
            and isinstance(sender, str)
            and len(sender) <= 512
            and not any(ord(c) < 32 for c in sender)
            and (name != 'chatgpt' or not active or client_id.startswith('oaiapp_'))
            and (
                name != 'agentid'
                or not active
                or (
                    client_id
                    and client_id.isascii()
                    and credential_file
                    and token_auth_method in {'client_secret_basic', 'client_secret_post'}
                )
            ),
            'invalid_login_provider',
        )
        providers.append(
            LoginProvider(
                name, active, client_id, credential_file, token_auth_method, transport, sender
            )
        )
    parsed = urlsplit(service)
    require(
        not enabled
        or parsed.scheme == 'https'
        or parsed.hostname in {'localhost', '127.0.0.1', '::1', 'testserver'},
        'secure_channel_required',
    )
    return LoginConfig(enabled, registration, ttl, tuple(providers))


def registration_closed(settings):
    config = getattr(settings, 'login', LoginConfig())
    return config.enabled and config.registration == 'provider_only'
