"""SSH-style connections. Quoted commands are MSG arguments, never shell code."""

import fnmatch
import os
import re
import shlex
import stat
from pathlib import Path

from msg.core.errors import Failure, require
from msg.paths import xdg_directory
from msg.service_origin import service_origin

OPTIONS = {'hostname', 'user', 'port', 'scheme', 'identityfile', 'transport'}


def host_options(host, filename=None):
    path = (
        Path(filename).expanduser()
        if filename
        else xdg_directory('XDG_CONFIG_HOME', '.config') / 'msg/config'
    )
    if not path.exists() and not path.is_symlink():
        require(filename is None, 'connection_config_missing')
        return {}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Failure('unsafe_connection_config') from None
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_mode & 0o022 == 0,
            'unsafe_connection_config',
        )
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as stream:
            text = stream.read(65537)
        require(len(text.encode()) <= 65536, 'connection_config_too_large')
    finally:
        os.close(fd)
    options = {}
    active = True
    for number, line in enumerate(text.splitlines(), 1):
        try:
            fields = shlex.split(line, comments=True)
        except ValueError:
            raise Failure('invalid_connection_config', details={'line': number}) from None
        if not fields:
            continue
        name, separator, value = fields[0].partition('=')
        values = ([value] if separator and value else []) + fields[1:]
        if values and values[0] == '=':
            values = values[1:]
        name = name.lower()
        if name == 'host':
            require(bool(values), 'invalid_connection_config', details={'line': number})
            positive = [pattern.lower() for pattern in values if not pattern.startswith('!')]
            negative = [pattern[1:].lower() for pattern in values if pattern.startswith('!')]
            active = any(
                fnmatch.fnmatchcase(host.lower(), pattern) for pattern in positive
            ) and not any(fnmatch.fnmatchcase(host.lower(), pattern) for pattern in negative)
            continue
        require(
            name in OPTIONS and len(values) == 1 and bool(values[0]),
            'unsupported_connection_config',
            details={'line': number},
        )
        if active:
            options.setdefault(name, values[0])
    return options


def expand_connection_args(argv, commands):
    argv = list(argv)
    prefix = []
    explicit = {}
    values = {
        '-F': 'connection_config',
        '--connection-config': 'connection_config',
        '-l': 'user',
        '--user': 'user',
        '-p': 'port',
        '--port': 'port',
        '-i': 'identityfile',
        '--key': 'identityfile',
        '--server': 'server',
        '--transport': 'transport',
        '--config-dir': 'config_dir',
        '--profile': 'profile',
        '--migrate-from': 'migrate_from',
        '--certificate': 'certificate',
        '--as-subject': 'as_subject',
    }
    index = 0
    while index < len(argv) and argv[index].startswith('-'):
        flag, equals, value = argv[index].partition('=')
        if flag not in values:
            return argv
        prefix.append(argv[index])
        if not equals:
            if index + 1 >= len(argv):
                return argv
            index += 1
            value = argv[index]
            prefix.append(value)
        explicit[values[flag]] = value
        index += 1
    if index >= len(argv) or argv[index] in commands:
        require('port' not in explicit, 'port_requires_connection_target')
        require('connection_config' not in explicit, 'config_requires_connection_target')
        return argv
    target = argv[index]
    require(target.count('@') <= 1, 'invalid_connection_target')
    user, separator, host = target.rpartition('@')
    if not separator:
        host, user = target, None
    require(bool(host) and (not separator or bool(user)), 'invalid_connection_target')
    chosen = {**host_options(host, explicit.get('connection_config')), **explicit}
    chosen['user'] = user or chosen.get('user')
    if chosen['user'] is not None:
        require(
            re.fullmatch(r'[a-z][a-z0-9-]{1,40}', chosen['user']) is not None,
            'invalid_connection_user',
        )
    hostname = chosen.get('hostname', host).replace('%h', host)
    require(
        '%' not in hostname and '/' not in hostname and '@' not in hostname,
        'invalid_connection_target',
    )
    port = chosen.get('port')
    if port is not None:
        require(
            str(port).isascii() and str(port).isdecimal() and 1 <= int(port) <= 65535,
            'invalid_connection_port',
        )
        require(not hostname.startswith('[') or hostname.endswith(']'), 'invalid_connection_target')
        hostname += ':' + str(int(port))
    origin = service_origin(f'{chosen.get("scheme", "https")}://{hostname}')
    if chosen.get('server'):
        require(service_origin(chosen['server']) == origin, 'connection_server_conflict')
    generated = ['--server', origin]
    for name, flag in [('user', '--user'), ('transport', '--transport')]:
        if chosen.get(name) is not None:
            generated += [flag, chosen[name]]
    if chosen.get('identityfile'):
        key = (
            chosen['identityfile']
            .replace('%h', host)
            .replace('%r', chosen['user'] or '')
            .replace('%d', str(Path.home()))
        )
        require('%' not in key, 'invalid_connection_identity_file')
        generated += ['--key', str(Path(key).expanduser())]
    command = argv[index + 1 :]
    if len(command) == 1:
        try:
            command = shlex.split(command[0])
        except ValueError:
            raise Failure('invalid_connection_command') from None
    if command and command[0] == 'msg':
        command = command[1:]
    require(not command or command[0] in commands, 'unknown_connection_command')
    return prefix + generated + (command or ['tui'])
