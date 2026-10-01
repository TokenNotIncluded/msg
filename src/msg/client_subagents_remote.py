"""Private online mailboxes for local labels sharing one registered account.

This is a client adapter, not an identity type. The server authenticates every
operation as the owning account. Read calls never create resources. Inbox
cursors use the existing committed event stream, scoped to the account and
mailbox. Timestamp ties and concurrent inserts cannot skip messages.
"""

import re
import uuid
from collections.abc import Mapping

from msg.core.codec import b64, canonical, loads, unb64
from msg.core.errors import Failure, require


class RemoteAgents:
    def __init__(self, client, username=None):
        self.client = client
        self.username = username.removeprefix('@') if username else None

    async def _identity(self):
        require(self.client.state.subject is not None, 'connection_identity_required')
        value = await self._call(
            'discovery.get', {'id': self.client.state.subject, 'fields': ['id', 'name']}
        )
        name = value['name']
        require(name.startswith('@'), 'subagent_account_required')
        require(self.username is None or self.username == name[1:], 'connection_user_mismatch')
        self.username = name[1:]
        return '/@' + self.username + '/files/agents'

    async def account_name(self):
        await self._identity()
        return self.username

    def _label(self, name):
        require(isinstance(name, str), 'invalid_subagent_name')
        if '#' in name:
            account, name = name.split('#', 1)
            require(account == '@' + self.username, 'subagent_account_mismatch')
        require(
            bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name)), 'invalid_subagent_name'
        )
        return name

    def _full(self, name):
        return '@' + self.username + '#' + name

    async def _call(self, operation, args, **kwargs):
        return self.client.checked(await self.client.call(operation, args, **kwargs)).data

    async def _meta(self, path, *, missing=False):
        try:
            data = await self._call('discovery.get', {'id': path, 'view': 'meta'})
        except Failure as exc:
            if missing and exc.code == 'not_found':
                return None
            raise
        mode = data['mode']
        mode = int(mode, 8) if isinstance(mode, str) else mode
        require(
            data['owner'] == self.client.state.subject and mode & 0o077 == 0,
            'subagent_private_namespace_conflict',
        )
        require(
            mode & 0o700 == (0o700 if data['type'] == 'topic' else 0o600),
            'subagent_private_namespace_conflict',
        )
        require(data['state'] == 'active', 'subagent_archived')
        return data

    async def _directory(self, path, *, create=False):
        meta = await self._meta(path, missing=True)
        if meta is None and create:
            parent, _, name = path.rpartition('/')
            result = await self.client.call('file.mkdir', {'parent': parent, 'name': name})
            if result.status == 'error':
                if result.error.code != 'constraint_conflict':
                    self.client.checked(result)
            else:
                # Only our new resource is changed; existing public data is rejected.
                await self._call(
                    'content.chmod',
                    {'id': result.resources[0].id, 'mode': '0700'},
                    expected=((result.resources[0].id, result.data['generation']),),
                )
                meta = await self._meta(path)
                await self._call(
                    'content.topic_configure',
                    {'id': meta['id'], 'policy': {'topic_mode': '0700', 'file_mode': '0600'}},
                    expected=((meta['id'], meta['generation']),),
                )
            meta = await self._meta(path)
        require(meta is not None, 'subagent_not_found')
        require(meta['type'] == 'topic', 'subagent_private_namespace_conflict')
        return meta

    async def _root(self, *, create=False):
        path = await self._identity()
        await self._directory(path.rpartition('/')[0])
        await self._directory(path, create=create)
        return path

    async def _json(self, path):
        meta = await self._meta(path)
        require(meta['type'] == 'file', 'invalid_subagent_message')
        data = await self._call('file.read', {'id': meta['id']})
        try:
            content = data['content']
            value = loads(canonical(content)) if isinstance(content, Mapping) else loads(content)
        except (ValueError, TypeError) as exc:
            raise Failure('invalid_subagent_message') from exc
        return meta, value

    async def _put(self, parent, name, value):
        result = await self.client.call(
            'file.create',
            {
                'parent': parent,
                'name': name,
                'data': b64(canonical(value)),
                'media_type': 'application/json',
            },
        )
        if result.status == 'error':
            if result.error.code != 'constraint_conflict':
                self.client.checked(result)
            meta, existing = await self._json(parent + '/' + name)
            require(existing == value, 'subagent_message_id_conflict')
            return meta
        meta = await self._meta(parent + '/' + name)
        require(
            meta['mode'] == 0o600 or meta['mode'] == '0600', 'subagent_private_namespace_conflict'
        )
        return meta

    async def _agent(self, root, name, *, active=True):
        await self._directory(root + '/' + name)
        meta, config = await self._json(root + '/' + name + '/agent.json')
        require(
            isinstance(config, dict)
            and config.get('version') == 1
            and config.get('identity') == self._full(name)
            and isinstance(config.get('archived'), bool),
            'invalid_subagent_message',
        )
        require(not active or not config['archived'], 'subagent_archived')
        return meta, config

    async def create(self, name):
        root = await self._root(create=True)
        name = self._label(name)
        await self._directory(root + '/' + name, create=True)
        config = {'version': 1, 'identity': self._full(name), 'archived': False}
        await self._put(root + '/' + name, 'agent.json', config)
        return {'name': name, 'identity': self._full(name), 'archived': False}

    async def _children(self, parent, resource_type):
        arguments = {
            'parent': parent,
            'type': resource_type,
            'sort': 'time',
            'direction': 'asc',
            'limit': 200,
            'fields': ['id', 'name', 'path', 'created_at'],
        }
        while True:
            page = await self._call('discovery.list', arguments)
            yield page['items']
            if not page.get('cursor'):
                break
            arguments['cursor'] = page['cursor']

    async def list(self):
        try:
            root = await self._root()
        except Failure as exc:
            if exc.code == 'subagent_not_found':
                return []
            raise
        values = []
        async for page in self._children(root, 'topic'):
            for item in page:
                name = self._label(item['name'])
                _, config = await self._agent(root, name, active=False)
                values.append({
                    'name': name,
                    'identity': config['identity'],
                    'archived': config['archived'],
                })
        return sorted(values, key=lambda item: item['name'])

    async def archive(self, name):
        root = await self._root()
        name = self._label(name)
        meta, config = await self._agent(root, name, active=False)
        if not config['archived']:
            config['archived'] = True
            await self._call(
                'file.write',
                {
                    'id': meta['id'],
                    'base_revision': meta['revision'],
                    'data': b64(canonical(config)),
                    'media_type': 'application/json',
                },
                expected=((meta['id'], meta['generation']),),
            )
        return {'name': name, 'identity': self._full(name), 'archived': True}

    async def send(self, sender, recipient, message, message_id=None):
        root = await self._root()
        sender, recipient = self._label(sender), self._label(recipient)
        require(isinstance(message, str) and bool(message.strip()), 'invalid_subagent_message')
        require(len(message.encode()) <= 65536, 'subagent_message_too_large')
        message_id = message_id or uuid.uuid4().hex
        require(
            isinstance(message_id, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', message_id)),
            'invalid_subagent_message_id',
        )
        await self._agent(root, sender)
        await self._agent(root, recipient)
        value = {
            'version': 1,
            'id': message_id,
            'from': self._full(sender),
            'to': self._full(recipient),
            'message': message,
        }
        meta = await self._put(root + '/' + recipient, 'msg-' + message_id + '.json', value)
        return self._event(meta, value, recipient)

    def _event(self, meta, value, recipient):
        require(
            isinstance(value, dict) and set(value) == {'version', 'id', 'from', 'to', 'message'},
            'invalid_subagent_message',
        )
        require(
            type(value['version']) is int
            and value['version'] == 1
            and value['to'] == self._full(recipient),
            'invalid_subagent_message',
        )
        require(
            isinstance(value['from'], str) and value['from'].startswith('@' + self.username + '#'),
            'invalid_subagent_message',
        )
        self._label(value['from'])
        require(
            isinstance(value['id'], str)
            and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value['id']))
            and meta['name'] == 'msg-' + value['id'] + '.json',
            'invalid_subagent_message',
        )
        require(
            isinstance(value['message'], str)
            and bool(value['message'].strip())
            and len(value['message'].encode()) <= 65536,
            'invalid_subagent_message',
        )
        return {
            'type': 'subagent.message',
            'id': value['id'],
            'from': value['from'],
            'to': value['to'],
            'message': value['message'],
            'created_at': meta['created_at'],
        }

    async def inbox(self, agent, cursor=None, limit=50, tail=False):
        root = await self._root()
        agent = self._label(agent)
        require(type(limit) is int and 1 <= limit <= 200, 'invalid_subagent_limit')
        await self._agent(root, agent, active=False)
        scope = {
            'server': self.client.state.server,
            'owner': self.client.state.subject,
            'to': self._full(agent),
        }
        sync_cursor = None
        if cursor is not None:
            try:
                saved = loads(unb64(cursor, limit=65536))
                require(
                    saved['version'] == 1
                    and saved['scope'] == scope
                    and isinstance(saved['sync_cursor'], str),
                    'invalid_subagent_cursor',
                )
                sync_cursor = saved['sync_cursor']
            except (ValueError, TypeError, KeyError) as exc:
                raise Failure('invalid_subagent_cursor') from exc
        items, more = [], False
        mailbox = (await self._directory(root + '/' + agent))['id']
        while True:
            arguments = {'limit': limit if not tail else 200}
            if sync_cursor is not None:
                arguments['cursor'] = sync_cursor
            page = await self._call('communication.changes', arguments)
            require(not page.get('resync_required'), 'resync_required')
            for event in page['items']:
                if event['type'] not in {'file.create', 'content.file_put'}:
                    continue
                for ref in event['resources']:
                    # Check metadata and owner before ever reading the message body.
                    try:
                        meta = await self._call('discovery.get', {'id': ref['id'], 'view': 'meta'})
                    except Failure as exc:
                        if exc.code in {'not_found', 'subagent_archived'}:
                            continue
                        raise
                    if meta['parent'] != mailbox or meta['name'] == 'agent.json':
                        continue
                    meta, value = await self._json(meta['id'])
                    value = self._event(meta, value, agent)
                    if not tail:
                        items.append(value)
            sync_cursor = page['sync_cursor']
            more = len(page['items']) == arguments['limit']
            if not more or (items and not tail):
                break
        return {
            'items': items,
            'cursor': b64(canonical({'version': 1, 'scope': scope, 'sync_cursor': sync_cursor})),
            'has_more': more,
        }
