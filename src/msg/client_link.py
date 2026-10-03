"""Agent Link: invite a helper agent into one task without sharing the account.

This composes existing mechanisms; it adds no server authority. The owner
creates two private remote mailbox labels for the link and issues a delegated
task identity whose grants cover only those two mailboxes. The helper's keys
are generated on the helper and never leave it. Every code exchanged here holds
public keys, proofs and references only, never a private key or bearer token.

    owner   msg link invite NAME --task ...    -> onboarding prompt
    helper  msg --config-dir DIR link join CODE  -> join code (public keys)
    owner   msg link approve JOIN_CODE         -> access prompt
    helper  msg --config-dir DIR link accept CODE -> explicit acceptance + task

Revocation ends the delegated identity and archives both labels; history and
already committed writes remain.
"""

from __future__ import annotations

import base64
import secrets
import shlex
from copy import copy
from pathlib import Path
from types import SimpleNamespace

from msg.atomic_file import durable_write
from msg.client import MsgClient
from msg.client_delegated import (
    accept as accept_delegation,
    acceptance_binding,
    create as create_delegation,
    prepare as prepare_delegation,
    read_json,
)
from msg.client_subagents import normalize_agent
from msg.client_subagents_remote import RemoteAgents
from msg.core.codec import canonical, loads, unb64
from msg.core.errors import Failure, require
from msg.paths import private_directory
from msg.security.crypto import key_id

CODE_PREFIX = 'msglink1.'
LEAD_SUFFIX = '-lead'
TASK_LIMIT = 16384
DEFAULT_MINUTES = 120


def add_commands(commands):
    link = commands.add_parser(
        'link', help='Agent Link: invite a helper agent into one task with scoped access.'
    )
    actions = link.add_subparsers(dest='action', required=True)
    invite = actions.add_parser('invite', help='Owner: prepare a copyable onboarding prompt.')
    invite.add_argument('name', help='Link label, e.g. reviewer; also the helper mailbox.')
    source = invite.add_mutually_exclusive_group(required=True)
    source.add_argument('--task', help='Task text shown to the helper and sent on approval.')
    source.add_argument('--task-file', type=Path)
    invite.add_argument(
        '--minutes',
        type=int,
        default=DEFAULT_MINUTES,
        help=f'Access lifetime after approval, 1-1440 (default {DEFAULT_MINUTES}).',
    )
    join = actions.add_parser('join', help='Helper: create local keys and print a join code.')
    join.add_argument('code', help='Invitation code from the onboarding prompt.')
    approve = actions.add_parser('approve', help='Owner: authorize a joined helper.')
    approve.add_argument('code', help='Join code returned by the helper.')
    approve.add_argument('--minutes', type=int, help='Override the invitation lifetime.')
    accept = actions.add_parser('accept', help='Helper: load access and accept the task.')
    accept.add_argument('code', help='Access code from the approval prompt.')
    actions.add_parser('list', help='Owner: show local link records.')
    status = actions.add_parser('status', help='Owner: show live delegated identity status.')
    status.add_argument('name')
    revoke = actions.add_parser('revoke', help='Owner: revoke access and archive both mailboxes.')
    revoke.add_argument('name')


def encode_code(kind, value):
    raw = canonical({'version': 1, 'kind': kind, **value})
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip('=')


def decode_code(code, kind):
    require(isinstance(code, str), 'invalid_link_code')
    code = ''.join(code.split())
    require(code.startswith(CODE_PREFIX) and len(code) <= 65536, 'invalid_link_code')
    body = code.removeprefix(CODE_PREFIX)
    try:
        value = loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
    except (ValueError, TypeError) as exc:
        raise Failure('invalid_link_code') from exc
    require(
        isinstance(value, dict) and type(value.get('version')) is int and value['version'] == 1,
        'invalid_link_code',
    )
    require(value.get('kind') == kind, 'link_code_kind_mismatch')
    return value


def task_id(record):
    return 'task-' + record['invite_id']


def lead_label(name):
    return name + LEAD_SUFFIX


def check_name(name, username):
    name = normalize_agent(name, username)
    require(len(lead_label(name)) <= 64 and not name.endswith(LEAD_SUFFIX), 'invalid_link_name')
    return name


def link_grants(username, name):
    """The helper's complete ceiling: this link's two private mailboxes, nothing else."""
    root = '/@' + username
    agents = root + '/files/agents'
    inbox, outbox = agents + '/' + name, agents + '/' + lead_label(name)

    def grant(capability, path, operations, descendants):
        return {
            'capability': capability,
            'version': 1,
            'scope': {'resource_id': path, 'descendants': descendants},
            'operations': operations,
            'constraints': {},
        }

    # Namespace checks read only the three parents' own metadata, never siblings.
    grants = [
        grant('discovery.basic', p, ['discovery.get@1'], False)
        for p in (root, root + '/files', agents)
    ]
    grants += [
        grant('discovery.basic', inbox, ['discovery.get@1'], True),
        grant('discovery.basic', outbox, ['discovery.get@1'], True),
        grant('resource.basic', inbox, ['file.read@1'], True),
        grant('resource.basic', outbox, ['file.create@1', 'file.read@1'], True),
        # The stream itself is authorized on the account; each event is then
        # shown only if this same operation's ceiling covers its resource.
        grant('communication.basic', root, ['communication.changes@1'], False),
        grant('communication.basic', inbox, ['communication.changes@1'], True),
        grant('communication.basic', outbox, ['communication.changes@1'], True),
    ]
    return grants


def records_directory(state):
    return private_directory(state.paths.state / 'links')


def record_path(state, name):
    return records_directory(state) / (name + '.json')


def load_record(state, name):
    path = record_path(state, name)
    require(path.exists(), 'link_not_found')
    return read_json(path)


def grant_path(state, record):
    # A completed issuance belongs to this invitation, never an older use of its label.
    return records_directory(state) / (record['name'] + '.' + record['invite_id'] + '.grant.json')


def issuance_journal(state, record):
    return state.file('link-issuance-' + record['invite_id'] + '.json')


def load_grant(state, record):
    grant = read_json(grant_path(state, record))
    request = record['approval']['request']
    require(
        isinstance(grant, dict)
        and grant.get('target_service') == state.server
        and grant.get('grantor_address') == '@' + record['owner']
        and grant.get('key_id') == key_id(unb64(request['public_key'], limit=32))
        and grant.get('encryption_recipient') == request['encryption_recipient'],
        'link_grant_mismatch',
    )
    return grant


def save_issued(state, record, grant):
    record.update(
        state='issued',
        minutes=record['approval']['minutes'],
        delegation_id=grant['delegation_id'],
        address=grant['address'],
        expires_at=grant['expires_at'],
        public_key=record['approval']['request']['public_key'],
    )
    save_record(state, record)


def save_record(state, record):
    durable_write(record_path(state, record['name']), canonical(record), mode=0o600)


def task_text(args):
    if args.task_file is not None:
        with args.task_file.open('rb') as stream:
            raw = stream.read(TASK_LIMIT + 1)
        require(len(raw) <= TASK_LIMIT, 'link_task_too_large')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise Failure('invalid_message_encoding') from None
    else:
        text = args.task
        require(len(text.encode()) <= TASK_LIMIT, 'link_task_too_large')
    require(bool(text.strip()), 'link_task_required')
    return text


def helper_command(server, name, *words):
    directory = './msg-link-' + name
    parts = ['msg', '--server', server, '--config-dir', directory, *words]
    return ' '.join(shlex.quote(part) for part in parts)


async def owner_handle(client):
    require(
        client.state.subject is not None and not client.state.data.get('delegated_identity'),
        'link_owner_required',
    )
    # The current handle comes from the server; a renamed account keeps its links.
    return await RemoteAgents(client).account_name()


def invitation_prompt(server, owner, name, task, minutes, code):
    join = helper_command(server, name, 'link', 'join', code)
    return '\n'.join([
        f'You are invited by @{owner} on {server} to help with one task through MSG Agent Link.',
        '',
        'Task:',
        task.strip(),
        '',
        'Nothing is authorized yet. If you decide to help:',
        '',
        '1. Install the MSG client if `msg` is missing:',
        f'   curl -fsSL {server}/install | bash',
        '2. Create local keys for this task and print a join code:',
        f'   {join}',
        f'3. Send the printed join code back to @{owner} through the same channel as this prompt.',
        '',
        f'After approval you will receive an access code. Access lasts {minutes} minutes and',
        f'covers only the private mailboxes @{owner}#{name} and @{owner}#{lead_label(name)}.',
        'Your private keys stay in that config directory. Never paste them anywhere.',
        'This prompt contains no credentials; reading it grants nothing.',
    ])


def access_prompt(server, owner, name, expires_at, code):
    helper = helper_command(server, name) + ' --agent ' + name
    lead = shlex.quote(f'@{owner}#{lead_label(name)}')
    return '\n'.join([
        f'@{owner} approved your Agent Link access on {server} until {expires_at}.',
        '',
        'Accept the task explicitly; this prints the task from your private inbox:',
        f'   {helper_command(server, name, "link", "accept", code)}',
        '',
        'Then report progress and results, and wait for follow-up messages:',
        f'   {helper} agent send {lead} "progress or result" --remote',
        f'   {helper} listen --remote',
        '',
        'The listener starts at this approval, so it never scans older account history.',
        'The access code holds public references only, but describes the granted scope; keep it',
        'in this private channel. Access ends at expiry or when the owner revokes it.',
    ])


def owner_commands(owner, name):
    lead, helper = lead_label(name), shlex.quote(f'@{owner}#{name}')
    return {
        'listen': f'msg --agent {lead} listen --remote',
        'send': f'msg --agent {lead} agent send {helper} "message" --remote',
        'status': f'msg link status {name}',
        'revoke': f'msg link revoke {name}',
    }


def seed_listener(client, label, cursor):
    """Point the default `--agent LABEL listen --remote` checkpoint at a fresh tail."""
    from msg.client_agent_cli import cursor_path
    from msg.client_listener import seed_checkpoint

    state = client.state
    options = SimpleNamespace(cursor_file=None, agent=label, event=[])
    context = {'server': state.server, 'subject': state.subject, 'source': 'remote', 'agent': label}
    return seed_checkpoint(cursor_path(state, options, 'remote'), context, cursor)


async def invite(client, args):
    state = client.state
    owner = await owner_handle(client)
    name = check_name(args.name, owner)
    require(0 < args.minutes <= 1440, 'invalid_delegation_minutes')
    task = task_text(args)
    # A name keeps its archived mailboxes and history; a new link takes a new name.
    require(not record_path(state, name).exists(), 'link_exists')
    record = {
        'version': 1,
        'name': name,
        'server': state.server,
        'owner': owner,
        'invite_id': secrets.token_hex(16),
        'task': task,
        'minutes': args.minutes,
        'state': 'invited',
    }
    save_record(state, record)
    code = encode_code(
        'invite',
        {
            'server': state.server,
            'grantor': '@' + owner,
            'name': name,
            'invite': record['invite_id'],
        },
    )
    return {
        'status': 'ok',
        'data': {'name': name, 'state': 'invited', 'invite_code': code},
        'prompt': invitation_prompt(state.server, owner, name, task, args.minutes, code),
    }


async def join(client, args):
    state = client.state
    code = decode_code(args.code, 'invite')
    require(code.get('server') == state.server, 'wrong_service')
    grantor, name, invite_id = code.get('grantor'), code.get('name'), code.get('invite')
    require(isinstance(grantor, str) and grantor.startswith('@'), 'invalid_link_code')
    require(isinstance(invite_id, str) and len(invite_id) == 32, 'invalid_link_code')
    name = check_name(name, grantor[1:])
    saved = state.data.get('agent_link')
    link = {'grantor': grantor, 'name': name, 'invite': invite_id}
    require(saved is None or saved == link, 'link_profile_in_use')
    request = prepare_delegation(client, grantor)
    if saved is None:
        state.data['agent_link'] = link
        state._save()
    join_code = encode_code('join', {'name': name, 'invite': invite_id, 'request': request})
    return {
        'status': 'ok',
        'data': {'name': name, 'grantor': grantor, 'join_code': join_code},
        'prompt': '\n'.join([
            f'Send this join code to {grantor}. It contains only public keys and a signed proof:',
            '',
            join_code,
        ]),
    }


async def approve(client, args):
    state = client.state
    code = decode_code(args.code, 'join')
    owner = await owner_handle(client)
    name = check_name(code.get('name'), owner)
    record = load_record(state, name)
    require(record['invite_id'] == code.get('invite'), 'link_invite_mismatch')
    require(record['state'] in {'invited', 'issued', 'approved'}, 'link_revoked')
    request = code.get('request')
    require(
        isinstance(request, dict) and request.get('grantor') == '@' + owner,
        'delegated_grantor_mismatch',
    )
    require(
        request.get('target_service') == state.server
        and all(k in request for k in ('public_key', 'encryption_recipient', 'possession_proof')),
        'invalid_link_code',
    )
    approval = record.get('approval')
    minutes = (approval or record)['minutes'] if args.minutes is None else args.minutes
    require(0 < minutes <= 1440, 'invalid_delegation_minutes')
    if approval is not None:
        require(approval['request'] == request, 'link_already_approved')
        require(approval['minutes'] == minutes, 'link_approval_pending')
    grant_file = grant_path(state, record)
    if record['state'] == 'invited':
        mailboxes = RemoteAgents(client)
        for label in (name, lead_label(name)):
            await mailboxes.create(label)
        # Tails are taken before any link message exists, so both listeners
        # see the task, acceptance and every later message, but no history.
        tails = record.get('tails') or {
            label: (await mailboxes.inbox(label, tail=True))['cursor']
            for label in (name, lead_label(name))
        }
        record['tails'] = tails
        record['approval'] = {'request': request, 'minutes': minutes}
        save_record(state, record)
        if not grant_file.exists():
            try:
                await create_delegation(
                    client,
                    request,
                    link_grants(owner, name),
                    minutes=minutes,
                    output=grant_file,
                    journal=issuance_journal(state, record),
                )
            except Failure:
                if not issuance_journal(state, record).exists() and not grant_file.exists():
                    # A definitive rejection issued nothing. A corrected join
                    # code can be approved; uncertain attempts stay bound.
                    record.pop('approval', None)
                    save_record(state, record)
                raise
        grant = load_grant(state, record)
        # Save the revocation handle before sending anything. If this write fails,
        # the invitation-bound grant file still prevents a second issuance.
        save_issued(state, record, grant)
    else:
        grant = load_grant(state, record)
    if record['state'] == 'issued':
        # The first message is the task, idempotent under the invitation's ID.
        mailboxes = RemoteAgents(client)
        await mailboxes.send(lead_label(name), name, record['task'], message_id=task_id(record))
        record['state'] = 'approved'
        save_record(state, record)
    # Existing checkpoints win; this also resumes an interrupted initial seeding.
    seed_listener(client, lead_label(name), record['tails'][lead_label(name)])
    access = encode_code(
        'access',
        {
            'name': name,
            'invite': record['invite_id'],
            'grant': grant,
            'cursor': record['tails'][name],
        },
    )
    return {
        'status': 'ok',
        'data': {
            'name': name,
            'state': 'approved',
            'address': record['address'],
            'expires_at': record['expires_at'],
            'access_code': access,
            'commands': owner_commands(owner, name),
        },
        'prompt': access_prompt(state.server, owner, name, record['expires_at'], access),
    }


async def accept(client, args):
    state = client.state
    code = decode_code(args.code, 'access')
    link = state.data.get('agent_link')
    require(isinstance(link, dict), 'link_join_required')
    require(
        link.get('invite') == code.get('invite') and link.get('name') == code.get('name'),
        'link_invite_mismatch',
    )
    require(isinstance(code.get('cursor'), str), 'invalid_link_code')
    name = link['name']
    grant = code.get('grant')
    binding = acceptance_binding(client, grant)
    # Verify the candidate certificate against the known task using these same
    # prepared keys. A bad access code must leave the original profile usable.
    candidate_state = copy(state)
    candidate_state.data = {**state.data, **binding}
    candidate = MsgClient(
        candidate_state, client.transport, clock=client.clock, retries=client.retries
    )
    try:
        cursor = loads(unb64(code['cursor'], limit=65536))
        require(
            isinstance(cursor, dict)
            and type(cursor.get('version')) is int
            and cursor['version'] == 1
            and cursor.get('scope')
            == {'server': state.server, 'owner': candidate_state.subject, 'agent': name}
            and isinstance(cursor.get('sync_cursor'), str),
            'invalid_link_code',
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise Failure('invalid_link_code') from exc
    # The task reference is known, so read it directly rather than scanning.
    task = await RemoteAgents(candidate).read(name, 'task-' + link['invite'])
    page = candidate.checked(
        await candidate.call('communication.changes', {'cursor': cursor['sync_cursor'], 'limit': 1})
    )
    require(not page.data.get('resync_required'), 'resync_required')
    accepted = accept_delegation(client, grant)
    mailbox = RemoteAgents(client)
    await mailbox.send(
        name,
        lead_label(name),
        f'Accepted the task as {accepted["address"]}.',
        message_id='accept-' + link['invite'],
    )
    seed_listener(client, name, code['cursor'])
    link['accepted'] = True
    state.data['agent_link'] = link
    state._save()
    return {
        'status': 'ok',
        'data': {**accepted, 'name': name, 'task': task},
        'prompt': '\n'.join([
            f'Accepted as {accepted["address"]} until {accepted["expires_at"]}.',
            '',
            f'Task from {task["from"]}:',
            task['message'],
        ]),
    }


def list_links(client):
    records = []
    for path in sorted(records_directory(client.state).glob('*.json')):
        if path.name.endswith('.grant.json'):
            continue
        record = read_json(path)
        records.append({
            key: record[key]
            for key in ('name', 'state', 'address', 'expires_at', 'minutes')
            if key in record
        })
    return {'status': 'ok', 'data': {'items': records}}


async def revoke(client, args):
    state = client.state
    owner = await owner_handle(client)
    name = check_name(args.name, owner)
    record = load_record(state, name)
    if 'delegation_id' not in record and record.get('approval'):
        if grant_path(state, record).exists():
            save_issued(state, record, load_grant(state, record))
        else:
            # An uncertain issuance must be recovered with its saved request ID
            # before revocation can claim the authority has ended.
            require(not issuance_journal(state, record).exists(), 'link_issuance_pending')
    if 'delegation_id' in record:
        # Revocation is idempotent, so an interrupted revoke can simply be rerun.
        client.checked(
            await client.call('identity.delegation_revoke', {'id': record['delegation_id']})
        )
        mailboxes = RemoteAgents(client)
        for label in (name, lead_label(name)):
            await mailboxes.archive(label)
    record['state'] = 'revoked'
    save_record(state, record)
    return {'status': 'ok', 'data': {'name': name, 'state': 'revoked'}}


async def run_command(client, args):
    if args.action == 'invite':
        return await invite(client, args)
    if args.action == 'join':
        return await join(client, args)
    if args.action == 'approve':
        return await approve(client, args)
    if args.action == 'accept':
        return await accept(client, args)
    if args.action == 'list':
        return list_links(client)
    if args.action == 'status':
        record = load_record(client.state, check_name(args.name, await owner_handle(client)))
        require('delegation_id' in record, 'link_not_approved')
        return await client.call('identity.delegated_get', {'id': '/' + record['address']})
    return await revoke(client, args)
