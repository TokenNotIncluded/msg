"""Trusted sandbox entrypoint. Receives a bounded policy packet, never code."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import resource
import socket
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
import dns.asyncresolver
from aiohttp.abc import AbstractResolver

from msg.core.codec import canonical, decode, loads, unb64
from msg.core.errors import Failure, require
from msg.core.models import NetworkPolicy
from msg.security.network import normalized_host, validate_addresses, validate_url


class PinnedResolver(AbstractResolver):
    def __init__(self, hostname, addresses):
        self.hostname, self.addresses = normalized_host(hostname), addresses

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        require(normalized_host(host) == self.hostname, 'unexpected_dns_target')
        return [
            {
                'hostname': host,
                'host': address,
                'port': port,
                'family': socket.AF_INET6 if ':' in address else socket.AF_INET,
                'proto': socket.IPPROTO_TCP,
                'flags': socket.AI_NUMERICHOST,
            }
            for address in self.addresses
        ]

    async def close(self):
        return None


async def addresses_for(host):
    host = normalized_host(host)
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        records = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return sorted({record[4][0] for record in records})


def select_target(url, method, addresses, policies):
    accepted = []
    for policy in policies:
        try:
            validate_url(url, method, policy)
            validate_addresses(addresses, policy)
            accepted.append(policy)
        except Failure:
            continue
    require(accepted, 'network_policy_denied')
    return accepted


async def http_request(args, policies):
    method, url = args.get('method', 'GET'), args['url']
    headers = dict(args.get('headers', {}))
    forbidden = {
        'host',
        'connection',
        'proxy-authorization',
        'proxy-connection',
        'transfer-encoding',
        'content-length',
        'upgrade',
        'te',
        'trailer',
        'forwarded',
        'x-forwarded-for',
        'x-forwarded-host',
    }
    for name, value in headers.items():
        require(
            name.lower() not in forbidden
            and '\r' not in value
            and '\n' not in value
            and all(33 <= ord(c) <= 126 and c not in '()<>@,;:\\"/[]?={} ' for c in name),
            'invalid_request_header',
        )
    body = unb64(args['body']) if 'body' in args else None
    redirects = 0
    # Alternative grants are not combined into a more powerful synthetic policy.
    # Keep only policies which authorize every hop of this request.
    surviving = list(policies)
    started = asyncio.get_running_loop().time()

    def unexpired(candidates):
        elapsed = (asyncio.get_running_loop().time() - started) * 1000
        remaining = [p for p in candidates if elapsed <= p.timeout_ms]
        require(remaining, 'external_uncertain')
        return remaining

    while True:
        surviving = unexpired(surviving)
        provisional = []
        for policy in surviving:
            try:
                provisional.append(validate_url(url, method, policy))
            except Failure:
                pass
        require(provisional, 'network_policy_denied')
        target = provisional[0]
        addresses = await addresses_for(target.hostname)
        surviving = select_target(url, method, addresses, unexpired(surviving))
        resolver = PinnedResolver(target.hostname, addresses)
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, ttl_dns_cache=0)
        timeout = aiohttp.ClientTimeout(
            total=max(
                0.001,
                max(p.timeout_ms for p in surviving) / 1000
                - (asyncio.get_running_loop().time() - started),
            )
        )
        stream = open(args['_body_path'], 'rb') if '_body_path' in args else None
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                trust_env=False,
                auto_decompress=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            ) as session:
                async with session.request(
                    method, url, headers=headers, data=stream or body, allow_redirects=False
                ) as response:
                    surviving = unexpired(surviving)
                    if response.status in {301, 302, 303, 307, 308} and response.headers.get(
                        'Location'
                    ):
                        require(
                            method in {'GET', 'HEAD'} or response.status in {307, 308},
                            'unsafe_redirect_method',
                        )
                        redirects += 1
                        surviving = [p for p in surviving if redirects <= p.max_redirects]
                        require(surviving, 'too_many_redirects')
                        next_url = urljoin(url, response.headers['Location'])
                        # Credentials never follow an origin change.
                        if (urlsplit(next_url).scheme, urlsplit(next_url).netloc) != (
                            target.scheme,
                            target.netloc,
                        ):
                            headers = {
                                k: v
                                for k, v in headers.items()
                                if k.lower() not in {'authorization', 'cookie'}
                            }
                        url = next_url
                        continue
                    length = 0
                    with Path('/output/result.bin').open('xb') as output:
                        async for chunk in response.content.iter_chunked(65536):
                            length += len(chunk)
                            surviving = [
                                p for p in unexpired(surviving) if length <= p.max_response_bytes
                            ]
                            require(surviving, 'tool_response_too_large')
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    surviving = unexpired(surviving)
                    selected = {
                        k: v[:4096]
                        for k, v in response.headers.items()
                        if k.lower()
                        in {'content-type', 'content-encoding', 'etag', 'last-modified'}
                    }
                    return {
                        'media_type': response.headers.get(
                            'Content-Type', 'application/octet-stream'
                        )[:255],
                        'metadata': {
                            'status': response.status,
                            'headers': selected,
                            'redirects': redirects,
                        },
                    }
        finally:
            if stream:
                stream.close()


async def dns_query(args, policies):
    name = normalized_host(args['name'])
    permitted = [p for p in policies if not p.hosts or name in p.hosts]
    require(permitted, 'network_policy_denied')
    resolver = dns.asyncresolver.Resolver()
    # Resolver addresses come only from the operator's read-only resolv.conf.
    started = asyncio.get_running_loop().time()
    answer = await resolver.resolve(
        name, args['type'], lifetime=max(p.timeout_ms for p in permitted) / 1000
    )
    elapsed = (asyncio.get_running_loop().time() - started) * 1000
    permitted = [p for p in permitted if elapsed <= p.timeout_ms]
    require(permitted, 'external_uncertain')
    values = [record.to_text() for record in answer]
    if args['type'] in {'A', 'AAAA'}:
        accepted = []
        for policy in permitted:
            try:
                validate_addresses(values, policy)
                accepted.append(policy)
            except Failure:
                continue
        require(accepted, 'network_policy_denied')
        permitted = accepted
    content = canonical({
        'name': name,
        'type': args['type'],
        'ttl': answer.rrset.ttl,
        'answers': values,
    })
    require(any(len(content) <= p.max_response_bytes for p in permitted), 'tool_response_too_large')
    Path('/output/result.bin').write_bytes(content)
    return {'media_type': 'application/json', 'metadata': {'kind': 'dns'}}


async def run(packet):
    policies = tuple(decode(NetworkPolicy, p) for p in packet['policies'])
    require(policies, 'network_policy_denied')
    require(packet['executor'] in {'curl', 'dns'}, 'untrusted_tool_executor')
    handler = http_request if packet['executor'] == 'curl' else dns_query
    try:
        return await asyncio.wait_for(
            handler(packet['arguments'], policies), max(p.timeout_ms for p in policies) / 1000
        )
    except Failure:
        raise
    except Exception as exc:
        # For HTTP, a read timeout can occur after a non-idempotent request was
        # accepted. It is not proof of non-execution.
        raise Failure('external_uncertain') from exc


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    try:
        raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
        require(len(raw) <= 2 * 1024 * 1024, 'tool_request_too_large')
        result = asyncio.run(run(loads(raw)))
        sys.stdout.buffer.write(canonical(result))
    except Failure as exc:
        sys.stdout.buffer.write(canonical({'code': exc.code}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
