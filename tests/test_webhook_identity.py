"""Only authenticated delivery identity may enter a receiver's replay ledger."""

from datetime import UTC, datetime

import pytest

from msg.core.codec import canonical
from msg.core.errors import Failure
from msg.workers.webhook import sign_delivery, verify_delivery

NOW = datetime(2026, 9, 27, tzinfo=UTC)
SECRET = b'webhook-receiver-regression-key!!'


def delivery(*, body=None):
    timestamp = str(int(NOW.timestamp()))
    body = (
        body
        if body is not None
        else canonical({
            'event_id': 'e_committed',
            'delivery_id': 'job_original',
            'timestamp': timestamp,
            'subject_id': 'u_recipient',
            'type': 'inbox.reference',
        })
    )
    return {
        'Msg-Timestamp': timestamp,
        'Msg-Delivery-Id': 'job_original',
        'Msg-Event-Id': 'e_committed',
        'Msg-Signature': 'sha256=' + sign_delivery(SECRET, timestamp, body),
    }, body


def test_altering_unsigned_delivery_header_cannot_bypass_deduplication():
    headers, body = delivery()
    seen = set()
    assert verify_delivery(headers, body, SECRET, now=NOW, seen=seen) == 'job_original'
    headers['Msg-Delivery-Id'] = 'job_attacker_replay'
    with pytest.raises(Failure, match='invalid_webhook_delivery'):
        verify_delivery(headers, body, SECRET, now=NOW, seen=seen)
    assert seen == {'job_original'}


@pytest.mark.parametrize(
    'header,value,code',
    [
        ('Msg-Event-Id', 'e_changed', 'invalid_webhook_event'),
        ('Msg-Timestamp', None, 'invalid_webhook_timestamp'),
        ('Msg-Signature', None, 'invalid_webhook_signature'),
        ('Msg-Signature', 'sha256=\u00e9', 'invalid_webhook_signature'),
        ('Msg-Delivery-Id', None, 'invalid_webhook_delivery'),
        ('Msg-Delivery-Id', 'job_bad\nvalue', 'invalid_webhook_delivery'),
    ],
)
def test_bad_headers_are_stable_failures_without_mutating_replay_state(header, value, code):
    headers, body = delivery()
    headers[header] = value
    seen = set()
    with pytest.raises(Failure, match=code):
        verify_delivery(headers, body, SECRET, now=NOW, seen=seen)
    assert not seen


@pytest.mark.parametrize(
    'body',
    [
        b'[]',
        b'{}',
        b'not-json',
        b'{"delivery_id":"job_original","delivery_id":"job_changed"}',
        canonical({'delivery_id': 'job_original', 'event_id': 'e_committed', 'timestamp': '0'}),
    ],
)
def test_signed_but_invalid_identity_envelope_is_not_accepted(body):
    headers, body = delivery(body=body)
    seen = set()
    with pytest.raises(Failure):
        verify_delivery(headers, body, SECRET, now=NOW, seen=seen)
    assert not seen


def test_header_names_are_case_insensitive_but_ambiguous_duplicates_are_rejected():
    headers, body = delivery()
    lower = {name.lower(): value for name, value in headers.items()}
    assert verify_delivery(lower, body, SECRET, now=NOW) == 'job_original'
    lower['Msg-Delivery-Id'] = 'job_changed'
    with pytest.raises(Failure, match='invalid_webhook_delivery'):
        verify_delivery(lower, body, SECRET, now=NOW)
