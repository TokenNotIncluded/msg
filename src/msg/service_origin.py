"""Canonical service origins and safe, domain-scoped local namespaces."""

import hashlib
import ipaddress
import re
from urllib.parse import urlsplit

from msg.core.errors import Failure, require


def service_origin(value):
    require(
        isinstance(value, str)
        and bool(value)
        and all(ord(char) > 32 and ord(char) != 127 for char in value)
        and '\\' not in value,
        'invalid_server_url',
    )
    try:
        parsed = urlsplit(value.rstrip('/'))
        require(
            parsed.scheme in {'http', 'https'}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment,
            'invalid_server_url',
        )
        host = parsed.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        port = parsed.port
        require(port is None or 1 <= port <= 65535, 'invalid_server_url')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            require(
                len(host) <= 253
                and all(
                    re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                    for label in host.split('.')
                ),
                'invalid_server_url',
            )
        else:
            host = address.compressed
        authority = f'[{host}]' if ':' in host else host
        if port is not None and port != {'http': 80, 'https': 443}[parsed.scheme]:
            authority += f':{port}'
        return f'{parsed.scheme}://{authority}'
    except ValueError, UnicodeError:
        raise Failure('invalid_server_url') from None


def service_namespace(value):
    parsed = urlsplit(service_origin(value))
    host = parsed.hostname
    name = 'ipv6~' + ipaddress.IPv6Address(host).packed.hex() if ':' in host else host
    if parsed.port is not None:
        name += f'~{parsed.port}'
    if parsed.scheme == 'http':
        name = 'http~' + name
    if len(name) > 240:
        name = name[:150] + '~' + hashlib.sha256(service_origin(value).encode()).hexdigest()
    return name
