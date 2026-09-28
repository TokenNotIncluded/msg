"""Small, line-oriented terminal navigator over the public read contracts.

The UI deliberately has no mutation command.  It never interprets links or
resource text as terminal control sequences or as operations to execute.
"""
from __future__ import annotations

import re
import shutil
import sys
import unicodedata
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

from msg.core.errors import Failure


READ_OPERATIONS = frozenset({
    'discovery.get', 'discovery.read_query', 'discovery.lexical_search',
    'communication.inbox', 'communication.outbox', 'communication.following', 'discussion.thread',
    'identity.note_list', 'identity.note_get', 'identity.todo_list', 'identity.todo_get',
})
_CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f]')


def safe_text(value):
    """Treat all server text as data, including ANSI/OSC and bidi controls."""
    if value is None:
        return ''
    if not isinstance(value, str):
        value = str(value)
    return ''.join(' ' if _CONTROL.fullmatch(char) or char in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069'
                   else char for char in value)


def terminal_lines(value, width):
    """Preserve paragraphs and conservatively wrap wide/combining characters.

    Emoji join sequences can occupy fewer cells than this conservative count;
    we prefer an early wrap over writing beyond a narrow terminal's boundary.
    """
    for paragraph in ('' if value is None else str(value)).split('\n'):
        line = ''
        used = 0
        for token in re.findall(r'[!-~]+|[^!-~]', safe_text(paragraph)):
            # Keep a URL/error code/ASCII word intact when it fits on one line.
            if token.isascii() and 1 < len(token) <= width and used + len(token) > width:
                yield line.rstrip()
                line, used = '', 0
            for char in token:
                cells = (0 if unicodedata.combining(char) or unicodedata.category(char) == 'Cf'
                         else 2 if unicodedata.east_asian_width(char) in {'W', 'F'} else 1)
                if cells > width:
                    char, cells = '?', 1
                if cells and used + cells > width:
                    yield line.rstrip()
                    line, used = '', 0
                line += char
                used += cells
        yield line.rstrip()


HELP = ('命令：h Home，id 身份，topics 话题，following 关注，i Inbox，o Outbox，notes 笔记，'
        'todos 待办，files 文件，groups 组织，credentials 本地凭据，'
        's <scope> <terms> 搜索，t <id> 线程，r <id> 读取，'
        'n 下一页，retry 重新读取，q 退出。')


def service_origin(value):
    """The connection URL may be misconfigured; never display its credentials."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            return '（未配置）'
        host = '[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname
        port = ':' + str(parsed.port) if parsed.port is not None else ''
        return parsed.scheme + '://' + host + port
    except (TypeError, ValueError):
        return '（无效地址）'


class TerminalUI:
    def __init__(self, client, *, stdin=None, stdout=None, width=None):
        self.client = client
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.width = width
        self.page = None
        self._last_read = None
        self._read_context = None

    def _context(self):
        state = self.client.state
        # Public identifiers only; do not inspect the token, signer or journals.
        return (getattr(state, 'subject', None), getattr(state, 'server', None),
                tuple(getattr(state, 'certificates', ())))

    def _remember_read(self, function, *arguments):
        self.page = None
        self._last_read = (function, arguments)
        self._read_context = self._context()

    def _discard_stale_read(self):
        if self._read_context is not None and self._read_context != self._context():
            self.page = self._last_read = self._read_context = None
            self._write('身份或服务已变化；请重新选择视图。')
            return True
        return False

    def _write(self, value=''):
        columns = self.width if self.width is not None else shutil.get_terminal_size((80, 24)).columns
        width = max(1, min(columns, 160))
        for line in terminal_lines(value, width):
            self.stdout.write(line + '\n')
        self.stdout.flush()

    async def _read(self, operation, arguments):
        if operation not in READ_OPERATIONS:
            raise ValueError('TUI only supports read operations')
        try:
            result = await self.client.call(operation, arguments)
            if self._discard_stale_read():
                return None
        except (Failure, OSError, TimeoutError, httpx.HTTPError) as exc:
            self.page = None
            code = exc.code if isinstance(exc, Failure) else 'transport_unavailable'
            self._read_error(code)
            return None
        if result.status != 'ok':
            self.page = None
            self._read_error(getattr(getattr(result, 'error', None), 'code', None))
            return None
        if not isinstance(result.data, Mapping):
            return self._invalid_response()
        return result.data

    def _invalid_response(self):
        # A server/proxy that breaks the read contract must not crash the loop
        # or leave the previous page selectable.
        self.page = None
        self._read_error('invalid_read_response')
        return None

    def _read_error(self, code):
        # Exception text/URL/details may contain credentials. Only contract codes
        # reach the terminal; a retry always re-enters the signed read contract.
        if not isinstance(code, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,95}', code):
            code = 'read_failed'
        self._write('读取失败：' + code)
        self._write('retry 重新读取；不自动提交 ACK 或其他写入。')

    @staticmethod
    def _item_id(item):
        ref = item.get('ref') or item.get('resource')
        for rid in (item.get('id'), ref.get('id') if isinstance(ref, Mapping) else None,
                    item.get('conversation_id')):
            if isinstance(rid, str) and rid:
                return rid
        return None

    def _show_items(self, items):
        for index, item in enumerate(items, 1):
            rid = self._item_id(item) or ''
            name = item.get('title') or item.get('name') or item.get('path') or item.get('source') or rid
            self._write(f'{index}. {name}  {rid}')
            snippet = item.get('snippet')
            if isinstance(snippet, dict) and snippet.get('text'):
                self._write('   ' + snippet['text'])

    async def _show_page(self, operation, arguments, title):
        # A cursor may become invalid after a grant changes. Never keep the
        # previous page selectable after a failed permission recheck.
        self._remember_read(self._show_page, operation, dict(arguments), title)
        data = await self._read(operation, arguments)
        if data is None:
            return
        items = data.get('items', [])
        cursor = data.get('next_after_name') if operation == 'identity.todo_list' else data.get('cursor')
        if (not isinstance(items, (list, tuple)) or not all(isinstance(item, Mapping) for item in items)
                or not (cursor is None or isinstance(cursor, str))):
            self._invalid_response()
            return
        self.page = {'operation': operation, 'arguments': arguments, 'cursor': cursor,
                     'title': title, 'items': items}
        self._write(title)
        if items:
            self._show_items(items)
        else:
            self._write('（空）')
        if self.page['cursor']:
            self._write('n 下一页')

    async def home(self):
        subject = getattr(self.client.state, 'subject', None)
        self._write('msg  ' + (subject or '未登录（公开内容）'))
        await self._show_page('discovery.read_query', {'parent': '/', 'limit': 20}, 'Home')

    def identity(self):
        state = self.client.state
        subject = getattr(state, 'subject', None)
        self._write('身份：' + (subject or '未登录；可以浏览公开内容'))
        self._write('服务：' + service_origin(getattr(state, 'server', '')))
        if subject:
            self._write('身份管理请使用 msg identity；TUI 不接收密钥或令牌。')

    async def inbox(self):
        await self.private_page('communication.inbox', {'limit': 20}, 'Inbox')

    async def private_page(self, operation, arguments, title):
        if not getattr(self.client.state, 'subject', None):
            self.page = self._last_read = self._read_context = None
            self._write(title + ' 需要已登录身份。')
            return
        await self._show_page(operation, arguments, title)

    async def files(self):
        subject = getattr(self.client.state, 'subject', None)
        self.page = None
        if not subject:
            self._last_read = self._read_context = None
            self._write('Files 需要已登录身份。')
            return
        self._remember_read(self.files)
        profile = await self._read('discovery.get', {'id': subject, 'fields': ['path']})
        if profile is not None and not (isinstance(profile.get('path'), str)
                                        and profile['path'].startswith('/')):
            self._invalid_response()
        elif profile is not None:
            await self._show_page('discovery.read_query',
                                 {'parent': profile['path'] + '/files', 'limit': 20}, 'Files')

    def credentials(self):
        self.identity()
        # Never inspect or stringify ClientState.data, signer, token or journals.
        self._write('本地证书 ID（在线有效性由每次请求重新检查）：')
        for certificate in getattr(self.client.state, 'certificates', ()):
            self._write(certificate)

    async def search(self, scope, terms):
        if not scope or not terms:
            self._write('用法：s <scope> <terms>')
            return
        await self._show_page('discovery.lexical_search',
                              {'scope': scope, 'terms': terms, 'limit': 20,
                               'snippet': True}, '搜索')

    async def thread(self, rid):
        if not rid:
            self._write('用法：t <resource-id>')
            return
        await self._show_page('discussion.thread', {'id': rid, 'limit': 20}, '线程 ' + rid)

    async def read(self, rid):
        if not rid:
            self._write('用法：r <resource-id>')
            return
        await self._document('discovery.get', {'id': rid}, rid)

    async def _document(self, operation, arguments, title):
        self._remember_read(self._document, operation, dict(arguments), title)
        data = await self._read(operation, arguments)
        if data is None:
            return
        self._write(data.get('path') or data.get('name') or title)
        content = data.get('content')
        if isinstance(content, str):
            self._write(content)
        elif operation == 'identity.todo_get':
            self._write(data.get('title', ''))
            self._write(data.get('description', ''))
            self._write('状态：' + str(data.get('status', '')))
        else:
            self._write('（无文本正文）')
        self._write('读取不会发送 ACK。')

    async def next_page(self):
        if self._discard_stale_read():
            return
        if not self.page or not self.page['cursor']:
            self._write('没有下一页。')
            return
        current = self.page
        arguments = ({'cursor': current['cursor']} if current['operation'] in
                     {'discovery.read_query', 'discovery.lexical_search', 'communication.following'} else
                     {**current['arguments'],
                      'after_name' if current['operation'] == 'identity.todo_list' else 'cursor': current['cursor']})
        await self._show_page(current['operation'], arguments, current['title'])

    async def command(self, raw):
        self._discard_stale_read()
        command = raw.strip()
        if command in {'q', 'quit', 'exit'}:
            return False
        if command in {'h', 'home', ''}:
            await self.home()
        elif command in {'id', 'identity'}:
            self.identity()
        elif command in {'credentials', 'creds'}:
            self.credentials()
        elif command == 'topics':
            await self._show_page('discovery.read_query', {'type': 'topic', 'limit': 20}, 'Topics')
        elif command == 'groups':
            await self._show_page('discovery.read_query', {'type': 'organization', 'limit': 20}, 'Groups')
        elif command in {'o', 'outbox'}:
            await self.private_page('communication.outbox', {'limit': 20}, 'Outbox')
        elif command == 'following':
            await self.private_page('communication.following', {'limit': 20}, 'Following')
        elif command == 'notes':
            await self.private_page('identity.note_list', {}, 'Notes')
        elif command == 'todos':
            await self.private_page('identity.todo_list', {'limit': 20}, 'Todos')
        elif command == 'files':
            await self.files()
        elif command == 'retry':
            if self._last_read is None:
                self._write('没有可重新读取的请求。')
            else:
                function, arguments = self._last_read
                await function(*arguments)
        elif command in {'i', 'inbox'}:
            await self.inbox()
        elif command in {'n', 'next'}:
            await self.next_page()
        elif command.startswith('s '):
            parts = command.split(maxsplit=2)
            await self.search(parts[1] if len(parts) > 1 else '',
                              parts[2] if len(parts) > 2 else '')
        elif command.startswith('t '):
            await self.thread(command[2:].strip())
        elif command.startswith('r '):
            await self.read(command[2:].strip())
        elif command.isdecimal() and self.page:
            index = int(command) - 1 if len(command) <= 6 else -1
            if 0 <= index < len(self.page['items']):
                item = self.page['items'][index]
                if self.page['operation'] in {'identity.note_list', 'identity.todo_list'}:
                    name = item.get('name')
                    if isinstance(name, str) and name:
                        operation = self.page['operation'].replace('_list', '_get')
                        await self._document(operation, {'name': name}, name)
                    else:
                        self._write('该条目没有可读取的标识。')
                elif rid := self._item_id(item):
                    await self.read(rid)
                else:
                    self._write('该条目没有可读取的标识。')
            else:
                self._write('序号不在本页。')
        else:
            self._write(HELP)
        return True

    async def run(self):
        self._write(HELP)
        await self.home()
        while True:
            self.stdout.write('msg> ')
            self.stdout.flush()
            line = self.stdin.readline()
            if not line or not await self.command(line):
                break


async def run_tui(client, *, stdin=None, stdout=None, width=None):
    """Run the read-only UI; the owner of ``client`` closes its transport."""
    await TerminalUI(client, stdin=stdin, stdout=stdout, width=width).run()
