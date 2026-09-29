"""Network policies are intersected; parsing or DNS never grants authority."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import replace
from urllib.parse import urlsplit

from msg.core.errors import Failure, require


def normalized_host(host):
    require(
        isinstance(host, str)
        and host
        and len(host) <= 253
        and '%' not in host
        and not any(ord(c) <= 32 or ord(c) == 127 for c in host),
        'invalid_host',
    )
    host = host.rstrip('.').lower()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        try:
            value = host.encode('idna').decode('ascii')
        except UnicodeError as exc:
            raise Failure('invalid_host') from exc
        require(
            all(
                re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                for part in value.split('.')
            ),
            'invalid_host',
        )
        return value


def validate_url(url, method, policy):
    require(
        type(url) is str
        and 0 < len(url) <= 8192
        and '\\' not in url
        and all(ord(c) > 32 and ord(c) != 127 for c in url),
        'invalid_network_url',
    )
    try:
        parsed = urlsplit(url)
        require(
            parsed.scheme in policy.schemes and parsed.scheme in {'http', 'https'},
            'network_scheme_forbidden',
        )
        require(
            parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment,
            'invalid_network_url',
        )
        host = normalized_host(parsed.hostname)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError as exc:
        if isinstance(exc, Failure):
            raise
        raise Failure('invalid_network_url') from exc
    require(port in policy.ports, 'network_port_forbidden')
    require(method in policy.methods, 'network_method_forbidden')
    require(
        not policy.hosts or host in {normalized_host(h) for h in policy.hosts},
        'network_host_forbidden',
    )
    return parsed


def validate_addresses(addresses, policy):
    require(bool(addresses), 'dns_no_address')
    results = []
    for value in addresses:
        require('%' not in value, 'invalid_network_target')
        try:
            ip = ipaddress.ip_address(value)
        except ValueError as exc:
            raise Failure('invalid_network_target') from exc
        actual = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if not policy.allow_private:
            require(actual.is_global and not actual.is_multicast, 'private_target_forbidden')
        require(not actual.is_unspecified and not actual.is_multicast, 'invalid_network_target')
        results.append(value)
    return results


def intersect_policy(policy, constraints):
    require(
        set(constraints)
        <= {
            'hosts',
            'schemes',
            'ports',
            'methods',
            'timeout_ms',
            'max_response_bytes',
            'max_redirects',
        },
        'unknown_network_constraint',
    )
    values = {}
    for field in ('hosts', 'schemes', 'ports', 'methods'):
        if field not in constraints:
            continue
        supplied = set(constraints[field])
        if field == 'hosts':
            supplied = {normalized_host(h) for h in supplied}
        current = set(getattr(policy, field))
        narrowed = supplied if field == 'hosts' and not current else current & supplied
        require(bool(narrowed), 'network_policy_empty')
        values[field] = tuple(sorted(narrowed)) if field == 'hosts' else frozenset(narrowed)
    for field in ('timeout_ms', 'max_response_bytes', 'max_redirects'):
        if field in constraints:
            value = constraints[field]
            require(
                type(value) is int and value >= (0 if field == 'max_redirects' else 1),
                'invalid_network_limit',
            )
            values[field] = min(value, getattr(policy, field))
    return replace(policy, **values)
