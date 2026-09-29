"""A verified endpoint denotes one mailbox, never a header/address list.

SMTP uses the real stdlib send_message parser and serializer with only sendmail
replaced by a recorder. No socket, SMTP server or external address is contacted.
"""
from datetime import UTC, datetime
from email import policy
from types import SimpleNamespace
import smtplib

import pytest

from msg.core.codec import canonical
from msg.core.errors import Failure
from msg.core.models import EmailSettings
from msg.market.delivery_targets import email_binding, validate_address
from msg.workers import mail


AMBIGUOUS = (
    'alias,buyer@example.test', 'buyer@example.test,alias',
    ',buyer@example.test', 'buyer@example.test,',
    'buyer@example.test;', 'group:buyer@example.test;',
    '<buyer@example.test>', 'buyer(comment)@example.test',
    'buyer@example.test(comment)', 'buyer@exam(comment)ple.test',
    '"buyer"@example.test', 'buyer..alias@example.test',
    'buyer@example..test', 'buyer@example.test\x7f',
)
VALID = (
    'buyer@example.test', 'Buyer+tag@Example.TEST', 'first.last@example.test',
    'a_b-c@example.test', "o'hara@example.test", 'buyer@xn--bcher-kva.test',
    'buyer@例子.测试', '用户@example.test', '"a,b"@example.test',
)


@pytest.mark.parametrize('address', AMBIGUOUS)
def test_market_rejects_non_exact_single_mailbox_without_leaking_it(address):
    with pytest.raises(Failure) as caught:
        validate_address(address)
    assert caught.value.as_dict() == {'code': 'invalid_email', 'retryable': False,
                                           'message': 'Enter one valid email address.'}
    assert address not in str(caught.value)


@pytest.mark.parametrize('codepoint', [*range(33), 127])
def test_market_rejects_controls_and_whitespace(codepoint):
    with pytest.raises(Failure, match='invalid_email'):
        validate_address('buyer' + chr(codepoint) + '@example.test')


@pytest.mark.parametrize('address', [None, 1, [], {}, '', 'buyer', '@example.test',
                                   'buyer@localhost', 'x' * 243 + '@example.test'])
def test_market_rejects_non_addresses(address):
    with pytest.raises(Failure, match='invalid_email'):
        validate_address(address)


@pytest.mark.parametrize('address', VALID)
def test_valid_mailbox_bytes_are_not_rewritten(address):
    validate_address(address)
    header = policy.default.header_factory('To', address)
    assert len(header.addresses) == 1
    assert header.addresses[0].addr_spec == address


@pytest.mark.parametrize('address', AMBIGUOUS)
def test_legacy_verified_row_cannot_authorize_an_ambiguous_recipient(address):
    email = EmailSettings(subject_id='u_buyer', address=address,
                          verified_at=datetime(2026, 1, 1, tzinfo=UTC))
    tx = SimpleNamespace(one=lambda *_: (3, canonical(email)))
    with pytest.raises(Failure, match='invalid_email'):
        email_binding(tx, 'u_buyer', address)


@pytest.fixture
def smtp(monkeypatch):
    seen = SimpleNamespace(connections=0, envelopes=[], messages=[], closed=0,
                           fail_after_data=False, refused={})
    real_smtp = smtplib.SMTP

    class RecordingSMTP(real_smtp):
        def __init__(self, *_args, **_kwargs):
            super().__init__(local_hostname='client.test')
            self.ehlo_resp = b'250 test'
            self.esmtp_features = {'smtputf8': ''}
            self.does_esmtp = True
            seen.connections += 1

        def ehlo(self, name=''):
            return 250, b'test'

        def starttls(self, *, context=None):
            return 220, b'test'

        def send_message(self, msg, from_addr=None, to_addrs=None,
                         mail_options=(), rcpt_options=()):
            seen.envelopes.append(to_addrs)
            return super().send_message(msg, from_addr, to_addrs, mail_options, rcpt_options)

        def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
            seen.messages.append((from_addr, list(to_addrs), msg))
            if seen.fail_after_data:
                raise smtplib.SMTPServerDisconnected('simulated after DATA')
            return seen.refused

        def close(self):
            seen.closed += 1

    monkeypatch.setattr(mail.smtplib, 'SMTP', RecordingSMTP)
    monkeypatch.setattr(mail.smtplib, 'SMTP_SSL', RecordingSMTP)
    return seen


def sender(tls='starttls'):
    return mail.SmtpSender(SimpleNamespace(enabled=True, host='smtp.invalid', port=587,
        tls=tls, sender='msg@example.test', credential_file=None))


def job(recipient, verification=False):
    return SimpleNamespace(id='job_mailbox', arguments={
        'recipient': recipient, 'recipient_subject': 'u_buyer', 'verification': verification,
        'subject': 'Verify msg email' if verification else 'msg order ready',
        'text': 'test-only notification',
    })


@pytest.mark.parametrize('address', AMBIGUOUS)
@pytest.mark.parametrize('verification', [False, True])
def test_smtp_rejects_ambiguous_legacy_jobs_before_connecting(smtp, address, verification):
    with pytest.raises(Failure, match='invalid_email'):
        sender()._send(job(address, verification))
    assert smtp.connections == 0
    assert smtp.messages == []


@pytest.mark.parametrize('address', VALID)
@pytest.mark.parametrize('tls', ['starttls', 'tls'])
def test_smtp_pins_one_exact_envelope_recipient(smtp, address, tls):
    assert sender(tls)._send(job(address)) == 'sent'
    assert smtp.envelopes == [[address]]  # Must not infer targets from headers.
    assert len(smtp.messages) == 1
    assert smtp.messages[0][1] == [address]
    assert smtp.closed == 1


def test_smtp_uncertain_boundary_is_not_retried_or_changed(smtp):
    smtp.fail_after_data = True
    assert sender()._send(job('buyer@example.test')) == 'uncertain'
    assert smtp.envelopes == [['buyer@example.test']]
    assert len(smtp.messages) == 1
    assert smtp.closed == 1
