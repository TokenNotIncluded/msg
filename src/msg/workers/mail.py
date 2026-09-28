"""Optional TLS SMTP projection. SMTP acknowledgement is not exactly-once delivery."""
from __future__ import annotations
import asyncio
from email.message import EmailMessage
import smtplib
import ssl
from msg.core.codec import loads
from msg.core.errors import Failure, require
from msg.core.email_address import validate_address


class SmtpSender:
    def __init__(self, config):
        self.config = config

    async def send(self, job):
        require(self.config is not None and self.config.enabled, 'mail_disabled')
        return await asyncio.to_thread(self._send, job)

    def _send(self, job):
        config = self.config
        # Revalidate at the external boundary, including jobs queued by old code.
        # Invalid targets fail before opening a connection or reading credentials.
        recipient = validate_address(job.arguments['recipient'])
        message = EmailMessage()
        message['From'] = config.sender
        message['To'] = recipient
        message['Subject'] = job.arguments['subject']
        # A stable Message-ID is useful for tracing; not a remote dedup guarantee.
        message['Message-ID'] = '<'+job.id+'@msgd.local>'
        message.set_content(job.arguments['text'])
        sending = False
        connection = None
        try:
            context = ssl.create_default_context()
            if config.tls == 'tls':
                connection = smtplib.SMTP_SSL(config.host, config.port, timeout=20, context=context)
            else:
                connection = smtplib.SMTP(config.host, config.port, timeout=20)
                connection.ehlo()
                connection.starttls(context=context)
                connection.ehlo()
            if config.credential_file:
                credentials = loads(config.credential_file.read_bytes())
                require(set(credentials) == {'username', 'password'}, 'invalid_smtp_credentials')
                connection.login(credentials['username'], credentials['password'])
            # Once send_message starts, an exception can happen after the receiver
            # accepted DATA. Do not automatically retry this ambiguous boundary.
            sending = True
            refused = connection.send_message(message, to_addrs=[recipient.addr_spec])
            require(not refused, 'mail_recipient_refused')
            return 'sent'
        except (smtplib.SMTPException, OSError, Failure) as exc:
            if sending:
                return 'uncertain'
            raise Failure('mail_connection_failed', retryable=True) from exc
        finally:
            if connection:
                # QUIT failure cannot undo a successful DATA acknowledgement.
                try:
                    connection.close()
                except OSError:
                    pass
