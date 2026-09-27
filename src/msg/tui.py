"""Small, line-oriented terminal navigator over the public read contracts.

The UI deliberately has no mutation command.  It never interprets links or
resource text as terminal control sequences or as operations to execute.
"""
from __future__ import annotations

import re
import shutil
import sys
import textwrap


READ_OPERATIONS = frozenset({
    'discovery.get', 'discovery.read_query', 'discovery.lexical_search',
    'communication.inbox', 'discussion.thread',
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


class TerminalUI:
    def __init__(self, client, *, stdin=None, stdout=None, width=None):
        self.client = client
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.width = width
        self.page = None

    def _write(self, value=''):
        width = max(24, min(self.width or shutil.get_terminal_size((80, 24)).columns, 160))
        for line in safe_text(value).split('\n'):
            for wrapped in textwrap.wrap(line, width=width, break_long_words=True,
                                         break_on_hyphens=False, replace_whitespace=False,
                                         drop_whitespace=False) or ['']:
                self.stdout.write(wrapped.rstrip() + '\n')
        self.stdout.flush()

    async def _read(self, operation, arguments):
        if operation not in READ_OPERATIONS:
            raise ValueError('TUI only supports read operations')
        result = await self.client.call(operation, arguments)
        if result.status != 'ok':
            error = getattr(result, 'error', None)
            code = getattr(error, 'code', None) or 'read_failed'
            self._write('读取失败：' + code)
            return None
        return result.data

    def _show_items(self, items):
        for index, item in enumerate(items, 1):
            ref = item.get('ref') or item.get('resource') or {}
            rid = item.get('id') or ref.get('id') or item.get('conversation_id') or ''
            name = item.get('name') or item.get('path') or item.get('source') or rid
            self._write(f'{index}. {name}  {rid}')
            snippet = item.get('snippet')
            if isinstance(snippet, dict) and snippet.get('text'):
                self._write('   ' + snippet['text'])

    async def _show_page(self, operation, arguments, title):
        # A cursor may become invalid after a grant changes. Never keep the
        # previous page selectable after a failed permission recheck.
        self.page = None
        data = await self._read(operation, arguments)
        if data is None:
            return
        items = data.get('items', [])
        self.page = {'operation': operation, 'arguments': arguments,
                     'cursor': data.get('cursor'), 'title': title, 'items': items}
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
        self._write('服务：' + getattr(state, 'server', ''))
        if subject:
            self._write('身份管理请使用 msg identity；TUI 不接收密钥或令牌。')

    async def inbox(self):
        if not getattr(self.client.state, 'subject', None):
            self._write('Inbox 需要已登录身份。')
            return
        await self._show_page('communication.inbox', {'limit': 20}, 'Inbox')

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
        data = await self._read('discovery.get', {'id': rid})
        if data is None:
            self.page = None
            return
        self._write(data.get('path') or data.get('name') or rid)
        content = data.get('content')
        if isinstance(content, str):
            self._write(content)
        else:
            self._write('（无文本正文）')
        self._write('读取不会发送 ACK。')

    async def next_page(self):
        if not self.page or not self.page['cursor']:
            self._write('没有下一页。')
            return
        current = self.page
        arguments = ({'cursor': current['cursor']} if current['operation'] in
                     {'discovery.read_query', 'discovery.lexical_search'} else
                     {**current['arguments'], 'cursor': current['cursor']})
        await self._show_page(current['operation'], arguments, current['title'])

    async def command(self, raw):
        command = raw.strip()
        if command in {'q', 'quit', 'exit'}:
            return False
        if command in {'h', 'home', ''}:
            await self.home()
        elif command in {'id', 'identity'}:
            self.identity()
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
            index = int(command) - 1
            if 0 <= index < len(self.page['items']):
                item = self.page['items'][index]
                ref = item.get('ref') or item.get('resource') or {}
                rid = item.get('id') or ref.get('id') or item.get('conversation_id')
                await self.read(rid)
            else:
                self._write('序号不在本页。')
        else:
            self._write('命令：h Home，id 身份，i Inbox，s <scope> <terms> 搜索，t <id> 线程，r <id> 读取，n 下一页，q 退出。')
        return True

    async def run(self):
        self._write('命令：h Home，id 身份，i Inbox，s <scope> <terms> 搜索，t <id> 线程，r <id> 读取，n 下一页，q 退出。')
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
