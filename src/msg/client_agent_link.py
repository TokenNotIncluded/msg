"""Task-scoped private Agent Link files; no credential issuance or autonomous ACKs.

The server enforces existing operation/resource ceilings. Message schemas and
the append-only task lifecycle below are client checks, not new server policy.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from msg.atomic_file import durable_write
from msg.core.codec import b64, canonical, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.paths import ClientPaths

FIELDS = (
    'id',
    'type',
    'parent',
    'owner',
    'mode',
    'state',
    'revision',
    'created_by',
    'created_at',
    'revision_created_at',
    'content',
)
ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
MAX_BYTES = 65536
MAX_EVENTS = 200
KINDS = {'accept', 'progress', 'deliver'}


def fixed_ref(value):
    require(
        isinstance(value, Mapping) and set(value) == {'id', 'revision'},
        'agent_link_fixed_ref_required',
    )
    require(
        all(isinstance(value[k], str) and ID.fullmatch(value[k]) for k in ('id', 'revision')),
        'agent_link_fixed_ref_required',
    )
    return dict(value)


def _hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _json_file(path):
    path = Path(path)
    require(not path.is_symlink(), 'agent_link_private_file_required')
    with path.open('rb') as stream:
        raw = stream.read(1048577)
    require(len(raw) <= 1048576, 'agent_link_file_too_large')
    try:
        return loads(raw)
    except (ValueError, TypeError) as exc:
        raise Failure('agent_link_invalid_json') from exc


@contextmanager
def _journal(path):
    path = Path(path)
    require(not path.is_symlink(), 'agent_link_private_file_required')
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        require(os.fstat(fd).st_mode & 0o077 == 0, 'agent_link_private_file_required')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Failure('agent_link_journal_in_use') from exc
        if path.exists():
            require(path.stat().st_mode & 0o077 == 0, 'agent_link_private_file_required')
            saved = _json_file(path)
            require(
                isinstance(saved, dict)
                and saved.get('version') == 1
                and isinstance(saved.get('entries'), dict),
                'agent_link_invalid_journal',
            )
        else:
            saved = {'version': 1, 'entries': {}}
        yield path, saved
    finally:
        os.close(fd)


class AgentLink:
    def __init__(self, client):
        self.client = client
        url = urlsplit(client.state.server)
        require(
            url.scheme in {'https', 'http'}
            and url.netloc
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment,
            'agent_link_invalid_server',
        )

    def output_path(self, path):
        path = Path(path).expanduser()
        require(not path.is_symlink(), 'agent_link_private_file_required')
        target = path.resolve()
        default = ClientPaths.discover()
        selected = self.client.state.paths
        protected = {
            default.config,
            default.data,
            default.state,
            selected.config,
            selected.data,
            selected.state,
        }
        require(
            not any(target.is_relative_to(root.resolve()) for root in protected),
            'agent_link_profile_output_forbidden',
        )
        return path

    async def _call(self, operation, arguments, **kwargs):
        try:
            result = self.client.checked(await self.client.call(operation, arguments, **kwargs))
        except Failure as exc:
            # Do not echo request bodies, transport URLs or server error details.
            details = {'request_id': kwargs['request_id']} if 'request_id' in kwargs else None
            raise Failure(exc.code, retryable=exc.retryable, details=details) from exc
        require(
            result.status == 'ok',
            'agent_link_result_uncertain',
            retryable=True,
            details={'request_id': kwargs.get('request_id')} if kwargs.get('request_id') else None,
        )
        return result

    @staticmethod
    def _private(meta, owner, kind):
        mode = meta.get('mode', 0)
        try:
            mode = int(mode, 8) if isinstance(mode, str) else mode
        except (TypeError, ValueError) as exc:
            raise Failure('agent_link_private_namespace_required') from exc
        require(
            meta.get('owner') == owner
            and meta.get('type') == kind
            and meta.get('state') == 'active'
            and mode == (0o700 if kind == 'topic' else 0o600),
            'agent_link_private_namespace_required',
        )
        return meta

    async def _directory(self, ident, owner=None):
        result = await self._call('discovery.get', {'id': ident, 'view': 'meta'})
        return self._private(result.data, owner or result.subject, 'topic'), result

    async def _record(self, ref, owner=None):
        ref = fixed_ref(ref)
        result = await self._call('file.read', {**ref, 'fields': list(FIELDS)})
        meta = self._private(result.data, owner or result.subject, 'file')
        require(
            meta['id'] == ref['id'] and meta['revision'] == ref['revision'],
            'agent_link_revision_mismatch',
        )
        value = meta['content']
        try:
            value = loads(canonical(value)) if isinstance(value, Mapping) else loads(value)
            require(
                isinstance(value, dict) and len(canonical(value)) <= MAX_BYTES,
                'agent_link_invalid_record',
            )
        except (ValueError, TypeError) as exc:
            raise Failure('agent_link_invalid_record') from exc
        # created_by is immutable, but a newer revision may have another actor.
        # Read server history to verify the author of this exact pinned revision.
        cursor, revision_actor = None, None
        for _ in range(10):
            args = {'id': ref['id'], 'view': 'history', 'limit': 20}
            if cursor:
                args['cursor'] = cursor
            history = (await self._call('file.read', args)).data
            for row in history['revisions']:
                if row['id'] == ref['revision']:
                    revision_actor = row['actor']
                    break
            cursor = history.get('cursor')
            if revision_actor is not None or not cursor:
                break
        require(
            revision_actor is not None and revision_actor == meta['created_by'],
            'agent_link_author_mismatch',
        )
        return meta, value, result

    async def _invitation(self, invitation):
        meta, body, result = await self._record(invitation)
        require(
            set(body)
            == {
                'version',
                'kind',
                'owner',
                'worker_actor',
                'mode',
                'incoming',
                'outgoing',
                'expires_at',
                'task',
            }
            and type(body['version']) is int
            and body['version'] == 1
            and body['kind'] == 'invite'
            and body['owner'] == meta['owner'] == meta['created_by']
            and body['incoming'] == meta['parent']
            and isinstance(body['mode'], str)
            and body['mode'] in {'independent', 'shared-token'}
            and isinstance(body['task'], str)
            and isinstance(body['worker_actor'], str)
            and ID.fullmatch(body['worker_actor']),
            'agent_link_invalid_invitation',
        )
        require(
            (body['mode'] == 'shared-token') == (body['worker_actor'] == body['owner']),
            'agent_link_identity_mode_mismatch',
        )
        try:
            expiry = parse_time(body['expires_at'])
        except (ValueError, TypeError) as exc:
            raise Failure('agent_link_invalid_expiry') from exc
        await self._directory(body['incoming'], body['owner'])
        await self._directory(body['outgoing'], body['owner'])
        require(body['incoming'] != body['outgoing'], 'agent_link_distinct_channels_required')
        return body, result, expiry

    async def get(self, invitation):
        """Read status; this never writes or implicitly accepts an invitation."""
        invitation = fixed_ref(invitation)
        body, _, expiry = await self._invitation(invitation)
        records, ignored, cursor = [], 0, None
        for _ in range(10):
            args = {
                'parent': body['outgoing'],
                'limit': 50,
                'fields': ['id', 'type', 'parent', 'revision'],
            }
            if cursor:
                args['cursor'] = cursor
            page = (await self._call('discovery.list', args)).data
            for item in page['items']:
                if len(records) + ignored >= MAX_EVENTS:
                    raise Failure('agent_link_event_limit')
                if item['type'] != 'file':
                    ignored += 1
                    continue
                ref = {'id': item['id'], 'revision': item['revision']}
                try:
                    meta, value, _ = await self._record(ref, body['owner'])
                    require(
                        meta['parent'] == body['outgoing']
                        and meta['created_by'] == body['worker_actor']
                        and set(value) - {'previous'}
                        == {'version', 'kind', 'invitation', 'message', 'artifacts'}
                        and type(value['version']) is int
                        and value['version'] == 1
                        and isinstance(value['kind'], str)
                        and value['kind'] in KINDS
                        and fixed_ref(value['invitation']) == invitation
                        and isinstance(value['message'], str)
                        and isinstance(value['artifacts'], list)
                        and len(value['artifacts']) <= 20,
                        'agent_link_invalid_record',
                    )
                    previous = (
                        fixed_ref(value['previous']) if value.get('previous') is not None else None
                    )
                    artifacts = [fixed_ref(x) for x in value['artifacts']]
                    require(
                        value['kind'] == 'deliver' or not artifacts, 'agent_link_invalid_record'
                    )
                    require(
                        parse_time(meta['revision_created_at']) <= expiry,
                        'agent_link_record_expired',
                    )
                    records.append({
                        'resource': ref,
                        'kind': value['kind'],
                        'previous': previous,
                        'artifacts': artifacts,
                    })
                except Failure as exc:
                    if exc.retryable or exc.code not in {
                        'agent_link_invalid_record',
                        'agent_link_fixed_ref_required',
                        'agent_link_author_mismatch',
                        'agent_link_revision_mismatch',
                        'agent_link_private_namespace_required',
                        'agent_link_record_expired',
                        'invalid_json',
                        'invalid_utf8',
                    }:
                        raise
                    ignored += 1
                except ValueError, TypeError, KeyError:
                    ignored += 1
            cursor = page.get('cursor')
            if not cursor:
                break
        require(not cursor, 'agent_link_event_limit')
        state, previous, chain, artifacts, ambiguous = 'invited', None, [], [], False
        remaining = list(records)
        while remaining and state != 'delivered':
            valid = [
                x
                for x in remaining
                if x['previous'] == previous
                and (
                    (state == 'invited' and x['kind'] == 'accept')
                    or (state in {'accepted', 'progress'} and x['kind'] in {'progress', 'deliver'})
                )
            ]
            # Multiple successors are a fork, never silently choose one.
            if len(valid) != 1:
                ambiguous = len(valid) > 1
                break
            event = valid[0]
            chain.append({'resource': event['resource'], 'kind': event['kind']})
            previous = event['resource']
            state = {'accept': 'accepted', 'progress': 'progress', 'deliver': 'delivered'}[
                event['kind']
            ]
            artifacts = event['artifacts']
            remaining.remove(event)
        return {
            'invitation': invitation,
            'state': state,
            'mode': body['mode'],
            'owner': body['owner'],
            'worker_actor': body['worker_actor'],
            'expired': self.client.clock() >= expiry,
            'expires_at': body['expires_at'],
            'events': chain,
            'artifacts': artifacts,
            'ignored': ignored + len(remaining),
            'ambiguous': ambiguous,
        }

    async def task(self, invitation):
        body, _, _ = await self._invitation(fixed_ref(invitation))
        return body['task']

    async def _write(self, *, parent, body, request_id, journal, saved, path):
        require(
            isinstance(request_id, str) and ID.fullmatch(request_id),
            'agent_link_request_id_required',
        )
        args = {
            'parent': parent,
            'name': 'link-' + _hash(request_id) + '.json',
            'data': b64(canonical(body)),
            'media_type': 'application/json',
        }
        saved['record_digest'] = _hash(args)
        durable_write(path, canonical(journal), mode=0o600)
        result = await self._call('file.create', args, request_id=request_id)
        require(len(result.resources) == 1, 'agent_link_invalid_receipt')
        ref = wire(result.resources[0])
        meta, record, _ = await self._record(ref, result.subject)
        require(
            meta['parent'] == parent and meta['created_by'] == result.actor and record == body,
            'agent_link_invalid_receipt',
        )
        receipt = {
            'resource': fixed_ref(ref),
            'request_id': request_id,
            'actor': result.actor,
            'subject': result.subject,
            'kind': body['kind'],
        }
        saved['receipt'] = receipt
        durable_write(path, canonical(journal), mode=0o600)
        return receipt

    async def invite(
        self, *, incoming, outgoing, worker_actor, mode, task, expires_at=None, request_id, journal
    ):
        journal = self.output_path(journal)
        require(
            isinstance(task, str) and len(task.encode()) <= MAX_BYTES // 2,
            'agent_link_task_too_large',
        )
        require(
            mode in {'independent', 'shared-token'}
            and isinstance(worker_actor, str)
            and ID.fullmatch(worker_actor),
            'agent_link_identity_mode_mismatch',
        )
        meta, current = await self._directory(incoming)
        other, _ = await self._directory(outgoing, meta['owner'])
        require(current.actor == current.subject == meta['owner'], 'agent_link_owner_required')
        require(meta['id'] != other['id'], 'agent_link_distinct_channels_required')
        require(
            (mode == 'shared-token') == (worker_actor == current.actor),
            'agent_link_identity_mode_mismatch',
        )
        intention = {
            'server': self.client.state.server,
            'owner': current.subject,
            'incoming': meta['id'],
            'outgoing': other['id'],
            'worker_actor': worker_actor,
            'mode': mode,
            'task_digest': _hash(task),
            'expires_at': wire(expires_at) if expires_at is not None else None,
        }
        with _journal(journal) as (path, book):
            entry = book['entries'].get(request_id)
            if entry is None:
                expiry = parse_time(expires_at) if isinstance(expires_at, str) else expires_at
                expiry = expiry or self.client.clock() + timedelta(hours=1)
                require(
                    self.client.clock() < expiry <= self.client.clock() + timedelta(hours=1),
                    'agent_link_invalid_expiry',
                )
                entry = {'intent_digest': _hash(intention), 'expires_at': wire(expiry)}
                book['entries'][request_id] = entry
            require(entry['intent_digest'] == _hash(intention), 'agent_link_request_id_conflict')
            body = {
                'version': 1,
                'kind': 'invite',
                'owner': current.subject,
                'worker_actor': worker_actor,
                'mode': mode,
                'incoming': meta['id'],
                'outgoing': other['id'],
                'expires_at': entry['expires_at'],
                'task': task,
            }
            return await self._write(
                parent=meta['id'],
                body=body,
                request_id=request_id,
                journal=book,
                saved=entry,
                path=path,
            )

    async def _event(self, kind, invitation, *, message, artifacts, request_id, journal):
        journal = self.output_path(journal)
        invitation = fixed_ref(invitation)
        require(
            isinstance(message, str) and len(message.encode()) <= MAX_BYTES // 2,
            'agent_link_message_too_large',
        )
        artifacts = [fixed_ref(x) for x in artifacts]
        require(len(artifacts) <= 20, 'agent_link_artifact_limit')
        body, current, expiry = await self._invitation(invitation)
        require(
            current.actor == body['worker_actor'] and current.subject == body['owner'],
            'agent_link_worker_required',
        )
        if body['mode'] == 'shared-token':
            require(
                self.client.state.token is not None or bool(self.client.state.data.get('api_key')),
                'agent_link_shared_token_required',
            )
        require(self.client.clock() < expiry, 'agent_link_expired')
        intention = {
            'server': self.client.state.server,
            'actor': current.actor,
            'subject': current.subject,
            'kind': kind,
            'invitation': invitation,
            'message_digest': _hash(message),
            'artifacts': artifacts,
        }
        with _journal(journal) as (path, book):
            entry = book['entries'].get(request_id)
            if entry is None:
                status = await self.get(invitation)
                require(
                    status['state'] == 'invited'
                    if kind == 'accept'
                    else status['state'] in {'accepted', 'progress'},
                    'agent_link_transition_required',
                )
                require(not status['ambiguous'], 'agent_link_channel_ambiguous')
                previous = status['events'][-1]['resource'] if status['events'] else None
                entry = {'intent_digest': _hash(intention), 'previous': previous}
                book['entries'][request_id] = entry
            require(entry['intent_digest'] == _hash(intention), 'agent_link_request_id_conflict')
            record = {
                'version': 1,
                'kind': kind,
                'invitation': invitation,
                'message': message,
                'artifacts': artifacts,
            }
            # Enveloped JSON projections omit null fields. The first explicit
            # accept has no previous record; later records pin their predecessor.
            if entry['previous'] is not None:
                record['previous'] = entry['previous']
            return await self._write(
                parent=body['outgoing'],
                body=record,
                request_id=request_id,
                journal=book,
                saved=entry,
                path=path,
            )

    async def accept(self, invitation, *, request_id, journal):
        return await self._event(
            'accept', invitation, message='', artifacts=[], request_id=request_id, journal=journal
        )

    async def progress(self, invitation, *, message, request_id, journal):
        return await self._event(
            'progress',
            invitation,
            message=message,
            artifacts=[],
            request_id=request_id,
            journal=journal,
        )

    async def deliver(self, invitation, *, message, artifacts, request_id, journal):
        return await self._event(
            'deliver',
            invitation,
            message=message,
            artifacts=artifacts,
            request_id=request_id,
            journal=journal,
        )


def add_commands(commands):
    actions = commands.add_parser(
        'agent-link', help='Explicit private task exchange; no auto grants.'
    ).add_subparsers(dest='action', required=True)
    invite = actions.add_parser('invite', help='Write a task in preconfigured private channels.')
    for name in ('incoming', 'outgoing', 'worker-actor'):
        invite.add_argument('--' + name, required=True)
    invite.add_argument('--mode', choices=('independent', 'shared-token'), required=True)
    invite.add_argument('--task-file', type=Path, required=True)
    invite.add_argument('--expires-at')
    for name in ('accept', 'progress', 'deliver', 'get'):
        sub = actions.add_parser(name)
        sub.add_argument('--invitation', required=True)
        sub.add_argument('--revision', required=True)
        if name == 'get':
            sub.add_argument('--task-output', type=Path)
        elif name in {'progress', 'deliver'}:
            content = sub.add_mutually_exclusive_group(required=True)
            content.add_argument('--text')
            content.add_argument('--file', type=Path)
            if name == 'deliver':
                sub.add_argument(
                    '--artifact', nargs=2, action='append', default=[], metavar=('ID', 'REVISION')
                )
    for name in ('invite', 'accept', 'progress', 'deliver'):
        sub = actions.choices[name]
        sub.add_argument('--request-id', required=True, help='Stable ID; reuse after errors.')
        sub.add_argument(
            '--journal', type=Path, required=True, help='Private independent retry journal.'
        )


def _text_file(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES // 2 + 1)
    require(len(raw) <= MAX_BYTES // 2, 'agent_link_file_too_large')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise Failure('agent_link_text_required') from exc


async def run_command(client, args):
    link = AgentLink(client)
    if args.action == 'invite':
        return await link.invite(
            incoming=args.incoming,
            outgoing=args.outgoing,
            worker_actor=args.worker_actor,
            mode=args.mode,
            task=_text_file(args.task_file),
            expires_at=args.expires_at,
            request_id=args.request_id,
            journal=args.journal,
        )
    invitation = {'id': args.invitation, 'revision': args.revision}
    if args.action == 'get':
        result = await link.get(invitation)
        if args.task_output:
            target = link.output_path(args.task_output)
            durable_write(target, (await link.task(invitation)).encode(), mode=0o600)
            result['task_file'] = str(args.task_output)
        return result
    kwargs = {'request_id': args.request_id, 'journal': args.journal}
    if args.action == 'accept':
        return await link.accept(invitation, **kwargs)
    kwargs['message'] = args.text if args.text is not None else _text_file(args.file)
    if args.action == 'deliver':
        kwargs['artifacts'] = [
            {'id': ident, 'revision': revision} for ident, revision in args.artifact
        ]
    return await getattr(link, args.action)(invitation, **kwargs)
