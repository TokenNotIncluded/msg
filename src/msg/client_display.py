"""Terminal presentation without changing the JSON protocol or streaming output."""

import json
import sys
import unicodedata
from collections.abc import Mapping

from msg.core.codec import canonical


def safe_text(value, *, single_line=False):
    text = str(value)
    return ''.join(
        char
        if (char in '\n\t' and not single_line) or not unicodedata.category(char).startswith('C')
        else f'\\u{ord(char):04x}'
        for char in text
    )


def width(value):
    return sum(2 if unicodedata.east_asian_width(char) in {'W', 'F'} else 1 for char in value)


def table(headers, rows):
    cells = [[safe_text(cell, single_line=True) for cell in row] for row in [headers, *rows]]
    sizes = [max(width(row[index]) for row in cells) for index in range(len(headers))]
    return '\n'.join(
        '  '.join(
            cell + ' ' * (size - width(cell)) for cell, size in zip(row, sizes, strict=True)
        ).rstrip()
        for row in cells
    )


ERROR_HINTS = {
    'server_required': 'Set a default with: msg server use https://your-server.example',
    'local_account_not_found': 'List local accounts with: msg account list',
    'local_account_not_authenticated': 'Register or sign in with the selected account first.',
    'credential_ceiling': 'The selected credential does not cover this operation.',
    'connection_user_mismatch': 'Select the matching local identity with --account NAME.',
    'secure_channel_required': 'Use HTTPS POST; do not put credentials in a URL.',
}


def render_text(value, *, context=None, identity=None):
    if not isinstance(value, Mapping):
        return safe_text(json.dumps(value, ensure_ascii=False, indent=2))
    if value.get('status') == 'error':
        error = value.get('error') or {}
        code = error.get('code', 'unknown_error')
        lines = [f'Error: {safe_text(code)}']
        hint = (
            'The approval code has expired, was already handled, or is invalid. '
            'Open the sign-in page again and approve its new code.'
            if code == 'invalid_grant' and context == 'auth_approval'
            else ERROR_HINTS.get(code) or error.get('message')
        )
        if hint:
            lines.append(safe_text(hint))
        if value.get('operation'):
            lines.append(f'Operation: {safe_text(value["operation"])}')
        if value.get('request_id'):
            lines.append(f'Request: {safe_text(value["request_id"])}')
        if context == 'auth_approval' and identity:
            lines.extend(identity_lines(identity))
        return '\n'.join(lines)
    if context == 'auth_approval':
        data = value.get('data') or {}
        lines = [f'Authorization: {safe_text(data.get("status", value.get("status", "unknown")))}']
        if identity:
            lines.extend(identity_lines(identity))
        if data.get('kind') == 'login' and data.get('status') == 'approved':
            lines.append('Return to this browser sign-in page to finish signing in.')
        if data.get('scopes'):
            lines.append(f'Access: {safe_text(" ".join(data["scopes"]))}')
        return '\n'.join(lines)
    if context == 'link' and isinstance(value.get('prompt'), str):
        # Onboarding prompts are meant to be copied verbatim to another agent.
        return safe_text(value['prompt'])
    if context == 'server':
        lines = [f'Default server: {safe_text(value["default_server"] or "(not set)")}']
        if value['source'] != 'saved':
            lines.append(f'Current server: {safe_text(value["server"])} ({value["source"]})')
        if value.get('profile'):
            lines.append(f'Profile: {safe_text(value["profile"])}')
        if not value['default_server']:
            lines.append('Set one with: msg server use https://your-server.example')
        return '\n'.join(lines)
    if context == 'account':
        lines = [f'Server: {safe_text(value["server"])}']
        if 'accounts' in value:
            rows = [
                (
                    '*' if item['selected'] else '',
                    item['account'],
                    '@' + item['handle'] if item.get('handle') else '-',
                    item.get('signer') or '-',
                    item.get('subject_id') or '-',
                )
                for item in value['accounts']
            ]
            lines.append(table(('DEFAULT', 'ACCOUNT', 'HANDLE', 'SIGNER', 'SUBJECT'), rows))
            lines.append(
                '\n* = default account. Switch with: msg account use NAME'
                if rows
                else '\nNo local accounts. Register with: msg --account NAME identity new NAME'
            )
        else:
            lines.append(f'Account: {safe_text(value["account"])}')
            if value.get('handle'):
                lines.append(f'Identity: @{safe_text(value["handle"])}')
        return '\n'.join(lines)
    lines = []
    if 'status' in value:
        lines.append('OK' if value['status'] == 'ok' else safe_text(value['status']))
    for key, label in (
        ('operation', 'Operation'),
        ('subject', 'Identity'),
        ('request_id', 'Request'),
    ):
        if value.get(key):
            lines.append(f'{label}: {safe_text(value[key])}')
    data = value.get('data', value)
    if isinstance(data, Mapping) and isinstance(data.get('content'), str):
        if data.get('path'):
            lines.append(safe_text(data['path']))
        if data.get('revision'):
            lines.append(f'Revision: {safe_text(data["revision"])}')
        lines.append('\n' + safe_text(data['content']))
    elif isinstance(data, Mapping) and isinstance(data.get('items'), list):
        items = data['items']
        if all(isinstance(item, Mapping) for item in items):
            lines.append(
                table(
                    ('TYPE', 'PATH / NAME', 'ID'),
                    [
                        (
                            item.get('type', '-'),
                            item.get('path') or item.get('name', '-'),
                            item.get('id', '-'),
                        )
                        for item in items
                    ],
                )
            )
            for key in ('cursor', 'continuation', 'page_info'):
                if data.get(key):
                    lines.append(f'{key}: {safe_text(json.dumps(data[key], ensure_ascii=False))}')
        else:
            lines.append(safe_text(json.dumps(data, ensure_ascii=False, indent=2)))
    elif data:
        lines.append(safe_text(json.dumps(data, ensure_ascii=False, indent=2)))
    for resource in value.get('resources', []):
        lines.append(f'Resource: {safe_text(resource["id"])}')
        if resource.get('revision'):
            lines.append(f'Revision: {safe_text(resource["revision"])}')
    return '\n'.join(lines) or '{}'


def identity_lines(identity):
    handle = identity.get('handle')
    return [
        f'Account: {safe_text(identity.get("account") or "(portable identity)")}',
        f'Identity: {safe_text("@" + handle if handle else identity.get("subject_id") or "(not registered)")}',
        f'Server: {safe_text(identity["server"])}',
    ]


def print_result(value, args=None, *, context=None, identity=None, stream=None, raw=False):
    stream = sys.stdout if stream is None else stream
    mode = getattr(args, 'output_format', 'auto')
    text = mode == 'text' or mode == 'auto' and stream.isatty() and not raw
    print(
        render_text(value, context=context, identity=identity)
        if text
        else canonical(value).decode(),
        file=stream,
    )
