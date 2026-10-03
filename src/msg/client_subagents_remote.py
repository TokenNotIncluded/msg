"""Private online mailboxes for local labels sharing one registered account.

This is a client adapter, not an identity type. The server authenticates every
operation as the owning account. Read calls never create resources. Inbox
cursors use the existing committed event stream, scoped to the account and
mailbox. Timestamp ties and concurrent inserts cannot skip messages. Stored
records bind to the immutable account ID, so changing a handle keeps mailboxes
and history intact; qualified addresses always show the current handle.
"""

import re
import uuid
from collections.abc import Mapping

import httpx

from msg.client_subagents import normalize_agent
from msg.core.codec import b64, canonical, loads, unb64
from msg.core.errors import Failure, require

PRIVATE_FIELDS = (
    'id',
    'type',
    'name',
    'owner',
    'mode',
    'state',
    'parent',
    'revision',
    'generation',
    'created_at',
)


class RemoteAgents:
    def __init__(self, client, username=None):
        self.client = client
        self._explicit_username = username.removeprefix('@') if username else None
        self.username = self._explicit_username

    async def _identity(self):
        require(self.client.state.subject is not None, 'connection_identity_required')
        value = await self._call(
            'discovery.get', {'id': self.client.state.subject, 'fields': ['id', 'name']}
        )
        name = value['name']
        require(name.startswith('@'), 'subagent_account_required')
        require(
            self._explicit_username is None or self._explicit_username == name[1:],
            'connection_user_mismatch',
        )
        self.username = name[1:]
        return '/@' + self.username + '/files/agents'

    async def account_name(self):
        await self._identity()
        return self.username

    def _label(self, name):
        return normalize_agent(name, self.username)

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
        return self._private_meta(data)

    def _private_meta(self, data):
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
        # The server applies the current read ACL and returns metadata and
        # content in one projection. Never cache namespace or privacy checks.
        data = await self._call('file.read', {'id': path, 'fields': [*PRIVATE_FIELDS, 'content']})
        return self._private_json(data)

    def _private_json(self, data):
        meta = self._private_meta(data)
        require(meta['type'] == 'file', 'invalid_subagent_message')
        try:
            content = data['content']
            value = loads(canonical(content)) if isinstance(content, Mapping) else loads(content)
        except (ValueError, TypeError) as exc:
            raise Failure('invalid_subagent_message') from exc
        return meta, value

    async def _send_preflight(self, root, sender, recipient):
        """Fresh namespace checks, combined only when the read adapter supports it."""
        paths = [root.rpartition('/')[0], root, root + '/' + sender, root + '/' + recipient]
        calls = [('discovery.get', {'id': path, 'view': 'meta'}) for path in paths]
        calls.extend(
            (
                'file.read',
                {'id': root + '/' + name + '/agent.json', 'fields': [*PRIVATE_FIELDS, 'content']},
            )
            for name in (sender, recipient)
        )
        read_many = getattr(self.client.transport, 'call_reads', None)
        results = None
        if read_many is not None:
            try:
                results = await read_many([self.client.prepare(op, args) for op, args in calls])
            except httpx.TimeoutException, httpx.NetworkError:
                # Reads have no uncertain mutation. Normal calls retain the
                # client's retry and OAuth-refresh behavior on the fallback.
                pass
            except Failure as exc:
                if not exc.retryable and exc.code not in {'not_found', 'dependency_unavailable'}:
                    raise
        retryable = results is not None and any(
            result.status == 'error' and result.error.retryable for result in results
        )
        permanent = results is not None and any(
            result.status == 'error' and not result.error.retryable for result in results
        )
        if results is None or retryable and not permanent:
            await self._directory(paths[0])
            await self._directory(root)
            await self._agent(root, sender)
            await self._agent(root, recipient)
            return
        if retryable:
            # A transient sibling must never turn an authority rejection into
            # another read attempt. Permanent errors win in either ordering.
            for index, result in enumerate(results):
                if result.status == 'error' and not result.error.retryable:
                    if index < 4 and result.error.code == 'not_found':
                        raise Failure('subagent_not_found')
                    self.client.checked(result)
        for result in results[:4]:
            if result.status == 'error' and result.error.code == 'not_found':
                raise Failure('subagent_not_found')
            data = self.client.checked(result).data
            meta = self._private_meta(data)
            require(meta['type'] == 'topic', 'subagent_private_namespace_conflict')
        for name, result in zip((sender, recipient), results[4:], strict=True):
            data = self.client.checked(result).data
            meta, value = self._private_json(data)
            config = self._config(meta, value, name)
            require(not config['archived'], 'subagent_archived')

    async def _receive_preflight(self, root, name):
        """Recheck the current private namespace, without serial HTTP overhead."""
        paths = [root.rpartition('/')[0], root, root + '/' + name]
        calls = [('discovery.get', {'id': path, 'view': 'meta'}) for path in paths]
        calls.append((
            'file.read',
            {'id': paths[-1] + '/agent.json', 'fields': [*PRIVATE_FIELDS, 'content']},
        ))
        read_many = getattr(self.client.transport, 'call_reads', None)
        results = None
        if read_many is not None:
            try:
                results = await read_many([self.client.prepare(op, args) for op, args in calls])
            except httpx.TimeoutException, httpx.NetworkError:
                pass
            except Failure as exc:
                if not exc.retryable and exc.code not in {'not_found', 'dependency_unavailable'}:
                    raise
        retryable, config_meta = False, None
        if results is not None:
            for index, result in enumerate(results):
                if result.status == 'error' and result.error.retryable:
                    retryable = True
                    continue
                if (
                    index < len(paths)
                    and result.status == 'error'
                    and result.error.code == 'not_found'
                ):
                    raise Failure('subagent_not_found')
                data = self.client.checked(result).data
                if index < len(paths):
                    meta = self._private_meta(data)
                    require(meta['type'] == 'topic', 'subagent_private_namespace_conflict')
                else:
                    config_meta, value = self._private_json(data)
                    # Archived labels remain readable; the physical namespace
                    # and stable account binding still have to be valid now.
                    self._config(config_meta, value, name)
            if not retryable:
                return config_meta
        # Unsupported/temporarily unavailable batches keep the original fresh
        # checks. A permanent result or invalid projection above cannot enter it.
        await self._directory(paths[0])
        await self._directory(root)
        config_meta, _ = await self._agent(root, name, active=False)
        return config_meta

    async def _put(self, parent, name, value, *, existing_match=None):
        result = await self.client.call(
            'file.create',
            {
                'parent': parent,
                'name': name,
                'data': b64(canonical(value)),
                'media_type': 'application/json',
            },
            return_fields=PRIVATE_FIELDS,
        )
        if result.status == 'error':
            if result.error.code != 'constraint_conflict':
                self.client.checked(result)
            meta, existing = await self._json(parent + '/' + name)
            require(
                existing_match(meta, existing) if existing_match is not None else existing == value,
                'subagent_message_id_conflict',
            )
            return meta
        # The projection is checked inside the same write transaction, so the
        # newly created file needs no second network read.
        meta = (
            await self._meta(parent + '/' + name)
            if result.replayed
            else self._private_meta(result.data['projection'][0])
        )
        require(
            meta['mode'] == 0o600 or meta['mode'] == '0600', 'subagent_private_namespace_conflict'
        )
        return meta

    def _stored_label(self, identity):
        require(
            isinstance(identity, str) and identity.startswith('@') and identity.count('#') == 1,
            'invalid_subagent_message',
        )
        account, label = identity.split('#')
        require(
            bool(re.fullmatch(r'@[A-Za-z0-9][A-Za-z0-9_-]*', account)), 'invalid_subagent_message'
        )
        return self._label(label)

    def _config(self, meta, config, name):
        # Legacy handles are display text. Only the already-verified private
        # leaf's stable owner decides which account this record belongs to.
        require(meta['owner'] == self.client.state.subject, 'subagent_private_namespace_conflict')
        require(
            isinstance(config, dict)
            and type(config.get('version')) is int
            and isinstance(config.get('archived'), bool),
            'invalid_subagent_message',
        )
        if config['version'] == 1:
            require(
                set(config) == {'version', 'identity', 'archived'}
                and self._stored_label(config['identity']) == name,
                'invalid_subagent_message',
            )
        else:
            require(
                config['version'] == 2
                and set(config) == {'version', 'owner', 'name', 'archived'}
                and config['owner'] == self.client.state.subject
                and config['name'] == name,
                'invalid_subagent_message',
            )
        return {
            'version': 2,
            'owner': self.client.state.subject,
            'name': name,
            'archived': config['archived'],
        }

    async def _agent(self, root, name, *, active=True):
        await self._directory(root + '/' + name)
        meta, value = await self._json(root + '/' + name + '/agent.json')
        config = self._config(meta, value, name)
        require(not active or not config['archived'], 'subagent_archived')
        return meta, config

    async def create(self, name):
        root = await self._root(create=True)
        name = self._label(name)
        await self._directory(root + '/' + name, create=True)
        config = {'version': 2, 'owner': self.client.state.subject, 'name': name, 'archived': False}
        await self._put(
            root + '/' + name,
            'agent.json',
            config,
            existing_match=lambda meta, existing: self._config(meta, existing, name) == config,
        )
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
                    'identity': self._full(name),
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

    async def send(self, sender, recipient, message, message_id=None, *, receipt=False):
        root = await self._identity()
        sender, recipient = self._label(sender), self._label(recipient)
        require(isinstance(message, str) and bool(message.strip()), 'invalid_subagent_message')
        require(len(message.encode()) <= 65536, 'subagent_message_too_large')
        message_id = message_id or uuid.uuid4().hex
        require(
            isinstance(message_id, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', message_id)),
            'invalid_subagent_message_id',
        )
        await self._send_preflight(root, sender, recipient)
        value = {
            'version': 2,
            'owner': self.client.state.subject,
            'id': message_id,
            'from': sender,
            'to': recipient,
            'message': message,
        }
        meta = await self._put(
            root + '/' + recipient,
            'msg-' + message_id + '.json',
            value,
            existing_match=lambda meta, existing: (
                self._event(meta, existing, recipient) == self._event(meta, value, recipient)
            ),
        )
        event = self._event(meta, value, recipient)
        if receipt:
            return {
                'type': 'subagent.receipt',
                'id': event['id'],
                'from': event['from'],
                'to': event['to'],
                'created_at': event['created_at'],
                'resource': {'id': meta['id'], 'revision': meta['revision']},
                'path': root + '/' + recipient + '/' + meta['name'],
                'body_bytes': len(message.encode()),
            }
        return event

    def _event(self, meta, value, recipient):
        require(meta['owner'] == self.client.state.subject, 'subagent_private_namespace_conflict')
        require(
            isinstance(value, dict) and type(value.get('version')) is int,
            'invalid_subagent_message',
        )
        if value['version'] == 1:
            require(
                set(value) == {'version', 'id', 'from', 'to', 'message'}, 'invalid_subagent_message'
            )
            sender = self._stored_label(value['from'])
            target = self._stored_label(value['to'])
        else:
            require(
                value['version'] == 2
                and set(value) == {'version', 'owner', 'id', 'from', 'to', 'message'}
                and value['owner'] == self.client.state.subject,
                'invalid_subagent_message',
            )
            sender, target = self._label(value['from']), self._label(value['to'])
            require(sender == value['from'] and target == value['to'], 'invalid_subagent_message')
        require(target == recipient, 'invalid_subagent_message')
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
            'from': self._full(sender),
            'to': self._full(target),
            'message': value['message'],
            'created_at': meta['created_at'],
        }

    def _legacy_scope_matches(self, saved, scope, agent):
        return (
            isinstance(saved, dict)
            and set(saved) == {'server', 'owner', 'to'}
            and saved['server'] == scope['server']
            and saved['owner'] == scope['owner']
            and self._stored_label(saved['to']) == agent
        )

    async def inbox(self, agent, cursor=None, limit=50, tail=False):
        root = await self._identity()
        agent = self._label(agent)
        require(type(limit) is int and 1 <= limit <= 200, 'invalid_subagent_limit')
        config_meta = await self._receive_preflight(root, agent)
        scope = {
            'server': self.client.state.server,
            'owner': self.client.state.subject,
            'agent': agent,
        }
        sync_cursor = None
        if cursor is not None:
            try:
                saved = loads(unb64(cursor, limit=65536))
                require(
                    saved['version'] == 1
                    and (
                        saved['scope'] == scope
                        or self._legacy_scope_matches(saved['scope'], scope, agent)
                    )
                    and isinstance(saved['sync_cursor'], str),
                    'invalid_subagent_cursor',
                )
                sync_cursor = saved['sync_cursor']
            except (ValueError, TypeError, KeyError) as exc:
                raise Failure('invalid_subagent_cursor') from exc
        items, more = [], False
        mailbox = config_meta['parent']
        page_limit = 1 if tail else limit
        large_pages = True
        pages = 0
        while True:
            arguments = {'limit': page_limit}
            if sync_cursor is not None:
                arguments['cursor'] = sync_cursor
            page = await self._call('communication.changes', arguments)
            pages += 1
            require(not page.get('resync_required'), 'resync_required')
            if tail and isinstance(page.get('tail_cursor'), str):
                sync_cursor = page['tail_cursor']
                more = False
                break
            resumable = all(isinstance(event.get('resume_cursor'), str) for event in page['items'])
            if not tail and page_limit > limit and not resumable:
                # Rolling deployments can route the next request to an older
                # worker. Re-read the unchanged cursor at the original bound
                # before consuming any of that non-resumable large page.
                page_limit = limit
                large_pages = False
                if pages >= 8:
                    more = True
                    break
                continue
            for number, event in enumerate(page['items']):
                if event['type'] not in {'file.create', 'content.file_put'}:
                    continue
                for ref in event['resources']:
                    hint = event.get('resource_parents', {}).get(ref['id'])
                    if isinstance(hint, Mapping):
                        if hint.get('parent') != mailbox or hint.get('name') == 'agent.json':
                            continue
                    else:
                        try:
                            meta = await self._call(
                                'discovery.get', {'id': ref['id'], 'view': 'meta'}
                            )
                        except Failure as exc:
                            if exc.code in {'not_found', 'subagent_archived'}:
                                continue
                            raise
                        if meta['parent'] != mailbox or meta['name'] == 'agent.json':
                            continue
                    try:
                        meta, value = await self._json(ref['id'])
                    except Failure as exc:
                        if exc.code in {'not_found', 'subagent_archived'}:
                            continue
                        raise
                    if meta['parent'] != mailbox or meta['name'] == 'agent.json':
                        continue
                    value = self._event(meta, value, agent)
                    if not tail:
                        items.append(value)
                if resumable and len(items) >= limit:
                    # A protected cursor after this event keeps the remaining
                    # large page unread, without embedding messages in a cursor.
                    sync_cursor = event['resume_cursor']
                    more = number + 1 < len(page['items']) or page.get(
                        'has_more', len(page['items']) == page_limit
                    )
                    break
            else:
                sync_cursor = page['sync_cursor']
                more = page.get('has_more', len(page['items']) == page_limit)
            if not more or (items and not tail) or (pages >= 8 and not tail):
                break
            # Only servers with event resume markers can enlarge a page without
            # consuming messages beyond the caller's bound. Old servers keep
            # their original bounded scan. A legacy tail can safely use 200.
            page_limit = 200 if tail or resumable and large_pages else limit
        return {
            'items': items,
            'cursor': b64(canonical({'version': 1, 'scope': scope, 'sync_cursor': sync_cursor})),
            'has_more': more,
        }
