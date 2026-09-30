"""Stable account names and an account-wide seven-day rename interval."""

import re
from datetime import timedelta

from msg.core.codec import parse_time, wire
from msg.core.errors import require

HANDLE_RENAME_INTERVAL = timedelta(days=7)


def claim_handle(tx, parent, handle, subject):
    require(
        isinstance(handle, str)
        and re.fullmatch(r'[a-z][a-z0-9-]{1,40}', handle) is not None
        and handle not in {'root', 'online-ca'},
        'invalid_handle',
    )
    name = '@' + handle
    require(
        not tx.one(
            'SELECT id FROM resources WHERE parent=? AND name=? AND id<>?', (parent, name, subject)
        )
        and not tx.one(
            'SELECT resource_id FROM resource_path_aliases '
            'WHERE parent_id=? AND name=? AND resource_id<>?',
            (parent, name, subject),
        ),
        'handle_unavailable',
    )
    return name


def check_handle_change(tx, user, handle, now, *, record=False):
    name = claim_handle(tx, user.parent, handle, user.id)
    key = 'identity_handle_renamed_at:' + user.id
    last = tx.setting(key)
    next_at = parse_time(last) + HANDLE_RENAME_INTERVAL if last is not None else now
    if name != user.name:
        require(now >= next_at, 'handle_rename_cooldown', details={'next_rename_at': wire(next_at)})
        if record:
            tx.set_setting(key, wire(now))
            next_at = now + HANDLE_RENAME_INTERVAL
    return name, next_at
