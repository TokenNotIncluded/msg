"""Bounded Sequenzy transactional login mail; acceptance is not delivery.

Contract: https://docs.sequenzy.com/api-reference/transactional/send
Credentials never leave the Authorization header. No automatic send retries.
"""

from __future__ import annotations

import asyncio
import re
from email.utils import parseaddr
from html import escape
from pathlib import Path
from time import monotonic

from httpx import Client, HTTPError, StreamError, Timeout

from msg.core.codec import loads
from msg.core.email_address import validate_address
from msg.core.errors import Failure, require
from msg.security.root_files import read_private

SEND_URL = 'https://api.sequenzy.com/api/v1/transactional/send'
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CREDENTIAL_BYTES = 16 * 1024
MAX_SEND_SECONDS = 20.0


def _api_key(path):
    try:
        credentials = loads(read_private(path, limit=MAX_CREDENTIAL_BYTES))
        require(
            isinstance(credentials, dict) and set(credentials) == {'api_key'},
            'invalid_sequenzy_credentials',
        )
        key = credentials['api_key']
        require(
            isinstance(key, str)
            and re.fullmatch(r'[A-Za-z0-9_.-]{1,4096}', key) is not None
            # Account keys need an explicit workspace selection that this
            # deliberately small credential/config contract does not expose.
            and not key.startswith('seq_user_'),
            'invalid_sequenzy_credentials',
        )
        return key
    except Failure, OSError, TypeError, ValueError:
        # Private-file and JSON exceptions can carry a path or arbitrary key.
        raise Failure('invalid_sequenzy_credentials') from None


def _sender(value):
    if value is None:
        return None
    require(
        isinstance(value, str)
        and 0 < len(value) <= 320
        and not any(ord(c) < 32 or ord(c) == 127 for c in value),
        'invalid_sequenzy_sender',
    )
    try:
        _, address = parseaddr(value, strict=True)
        validate_address(address)
    except Failure, ValueError:
        raise Failure('invalid_sequenzy_sender') from None
    return value


def _accepted(response, deadline):
    require(monotonic() <= deadline, 'sequenzy_send_failed')
    require(200 <= response.status_code < 300, 'sequenzy_send_failed')
    require(
        response.headers.get('content-encoding', 'identity').lower() == 'identity',
        'invalid_sequenzy_response',
    )
    length = response.headers.get('content-length')
    if length is not None:
        require(
            length.isdecimal() and int(length) <= MAX_RESPONSE_BYTES,
            'invalid_sequenzy_response',
        )
    chunks, size = [], 0
    for chunk in response.iter_raw():
        require(monotonic() <= deadline, 'sequenzy_send_failed')
        size += len(chunk)
        require(size <= MAX_RESPONSE_BYTES, 'invalid_sequenzy_response')
        chunks.append(chunk)
    try:
        value = loads(b''.join(chunks))
    except Failure, ValueError:
        raise Failure('invalid_sequenzy_response') from None
    require(
        isinstance(value, dict)
        and value.get('success') is True
        and value.get('emailType', 'transactional') == 'transactional',
        'invalid_sequenzy_response',
    )
    # Response IDs, recipient echoes and diagnostics are never returned/logged.
    return 'queued'


class SequenzySender:
    def __init__(self, credential_file, sender=None):
        require(
            isinstance(credential_file, (str, Path)) and bool(str(credential_file)),
            'invalid_sequenzy_credentials',
        )
        self.credential_file = Path(credential_file)
        self.sender = _sender(sender)

    async def send_code(self, address, code):
        return await asyncio.to_thread(self._send_code, address, code)

    def _send_code(self, address, code):
        recipient = validate_address(address).addr_spec
        require(
            isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9]{1,128}', code) is not None,
            'invalid_login_code',
        )
        key = _api_key(self.credential_file)
        payload = {
            'to': recipient,
            'subject': 'Your MSG login code',
            'body': '<p>Your MSG login code is:</p><p><strong>'
            + escape(code)
            + '</strong></p><p>If you did not request it, ignore this email.</p>',
            'emailType': 'transactional',
            'trackingSettings': {'clickTracking': False, 'openTracking': False},
        }
        if self.sender is not None:
            payload['from'] = self.sender
        deadline = monotonic() + MAX_SEND_SECONDS
        try:
            with Client(
                timeout=Timeout(10.0, connect=5.0), follow_redirects=False, trust_env=False
            ) as client:
                with client.stream(
                    'POST',
                    SEND_URL,
                    headers={
                        'Authorization': 'Bearer ' + key,
                        'Accept': 'application/json',
                        'Accept-Encoding': 'identity',
                    },
                    json=payload,
                ) as response:
                    return _accepted(response, deadline)
        except HTTPError, StreamError, OSError, ValueError:
            # A timeout/read error may follow remote acceptance. Do not retry it
            # automatically or expose HTTP exceptions with request/response data.
            raise Failure('sequenzy_send_failed') from None
