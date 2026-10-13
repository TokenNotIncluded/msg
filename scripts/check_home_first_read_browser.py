"""Verify first-view readability and navigation with actual HTML/CSP fixtures.

The HTTP integration tests own cold-cache and authorization behavior. This
isolated browser server uses sample data, not live identities or counters.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from msg.plugins.public_board import default
from msg.transports.home_page import HOME_BROWSER_HEADERS, document_html, home_html

OUTPUT = Path('artifacts/home-first-read')
POSTS = [
    {
        'name': 'first-read',
        'path': '/main/first-read',
        'title': '让第一次打开，就能读到内容',
        'excerpt': '页面不必等待全站统计。先读帖子，再继续讨论；每次读取仍检查当前权限。',
        'created_at': '2026-10-09T14:00:00Z',
        'view_count': 12,
    },
    {
        'name': 'small-changes',
        'path': '/main/small-changes',
        'title': '留白，让讨论更清楚',
        'excerpt': '减少边框，保留内容。把标题、摘要和回复信息放到各自清楚的位置。',
        'created_at': '2026-10-09T13:00:00Z',
        'view_count': 7,
    },
    {
        'name': 'long-title',
        'path': '/main/long-title',
        'title': '一条比较长的中英文标题：Reliable first reads, without refreshing the whole page',
        'excerpt': '这是一组独立的浏览器测试数据，不是线上用户、帖子或统计。',
        'created_at': '2026-10-09T12:00:00Z',
        'view_count': 3,
    },
]
DATA = {
    'posts': 22,
    'posts_today': 3,
    'users': 8,
    'latest': [],
    'date': '2026-10-09',
    'timezone': 'Asia/Taipei',
    'channels': [
        {
            'name': 'main',
            'path': '/main',
            'about': '公开讨论 / Public discussions',
            'posts': 22,
            'mode': '0755',
            'posting': 'identity',
        },
    ],
}
POST_BODY = (
    '---\nid: browser-fixture\n---\n\n# 让第一次打开，就能读到内容\n\n'
    '正文在第一次访问时可见。没有刷新，没有隐藏的加载占位。\n\n'
    '## 阅读优先\n\n帖子列表与全站统计独立读取。统计暂时不可用时，帖子仍可阅读。\n\n'
    '## 小范围的修改\n\n保留现有登录、主题和权限。\n\n'
    '```python\nprint("read on the first visit")\n```\n'
)
BOARD = default()


def html(path):
    if path in {'/', '/cold'}:
        return home_html(
            DATA if path == '/' else None,
            post_index={'items': POSTS},
            login_enabled=True,
            public_board=BOARD,
        )
    if path == '/topics':
        return document_html(
            '# Topics\n\n## [让第一次打开，就能读到内容](/main/first-read)\n\n'
            '公开讨论。\n\n## [留白，让讨论更清楚](/main/small-changes)\n\n'
            '减少边框，保留内容。',
            resource={'type': 'topic', '_post_index_next': None},
            raw_path=path,
        )
    return document_html(
        POST_BODY,
        raw_path=path,
        resource={
            'type': 'post',
            'id': 'r_browser_fixture',
            'revision': 'rev_fixture',
            'created_at': '2026-10-09T14:00:00Z',
            'view_count': 12,
            'links': {'a': {'path': '/@fixture'}, 't': {'path': '/main'}},
        },
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/favicon.png':
            self.send_response(204)
            self.end_headers()
            return
        image = path == '/_public-board/art.svg'
        payload = BOARD['svg'].encode() if image else html(path)
        self.send_response(200)
        self.send_header('Content-Type', 'image/svg+xml' if image else 'text/html; charset=utf-8')
        for key, value in HOME_BROWSER_HEADERS.items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def verify(page):
    assert not page.evaluate('window.cspErrors'), page.evaluate('window.cspErrors')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('#content').is_visible()


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    results = []
    try:
        with sync_playwright() as playwright:
            options = (
                {'executable_path': os.environ['MSG_BROWSER_EXECUTABLE']}
                if os.environ.get('MSG_BROWSER_EXECUTABLE')
                else {}
            )
            browser = playwright.chromium.launch(**options)
            for width in (360, 768, 1440):
                for theme in ('light', 'dark'):
                    context = browser.new_context(
                        viewport={'width': width, 'height': 960},
                        color_scheme=theme,
                        locale='zh-CN',
                        reduced_motion='reduce',
                    )
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda error, errors=errors: errors.append(str(error)))
                    page.add_init_script(
                        "window.cspErrors=[];document.addEventListener('securitypolicyviolation',"
                        'e=>cspErrors.push(e.violatedDirective));'
                    )
                    page.goto(origin + '/cold', wait_until='networkidle')
                    first = page.locator('.post-title a').first
                    expect(first).to_have_text(POSTS[0]['title'])
                    verify(page)
                    assert page.locator('.posts .post-excerpt').count() == len(POSTS)
                    # Direct navigation, browser back and forward must not lose the body.
                    first.click()
                    expect(page.locator('#content')).to_contain_text('正文在第一次访问时可见。')
                    verify(page)
                    page.go_back(wait_until='networkidle')
                    expect(page.locator('.post-title a').first).to_be_visible()
                    verify(page)
                    page.go_forward(wait_until='networkidle')
                    expect(page.locator('#content')).to_contain_text('正文在第一次访问时可见。')
                    verify(page)
                    page.screenshot(path=str(OUTPUT / f'post-{width}-{theme}.png'), full_page=True)
                    page.goto(origin + '/', wait_until='networkidle')
                    verify(page)
                    expect(page.locator('.latest')).to_be_visible()
                    assert (
                        page.locator('.latest').bounding_box()['y']
                        < page.locator('.activity').bounding_box()['y']
                    )
                    for target in page.locator('.post-title a, .section-link').all():
                        assert target.bounding_box()['height'] >= 44
                    page.screenshot(path=str(OUTPUT / f'home-{width}-{theme}.png'), full_page=True)
                    page.locator('.section-link').click()
                    expect(page.locator('.page-topic-index')).to_be_visible()
                    verify(page)
                    page.screenshot(
                        path=str(OUTPUT / f'topics-{width}-{theme}.png'), full_page=True
                    )
                    assert not errors, errors
                    assert not page.evaluate('window.cspErrors'), page.evaluate('window.cspErrors')
                    results.append({'width': width, 'theme': theme, 'passed': True})
                    context.close()
            # Also exercise normal-motion navigation; reduced motion must not hide a failure.
            context = browser.new_context(reduced_motion='no-preference')
            page = context.new_page()
            page.goto(origin + '/cold', wait_until='networkidle')
            page.locator('.post-title a').first.click()
            expect(page.locator('#content')).to_contain_text('正文在第一次访问时可见。')
            page.go_back(wait_until='networkidle')
            expect(page.locator('.post-title a').first).to_be_visible()
            page.go_forward(wait_until='networkidle')
            expect(page.locator('#content')).to_contain_text('正文在第一次访问时可见。')
            context.close()
            # The repair is server-rendered, not dependent on browser scripting.
            context = browser.new_context(java_script_enabled=False)
            page = context.new_page()
            page.goto(origin + '/cold')
            expect(page.locator('.post-title a').first).to_have_text(POSTS[0]['title'])
            verify(page)
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (OUTPUT / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    print(
        f'{len(results)} viewport/theme cases plus normal-motion navigation and JavaScript-disabled first read passed.'
    )


if __name__ == '__main__':
    main()
