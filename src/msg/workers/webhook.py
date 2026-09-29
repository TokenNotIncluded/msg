"""Small, opt-in Inbox webhook projection with pinned public DNS targets.

The receiver must persist delivery IDs for replay rejection across restarts.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import re
import secrets
import socket
from datetime import datetime
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from msg.core.codec import b64, loads, unb64
from msg.core.errors import Failure, require
from msg.security.network import normalized_host, validate_addresses


def seal_secret(app, subject: str, value: str) -> tuple[str, str]:
    secret = unb64(value, limit=64)
    require(32 <= len(secret) <= 64, 'invalid_webhook_secret')
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(app._vault_key).encrypt(nonce, secret, b'webhook-v1\0' + subject.encode())
    return b64(nonce), b64(ciphertext)


def open_secret(app, subject: str, nonce: str, ciphertext: str) -> bytes:
    return AESGCM(app._vault_key).decrypt(
        unb64(nonce, limit=12), unb64(ciphertext, limit=96), b'webhook-v1\0' + subject.encode()
    )


def validate_endpoint(url: str) -> tuple[str, int]:
    require(
        type(url) is str
        and 0 < len(url) <= 2048
        and '\\' not in url
        and all(32 < ord(char) < 127 for char in url),
        'invalid_webhook_url',
    )
    try:
        parsed = urlsplit(url)
        require(
            parsed.scheme == 'https'
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment,
            'invalid_webhook_url',
        )
        host = normalized_host(parsed.hostname)
        port = parsed.port or 443
    except ValueError as exc:
        raise Failure('invalid_webhook_url') from exc
    require(port == 443, 'webhook_port_forbidden')
    require(parsed.path.startswith('/') or parsed.path == '', 'invalid_webhook_url')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # DNS names are checked by the pinned resolver at delivery time.
    else:
        validate_addresses([host], _PUBLIC)
    return host, port


class _PublicPolicy:
    allow_private = False


_PUBLIC = _PublicPolicy()


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        resolved = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
        addresses = [entry[4][0] for entry in resolved]
        validate_addresses(addresses, _PUBLIC)
        return [
            {
                'hostname': host,
                'host': address,
                'port': port,
                'family': socket.AF_INET6 if ':' in address else socket.AF_INET,
                'proto': socket.IPPROTO_TCP,
                'flags': 0,
            }
            for address in addresses
        ]

    async def close(self):
        pass


def sign_delivery(secret: bytes, timestamp: str, body: bytes) -> str:
    return hmac.new(secret, timestamp.encode('ascii') + b'.' + body, hashlib.sha256).hexdigest()


def _delivery_header(headers, name, code):
    # HTTP field names are case-insensitive. Reject duplicates instead of making
    # the proxy, signature verifier and replay ledger pick different values.
    values = [
        value
        for key, value in headers.items()
        if isinstance(key, str) and key.lower() == name.lower()
    ]
    require(len(values) == 1 and type(values[0]) is str, code)
    return values[0]


def verify_delivery(
    headers: dict,
    body: bytes,
    secret: bytes,
    *,
    now: datetime,
    seen: set[str] | None = None,
    window_seconds: int = 300,
) -> str:
    """Authenticate the raw body *and* the identity used for persistent dedupe.

    Header identifiers alone are not signed. They must match the authenticated
    envelope before a receiver looks up or inserts a delivery in its replay log.
    ``seen`` is an in-process convenience; production receivers must atomically
    persist the returned ID together with their own business effect.
    """
    timestamp = _delivery_header(headers, 'Msg-Timestamp', 'invalid_webhook_timestamp')
    signature = _delivery_header(headers, 'Msg-Signature', 'invalid_webhook_signature')
    delivery_id = _delivery_header(headers, 'Msg-Delivery-Id', 'invalid_webhook_delivery')
    event_id = _delivery_header(headers, 'Msg-Event-Id', 'invalid_webhook_event')
    require(
        timestamp.isascii() and timestamp.isdecimal() and 0 < len(timestamp) <= 16,
        'invalid_webhook_timestamp',
    )
    require(type(window_seconds) is int and window_seconds >= 0, 'invalid_webhook_window')
    require(abs(int(now.timestamp()) - int(timestamp)) <= window_seconds, 'webhook_replay_window')
    require(
        re.fullmatch(r'job_[A-Za-z0-9_-]{1,156}', delivery_id) is not None,
        'invalid_webhook_delivery',
    )
    require(
        re.fullmatch(r'sha256=[0-9a-f]{64}', signature) is not None
        and hmac.compare_digest(signature[7:], sign_delivery(secret, timestamp, body)),
        'invalid_webhook_signature',
    )
    try:
        envelope = loads(body)
    except Failure as exc:
        # Never echo signed content or parser field names: the envelope may have
        # been supplied by an untrusted sender, even when a shared key matches.
        raise Failure('invalid_webhook_payload') from exc
    require(isinstance(envelope, dict), 'invalid_webhook_payload')
    require(envelope.get('delivery_id') == delivery_id, 'invalid_webhook_delivery')
    require(envelope.get('event_id') == event_id and bool(event_id), 'invalid_webhook_event')
    require(envelope.get('timestamp') == timestamp, 'invalid_webhook_timestamp')
    if seen is not None:
        require(delivery_id not in seen, 'webhook_replay')
        seen.add(delivery_id)
    return delivery_id


class WebhookSender:
    async def send(
        self,
        url: str,
        secret: bytes,
        body: bytes,
        *,
        timestamp: str,
        event_id: str,
        delivery_id: str,
    ) -> str:
        validate_endpoint(url)
        headers = {
            'Content-Type': 'application/json',
            'Msg-Event-Id': event_id,
            'Msg-Delivery-Id': delivery_id,
            'Msg-Timestamp': timestamp,
            'Msg-Signature': 'sha256=' + sign_delivery(secret, timestamp, body),
        }
        connector = aiohttp.TCPConnector(
            resolver=PublicResolver(), use_dns_cache=False, ttl_dns_cache=0, limit=1
        )
        try:
            async with aiohttp.ClientSession(
                connector=connector, trust_env=False, timeout=aiohttp.ClientTimeout(total=8)
            ) as session:
                async with session.post(
                    url, data=body, headers=headers, allow_redirects=False
                ) as response:
                    # Only the status matters; a remote endpoint must not make
                    # delivery allocate memory for an unbounded response body.
                    if 200 <= response.status < 300:
                        return 'delivered'
                    if response.status in {408, 429} or 500 <= response.status < 600:
                        return 'retry'
                    return 'failed'
        except Failure:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            # A broken connection may follow a successful remote commit.
            raise Failure('external_uncertain') from exc
