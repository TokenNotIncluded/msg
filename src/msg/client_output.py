"""Read field selection comes from the exact server Registry contract."""

import asyncio
import re
import shutil
import tempfile
from collections.abc import Mapping
from contextlib import suppress

from msg.core.codec import canonical
from msg.core.errors import Failure, require


async def json_fields(client, operation, version, arguments, selection):
    """Return signed-query arguments or a discovery/error result, never both."""
    require('fields' not in arguments and 'cursor' not in arguments, 'json_query_conflict')
    require(type(version) is int and version > 0, 'invalid_operation_version')
    response = await client.call('discovery.schema', {'operation': f'{operation}@{version}'})
    if response.status != 'ok':
        return None, response
    data = response.data
    require(isinstance(data, Mapping), 'invalid_read_response')
    spec = data.get('operation')
    require(isinstance(spec, Mapping) and spec.get('effect') == 'read', 'json_read_required')
    schema = data.get('input')
    require(isinstance(schema, Mapping), 'json_fields_unavailable')
    properties = schema.get('properties')
    require(isinstance(properties, Mapping), 'json_fields_unavailable')
    field = properties.get('fields')
    require(isinstance(field, Mapping), 'json_fields_unavailable')
    items = field.get('items')
    require(isinstance(items, Mapping), 'json_fields_unavailable')
    available = items.get('enum')
    require(
        isinstance(available, (list, tuple))
        and available
        and all(isinstance(name, str) and name for name in available),
        'json_fields_unavailable',
    )
    if selection == '':
        return None, {'status': 'ok', 'data': {'fields': list(available)}}
    chosen = selection.split(',')
    require(
        len(chosen) == len(set(chosen)) and set(chosen) <= set(available),
        'invalid_json_fields',
    )
    return {**arguments, 'fields': chosen}, None


MAX_FORMAT_BYTES = 1048576
MAX_EXPRESSION_BYTES = 4096
_POINTER = re.compile(r'\{\{(/[^{}]*)?\}\}')


def _bounded(value, maximum=MAX_FORMAT_BYTES):
    require(len(value.encode('utf-8')) <= maximum, 'output_format_too_large')
    return value


def render_template(template, data):
    """Replace JSON-pointer placeholders with JSON values; never evaluate text."""
    _bounded(template, MAX_EXPRESSION_BYTES)
    parts = []
    offset = 0
    size = 0
    for match in _POINTER.finditer(template):
        literal = template[offset : match.start()]
        require('{{' not in literal and '}}' not in literal, 'invalid_output_template')
        value = data
        pointer = match.group(1)
        for token in pointer.split('/')[1:] if pointer is not None else ():
            require(not re.search(r'~(?![01])', token), 'invalid_output_template')
            key = token.replace('~1', '/').replace('~0', '~')
            if isinstance(value, Mapping):
                require(key in value, 'invalid_output_template')
                value = value[key]
            elif isinstance(value, (list, tuple)):
                require(
                    re.fullmatch(r'0|[1-9][0-9]{0,8}', key) and int(key) < len(value),
                    'invalid_output_template',
                )
                value = value[int(key)]
            else:
                raise Failure('invalid_output_template')
        replacement = canonical(value).decode()
        size += len((literal + replacement).encode())
        require(size <= MAX_FORMAT_BYTES, 'output_format_too_large')
        parts.extend((literal, replacement))
        offset = match.end()
    tail = template[offset:]
    require('{{' not in tail and '}}' not in tail, 'invalid_output_template')
    parts.append(tail)
    return _bounded(''.join(parts))


async def render_jq(query, data, *, timeout=2):
    """Run optional jq on one returned JSON value, with no inherited secrets."""
    _bounded(query, MAX_EXPRESSION_BYTES)
    # Deliberately conservative, including quoted occurrences: jq modules may
    # load filesystem data. This interface supports filters, not module loading.
    require(not re.search(r'\b(?:import|include|modulemeta)\b', query), 'invalid_jq_query')
    executable = shutil.which('jq')
    require(executable is not None, 'jq_unavailable')
    payload = canonical(data) + b'\n'
    require(len(payload) <= MAX_FORMAT_BYTES, 'output_format_too_large')
    with tempfile.TemporaryDirectory(prefix='msg-jq-') as directory:
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                '-c',
                '-M',
                '-L',
                directory,
                '--',
                query,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=directory,
                env={},
            )
        except OSError:
            raise Failure('jq_unavailable') from None

        async def feed():
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except BrokenPipeError, ConnectionResetError:
                pass
            finally:
                process.stdin.close()
                with suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.wait_closed()

        async def collect():
            chunks = []
            size = 0
            while block := await process.stdout.read(65536):
                size += len(block)
                require(size <= MAX_FORMAT_BYTES, 'output_format_too_large')
                chunks.append(block)
            return b''.join(chunks)

        tasks = [
            asyncio.create_task(feed()),
            asyncio.create_task(collect()),
            asyncio.create_task(process.wait()),
        ]
        try:
            _, output, status = await asyncio.wait_for(asyncio.gather(*tasks), timeout)
            require(status == 0, 'invalid_jq_query')
            try:
                return output.decode('utf-8')
            except UnicodeError:
                raise Failure('invalid_jq_output') from None
        except TimeoutError:
            raise Failure('output_format_timeout') from None
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await process.wait()
