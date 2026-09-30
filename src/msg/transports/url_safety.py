"""Bounded URL inspection shared by HTTP, PathGET and clients.

Decoding here is for rejection only. It never produces a rewritten route or
business arguments. Ordinary data (including percent literals and quoted /)
still reaches its original parser. Only explicit credential field names and
legacy credential slots are classified; arbitrary text is not a secret scanner.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import SplitResult, unquote_to_bytes, urlsplit

from msg.core.errors import Failure, require

MAX_DECODE_LAYERS = 8
_SECRET_NAMES = frozenset({
    'token',
    'accesstoken',
    'refreshtoken',
    'recoverysecret',
    'newrecoverysecret',
    'privatekey',
    'secret',
    'clientsecret',
    'bootstrapclaim',
    'password',
    'apikey',
    'authorization',
})
_BAD_ESCAPE = re.compile(rb'%(?![0-9a-fA-F]{2})')
_CONTROL = re.compile(rb'[\x00-\x20\x7f]')


def _layers(raw: bytes):
    """At most eight decoding passes; never expose the rejected input."""
    current = raw
    for _ in range(MAX_DECODE_LAYERS + 1):
        yield current
        decoded = unquote_to_bytes(current)
        if decoded == current:
            return
        current = decoded
    require(False, 'invalid_path')


def is_secret_field(name: str | bytes) -> bool:
    raw = name.encode('utf-8') if isinstance(name, str) else name
    for layer in _layers(raw):
        # Understand conventional nested field labels, not arbitrary substrings.
        for part in re.split(rb'[.\[\]]', layer.lower()):
            normalized = part.strip().replace(b'_', b'').replace(b'-', b'')
            if normalized.decode('ascii', errors='replace') in _SECRET_NAMES:
                return True
    return False


def contains_secret_fields(value) -> bool:
    pending = [value]
    visited = 0
    while pending:
        item = pending.pop()
        visited += 1
        require(visited <= 16384, 'request_too_large')
        if isinstance(item, Mapping):
            if any(is_secret_field(key) for key in item):
                return True
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return False


def require_safe_request_target(raw_path: bytes, query: bytes = b'', *, maximum: int) -> None:
    """Inspect raw ASGI/request-target bytes before routing or sending a request."""
    require(len(raw_path) + len(query) + bool(query) <= maximum, 'path_too_large')
    require(b'#' not in raw_path and b'#' not in query, 'secure_channel_required')
    require(raw_path.startswith(b'/') and not raw_path.startswith(b'//'), 'invalid_path')
    require(not _CONTROL.search(raw_path) and not _CONTROL.search(query), 'invalid_path')
    require(not _BAD_ESCAPE.search(raw_path) and not _BAD_ESCAPE.search(query), 'invalid_path')

    # Inspect delimiters at every level, not just the name after one decoding.
    # This catches both %2574oken and ordinary=x%26token=... without interpreting
    # either spelling as an additional business parameter.
    for layer in _layers(query):
        for pair in re.split(rb'[&;]', layer):
            require(not is_secret_field(pair.partition(b'=')[0]), 'secure_channel_required')

    ambiguous = False
    for layer in _layers(raw_path):
        require(b'\\' not in layer and b'\x00' not in layer, 'invalid_path')
        segments = layer.split(b'/')
        if len(segments) >= 5 and segments[:3] == [b'', b'-', b'g']:
            require(segments[4].lower() not in {b'token', b'bootstrap'}, 'secure_channel_required')
        # A labelled assignment in a path is not a permitted secret channel.
        for segment in segments:
            if b'=' in segment:
                require(not is_secret_field(segment.partition(b'=')[0]), 'secure_channel_required')
        if layer == b'/-' or layer.startswith(b'/-/'):
            original = raw_path.split(b'/')
            ambiguous |= (
                not (raw_path == b'/-' or raw_path.startswith(b'/-/'))
                or any(part in {b'.', b'..'} for part in segments)
                or any(b'%' in part for part in original[2:4])
                or (
                    len(original) > 4
                    and original[2] in {b'g', b'p'}
                    and b'.' in original[3]
                    and b'%' in original[4]
                )
            )
    require(not ambiguous, 'not_found')


def require_safe_relative_url(path: str, *, maximum: int) -> None:
    require(path.startswith('/') and not path.startswith('//'), 'invalid_relative_endpoint')
    # Fragments are not transmitted; reject them before httpx/urlsplit can drop
    # them. The server can only validate fragment-like bytes actually received.
    raw_path, _, query = path.encode('utf-8').partition(b'?')
    require_safe_request_target(raw_path, query, maximum=maximum)


def require_matching_host(
    values: list[str], expected: SplitResult, *, aliases: tuple[str, ...] = ()
) -> SplitResult:
    """Check Host syntax before a URL parser can discard delimiters or controls."""
    require(
        len(values) == 1
        and re.fullmatch(r'(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?', values[0])
        is not None,
        'forbidden_host',
    )
    try:
        supplied = urlsplit('//' + values[0])
        port = supplied.port
        require(port is None or port > 0, 'forbidden_host')
        for candidate in (expected, *(urlsplit(value) for value in aliases)):
            default = 443 if candidate.scheme == 'https' else 80
            if supplied.hostname == candidate.hostname and (
                port if port is not None else default
            ) == (candidate.port or default):
                return candidate
    except ValueError:
        raise Failure('forbidden_host') from None
    raise Failure('forbidden_host')
