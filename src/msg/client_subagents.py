"""Private, account-scoped local agent labels and an offline SQLite mailbox.

Labels identify collaborators within one trusted OS user; they are not server
accounts, separate credentials, or access-control principals. Reading advances
only a caller's cursor, so independent background listeners never steal mail.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from msg.atomic_file import durable_write
from msg.client import private_client_json
from msg.core.errors import Failure, require
from msg.paths import private_directory


def validate_username(value):
    if isinstance(value, str) and value.startswith('/@'):
        value = value[2:]
    elif isinstance(value, str) and value.startswith('@'):
        value = value[1:]
    require(
        isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9-]{1,40}', value),
        'invalid_subagent_username',
    )
    return value


def normalize_agent(value, username):
    username = validate_username(username)
    require(isinstance(value, str), 'invalid_subagent_name')
    if value.startswith('@'):
        account, separator, value = value[1:].partition('#')
        require(separator and account == username, 'subagent_account_mismatch')
    elif value.startswith('#'):
        value = value[1:]
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value), 'invalid_subagent_name')
    return value


def agent_name(value, username):
    return f'@{validate_username(username)}#{normalize_agent(value, username)}'


def _now():
    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _private_file(path, *, create=False, allow_missing=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if allow_missing and not path.is_symlink():
            return None
        raise Failure('unsafe_subagent_state') from None
    except OSError:
        raise Failure('unsafe_subagent_state') from None
    info = os.fstat(descriptor)
    if not (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and info.st_mode & 0o077 == 0
        and (info.st_nlink == 1 or (allow_missing and info.st_nlink == 0))
    ):
        os.close(descriptor)
        raise Failure('unsafe_subagent_state')
    return descriptor


@contextmanager
def _lock(directory):
    descriptor = _private_file(directory / 'lock', create=True)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


class LocalAgents:
    def __init__(self, state, username=None):
        self.server = state.server
        origin = hashlib.sha256(self.server.encode()).hexdigest()
        root = private_directory(state.paths.data / 'subagents')
        origin_dir = private_directory(root / origin)
        supplied = validate_username(username) if username is not None else None
        handle = state.data.get('handle')
        handle = validate_username(handle) if handle else None
        require(not supplied or not handle or supplied == handle, 'subagent_account_mismatch')
        subject = state.data.get('subject_id')
        if not subject:
            with _lock(origin_dir):
                path = origin_dir / 'offline-account.json'
                previous = private_client_json(path)
                username = supplied or handle or (previous or {}).get('username')
                require(username is not None, 'subagent_username_required')
                username = validate_username(username)
                if previous is None or previous.get('username') != username:
                    durable_write(path, _encoded({'version': 1, 'username': username}))
        else:
            username = supplied or handle
        account = (
            {'server': self.server, 'subject': subject}
            if subject
            else {
                'server': self.server,
                'local_username': username,
            }
        )
        self.owner = hashlib.sha256(_encoded(account)).hexdigest()
        self.directory = private_directory(origin_dir / self.owner)
        self.path = self.directory / 'mailbox.sqlite3'
        with _lock(self.directory):
            path = self.directory / 'agents.json'
            config = private_client_json(path)
            if config is not None:
                require(config.get('version') == 1, 'invalid_subagent_state')
                require(config.get('owner') == self.owner, 'subagent_account_mismatch')
            username = username or (config or {}).get('username')
            require(username is not None, 'subagent_username_required')
            self.username = validate_username(username)
            if config is None:
                config = {
                    'version': 1,
                    'owner': self.owner,
                    'username': self.username,
                    'cursor_key': os.urandom(32).hex(),
                }
            elif config.get('username') != self.username:
                # A registered account may rename its handle without losing mail.
                config['username'] = self.username
            try:
                self._cursor_key = bytes.fromhex(config['cursor_key'])
            except KeyError, ValueError, TypeError:
                raise Failure('invalid_subagent_state') from None
            require(len(self._cursor_key) == 32, 'invalid_subagent_state')
            durable_write(path, _encoded(config))
            os.close(_private_file(self.path, create=True))
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS agents (
                        name TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        archived_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS messages (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        sender TEXT NOT NULL REFERENCES agents(name),
                        recipient TEXT NOT NULL REFERENCES agents(name),
                        message TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS messages_recipient_seq
                        ON messages(recipient, seq);
                    """
                )

    @contextmanager
    def _connection(self):
        private_directory(self.directory)
        for name in (
            'mailbox.sqlite3',
            'mailbox.sqlite3-journal',
            'mailbox.sqlite3-wal',
            'mailbox.sqlite3-shm',
        ):
            path = self.directory / name
            if path.exists() or path.is_symlink():
                descriptor = _private_file(path, allow_missing=name != 'mailbox.sqlite3')
                if descriptor is not None:
                    os.close(descriptor)
        # SQLite journals inherit the private database mode. No WAL files or
        # connection objects survive a method call.
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA busy_timeout=30000')
            connection.execute('PRAGMA foreign_keys=ON')
            with connection:
                yield connection

    def _agent(self, row):
        return {
            'name': agent_name(row['name'], self.username),
            'label': row['name'],
            'created_at': row['created_at'],
            'archived_at': row['archived_at'],
            'active': row['archived_at'] is None,
        }

    def create(self, name):
        name = normalize_agent(name, self.username)
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute(
                'INSERT OR IGNORE INTO agents(name, created_at) VALUES (?, ?)', (name, _now())
            )
            row = connection.execute('SELECT * FROM agents WHERE name=?', (name,)).fetchone()
            require(row['archived_at'] is None, 'subagent_archived')
            return self._agent(row)

    def list(self):
        with self._connection() as connection:
            return [
                self._agent(row) for row in connection.execute('SELECT * FROM agents ORDER BY name')
            ]

    def archive(self, name):
        name = normalize_agent(name, self.username)
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._exists(connection, name)
            connection.execute(
                'UPDATE agents SET archived_at=COALESCE(archived_at, ?) WHERE name=?',
                (_now(), name),
            )
            return self._agent(
                connection.execute('SELECT * FROM agents WHERE name=?', (name,)).fetchone()
            )

    def show(self, name):
        name = normalize_agent(name, self.username)
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM agents WHERE name=?', (name,)).fetchone()
            require(row is not None, 'subagent_not_found')
            return self._agent(row)

    @staticmethod
    def _exists(connection, name, *, active=False):
        row = connection.execute('SELECT * FROM agents WHERE name=?', (name,)).fetchone()
        require(row is not None, 'subagent_not_found')
        require(not active or row['archived_at'] is None, 'subagent_archived')

    def _event(self, row):
        return {
            'id': row['id'],
            'type': 'subagent.message',
            'seq': row['seq'],
            'from': agent_name(row['sender'], self.username),
            'to': agent_name(row['recipient'], self.username),
            'message': row['message'],
            'created_at': row['created_at'],
        }

    def read(self, agent, message_id):
        recipient = normalize_agent(agent, self.username)
        require(
            isinstance(message_id, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', message_id)),
            'invalid_subagent_message_id',
        )
        with self._connection() as connection:
            self._exists(connection, recipient)
            row = connection.execute(
                'SELECT * FROM messages WHERE recipient=? AND id=?',
                (recipient, message_id),
            ).fetchone()
            require(row is not None, 'not_found')
            return self._event(row)

    def send(self, sender, recipient, message, message_id=None):
        sender = normalize_agent(sender, self.username)
        recipient = normalize_agent(recipient, self.username)
        require(isinstance(message, str) and bool(message), 'invalid_subagent_message')
        if message_id is None:
            message_id = str(uuid4())
        else:
            require(
                isinstance(message_id, str)
                and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', message_id)),
                'invalid_subagent_message_id',
            )
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._exists(connection, sender, active=True)
            self._exists(connection, recipient, active=True)
            previous = connection.execute(
                'SELECT * FROM messages WHERE id=?', (message_id,)
            ).fetchone()
            if previous is not None:
                require(
                    (previous['sender'], previous['recipient'], previous['message'])
                    == (sender, recipient, message),
                    'subagent_message_conflict',
                )
                return self._event(previous)
            connection.execute(
                'INSERT INTO messages(id, sender, recipient, message, created_at) VALUES (?, ?, ?, ?, ?)',
                (message_id, sender, recipient, message, _now()),
            )
            return self._event(
                connection.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
            )

    def _cursor(self, recipient, seq):
        payload = _encoded({'owner': self.owner, 'recipient': recipient, 'seq': seq, 'version': 1})
        signature = hmac.digest(self._cursor_key, payload, 'sha256')
        return base64.urlsafe_b64encode(signature + payload).decode().rstrip('=')

    def _position(self, cursor, recipient):
        if cursor is None:
            return 0
        try:
            require(isinstance(cursor, str) and len(cursor) <= 1024, 'invalid_subagent_cursor')
            value = base64.b64decode(
                cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True
            )
            signature, payload = value[:32], value[32:]
            require(
                hmac.compare_digest(signature, hmac.digest(self._cursor_key, payload, 'sha256')),
                'invalid_subagent_cursor',
            )
            data = json.loads(payload)
            require(
                data['version'] == 1
                and data['owner'] == self.owner
                and data['recipient'] == recipient
                and type(data['seq']) is int
                and data['seq'] >= 0,
                'invalid_subagent_cursor',
            )
            return data['seq']
        except ValueError, KeyError, TypeError:
            raise Failure('invalid_subagent_cursor') from None

    def inbox(self, agent, cursor=None, limit=50, tail=False, sender=None):
        recipient = normalize_agent(agent, self.username)
        sender_norm = normalize_agent(sender, self.username) if sender is not None else None
        require(type(limit) is int and 1 <= limit <= 200, 'invalid_subagent_limit')
        require(type(tail) is bool and not (tail and cursor is not None), 'invalid_subagent_cursor')
        position = self._position(cursor, recipient)
        with self._connection() as connection:
            self._exists(connection, recipient)
            if sender_norm is not None:
                self._exists(connection, sender_norm)
            if tail:
                if sender_norm is not None:
                    row = connection.execute(
                        'SELECT COALESCE(MAX(seq), 0) FROM messages WHERE recipient=? AND sender=?',
                        (recipient, sender_norm),
                    ).fetchone()
                else:
                    row = connection.execute(
                        'SELECT COALESCE(MAX(seq), 0) FROM messages WHERE recipient=?',
                        (recipient,),
                    ).fetchone()
                position = row[0]
                return {'items': [], 'cursor': self._cursor(recipient, position), 'has_more': False}
            if sender_norm is not None:
                rows = connection.execute(
                    'SELECT * FROM messages WHERE recipient=? AND sender=? AND seq>? ORDER BY seq LIMIT ?',
                    (recipient, sender_norm, position, limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    'SELECT * FROM messages WHERE recipient=? AND seq>? ORDER BY seq LIMIT ?',
                    (recipient, position, limit + 1),
                ).fetchall()
            items = [self._event(row) for row in rows[:limit]]
            return {
                'items': items,
                'cursor': self._cursor(recipient, items[-1]['seq'] if items else position),
                'has_more': len(rows) > limit,
            }
