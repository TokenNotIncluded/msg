"""Server-qualified resource addresses without changing signed ResourceRefs."""

import re
from urllib.parse import urlsplit

from msg.core.codec import wire
from msg.core.errors import Failure, require

IDENTIFIER = r'[A-Za-z0-9_.:-]{1,160}'


def service_origin(service):
    require(isinstance(service, str), 'invalid_resource_address')
    try:
        parsed = urlsplit(service)
        port = parsed.port
    except ValueError as exc:
        raise Failure('invalid_resource_address') from exc
    require(
        parsed.scheme in {'http', 'https'}
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {'', '/'}
        and not parsed.query
        and not parsed.fragment
        and not any(c.isspace() or ord(c) < 32 for c in service),
        'invalid_resource_address',
    )
    host = parsed.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    if port is not None and port != {'http': 80, 'https': 443}[parsed.scheme]:
        host += ':' + str(port)
    return parsed.scheme + '://' + host


def resource_address(service, ref):
    require(re.fullmatch(IDENTIFIER, ref.id) is not None, 'invalid_resource_id')
    require(
        ref.revision is None or re.fullmatch(IDENTIFIER, ref.revision) is not None,
        'invalid_revision_id',
    )
    origin = service_origin(service)
    path = '/_r/' + ref.id
    path += '/rev/' + ref.revision if ref.revision is not None else '/json'
    return {'service': origin, 'ref': wire(ref), 'url': origin + path}


def parse_address(value, service):
    """Return a local lookup and optional pinned version; never fetch remote URLs."""
    require(
        isinstance(value, str)
        and 0 < len(value) <= 2048
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        and '\\' not in value,
        'invalid_resource_address',
    )
    absolute = '://' in value
    if absolute:
        require(not any(c.isspace() for c in value), 'invalid_resource_address')
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise Failure('invalid_resource_address') from exc
        require(not parsed.query and not parsed.fragment, 'invalid_resource_address')
        origin = service_origin(parsed.scheme + '://' + parsed.netloc)
        require(origin == service_origin(service), 'remote_resource_address')
        value = parsed.path
        # Absolute references have exactly the published, stable spelling.
        require(value.startswith(('/_r/', '/*')), 'invalid_resource_address')
    if absolute or value.startswith(('/_r/', '/*')):
        require(
            '?' not in value and '#' not in value and '%' not in value, 'invalid_resource_address'
        )
    match = re.fullmatch(
        rf'(?:/_r/|/\*)({IDENTIFIER})(?:/(?:json|meta|raw|history)|/rev/({IDENTIFIER}))?',
        value,
    )
    if match:
        return match[1], match[2]
    require(not value.startswith(('/_r/', '/*')), 'invalid_resource_address')
    return value, None
