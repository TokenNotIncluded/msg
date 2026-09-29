"""Validate one exact mailbox before binding or passing it to SMTP.

An email endpoint is an addr-spec, not a display name, group or address list.
Do not normalize it: the verified endpoint snapshot must identify the same bytes.
"""

from __future__ import annotations

import re
from email import policy
from email.errors import HeaderParseError, NonASCIILocalPartDefect
from email.headerregistry import Address

from msg.core.errors import Failure, require


def validate_address(address: str) -> Address:
    require(
        isinstance(address, str)
        and len(address) <= 254
        and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', address) is not None
        and not any(ord(c) < 32 or ord(c) == 127 for c in address),
        'invalid_email',
    )
    try:
        header = policy.default.header_factory('To', address)
        mailboxes, groups = header.addresses, header.groups
    except HeaderParseError, ValueError, IndexError:
        raise Failure('invalid_email') from None
    # SMTPUTF8 mailboxes remain supported. All other parser defects, including
    # obsolete or partially recovered syntax, fail closed rather than redirect.
    require(
        len(mailboxes) == len(groups) == 1
        and groups[0].display_name is None
        and all(isinstance(d, NonASCIILocalPartDefect) for d in header.defects),
        'invalid_email',
    )
    mailbox = mailboxes[0]
    require(
        bool(mailbox.username and mailbox.domain) and mailbox.addr_spec == address, 'invalid_email'
    )
    return mailbox
