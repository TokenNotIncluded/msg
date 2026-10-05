"""无数据库的共享页面审计；生产页面缺陷只报告，不自动修复。"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import urlsplit

from msg.bootstrap import ROOT_WEB_SAMPLE
from msg.core.codec import digest

SCRIPTS = Path(__file__).resolve().parent
DATA = SCRIPTS.parent / 'src/msg/data'
DEFAULT_OUTPUT = Path.home() / '.cache/msg-ui/ui-audit'
VIEWPORTS = {1280: 800, 768: 1024, 390: 844, 320: 640}
THRESHOLDS = {'textMin': 11, 'hitMin': 44}
CHECKS = ('font', 'contrast', 'target', 'overflow', 'page_error', 'console_error')
STAMP = '2026-10-03T20:00:00Z'
PAGE_NAMES = (
    'home',
    'home-acc',
    'wiki',
    'post',
    'search',
    'search-empty',
    'login',
    'now',
    'terminal',
    'root',
)


def pages(service):
    """注册表值只渲染内存数据；不实例化应用或存储连接。"""
    from msg.transports.home_page import document_html, home_html
    from msg.transports.oauth_http import page as form_page
    from msg.transports.search_page import search_document

    data = {
        'posts': 24,
        'posts_today': 3,
        'users': 36,
        'date': '2026-10-04',
        'timezone': 'Asia/Taipei',
        'latest': [
            {
                'name': 'audit.md',
                'title': 'Readable messages / 可读的消息',
                'path': '/main/audit.md',
                'created_at': STAMP,
                'view_count': 12,
                'excerpt': 'A fixed public reading fixture. 固定的公开阅读数据。',
            }
        ],
    }
    wiki = (
        '# Audit wiki / 审计百科\n\n'
        '正文中的 [行内链接](/wiki/reference.md) 用于解释内容。\n\n'
        '## List / 列表\n\n- First item\n- 第二条\n\n'
        '> A quoted paragraph.\n\n'
        '| Name | Value |\n| --- | --- |\n| Fixture | 42 |\n\n'
        '## Code / 代码\n\n```python\ndef greeting(name):\n    return f"Hello, {name}"\n```\n'
    )
    resource = {
        'id': 'p_audit_fixture',
        'revision': 'v_audit_fixture',
        'type': 'post',
        'created_at': STAMP,
        'modified_at': STAMP,
        'view_count': 42,
        'links': {'a': {'path': '/@kei'}, 't': {'path': '/main'}},
    }
    result = {
        'path': '/main/audit.md',
        'title': 'Readable messages / 可读的消息',
        'excerpt': 'Public content from this isolated fixture; no live account is used.',
    }
    login = (
        '<h1>Sign in / 登录</h1><p>Use your existing MSG identity.</p>'
        '<form method="get" action="/login"><label for="account">Account / 账号</label>'
        '<input id="account" name="account" autocomplete="username" required>'
        '<button type="submit">Continue / 继续</button></form>'
    )
    return {
        'home': lambda: home_html(data, service_url=service, login_enabled=True),
        'home-acc': lambda: home_html(
            data, service_url=service, account={'name': '@kei', 'groups': []}, login_enabled=True
        ),
        'wiki': lambda: document_html(
            wiki, title='Audit wiki', raw_path='/wiki', service_url=service
        ),
        'post': lambda: document_html(
            '---\nfixture: true\n---\n\n# A public post / 公开帖子\n\n'
            'This paragraph links to [the wiki](/wiki) for context.\n',
            title='A public post',
            resource=resource,
            raw_path='/main/audit.md',
            service_url=service,
        ),
        'search': lambda: search_document('messages', [result], service_url=service),
        'search-empty': lambda: search_document('unmatched', [], service_url=service),
        'login': lambda: form_page('Sign in', login).body,
        'now': lambda: (DATA / 'live-space.html').read_bytes(),
        'terminal': lambda: (DATA / 'public-terminal.html').read_bytes(),
        'root': lambda: ROOT_WEB_SAMPLE,
    }


def root_fixture():
    """复用旧脚本的真实 Handler，避开其导入即执行的浏览器验收。"""
    source = SCRIPTS / 'check_universe_browser.py'
    tree = ast.parse(source.read_text(), filename=str(source))
    declarations = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == 'SERVER' for target in node.targets
        ):
            break
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == 'OUTPUT' for target in node.targets
        ):
            continue
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == 'OUTPUT'
        ):
            continue
        declarations.append(node)
    else:
        raise RuntimeError('Root fixture 的 SERVER 边界已改变，请先复核复用方式。')
    module = ModuleType('msg_ui_audit_root_fixture')
    module.__file__ = str(source)
    exec(
        compile(ast.Module(body=declarations, type_ignores=[]), str(source), 'exec'),
        module.__dict__,
    )
    if not hasattr(module, 'Handler') or not hasattr(module, 'USERS'):
        raise RuntimeError('Root fixture 缺少 Handler 或 USERS。')
    return module


def static_headers(name):
    from starlette.requests import Request

    from msg.transports.live_space import now_response
    from msg.transports.public_terminal import terminal_response

    # HTML 分支只读取发行文件和响应上限，不需要 executor 或数据库。
    service = SimpleNamespace(
        settings=SimpleNamespace(
            server=SimpleNamespace(limits=SimpleNamespace(max_response_bytes=4 * 1024 * 1024))
        )
    )
    request = Request({
        'type': 'http',
        'method': 'GET',
        'path': '/' + name,
        'query_string': b'',
        'headers': [(b'accept', b'text/html')],
        'server': ('127.0.0.1', 80),
        'scheme': 'http',
    })
    response = asyncio.run((now_response if name == 'now' else terminal_response)(service, request))
    return response.headers


@contextmanager
def fixture_server():
    from msg.extensions.hosting import hosted_headers
    from msg.transports.home_page import HOME_BROWSER_HEADERS
    from msg.transports.oauth_http import page as form_page

    fixture = root_fixture()
    rendered = {}
    headers_by_name = {
        'root': hosted_headers('w_root_web', 'index.html', digest(ROOT_WEB_SAMPLE)),
        'login': form_page('', '').headers,
        'now': static_headers('now'),
        'terminal': static_headers('terminal'),
    }

    class Handler(fixture.Handler):
        def do_GET(self):
            path = urlsplit(self.path).path
            name = 'root' if path == '/@root/web/' else path.strip('/')
            if name in rendered:
                body = rendered[name]
                headers = headers_by_name.get(name, HOME_BROWSER_HEADERS)
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                for key, value in headers.items():
                    if key.lower() not in {'content-length', 'content-type'}:
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
            elif path == '/_now':
                self.respond({
                    'generated_at': STAMP,
                    'bounded': True,
                    'nodes': [
                        {
                            'id': 'u_fixture_kei',
                            'name': '@kei',
                            'path': '/@kei',
                            'presence': {'state': 'available'},
                            'last_public_activity_at': STAMP,
                        }
                    ],
                    'edges': [],
                    'events': [
                        {'author': 'u_fixture_kei', 'path': '/main/audit.md', 'time': STAMP}
                    ],
                })
            elif path == '/_terminal':
                self.respond({
                    'output': 'Fixed public fixture / 固定公开数据\n1 post · 1 user',
                    'links': [{'href': '/main/audit.md', 'label': 'Readable messages'}],
                })
            elif path == '/favicon.png':
                self.send_response(204)
                self.end_headers()
            else:
                super().do_GET()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    fixture.SERVICE = f'http://127.0.0.1:{server.server_port}'
    rendered.update((name, render()) for name, render in pages(fixture.SERVICE).items())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fixture.SERVICE, rendered
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def load_allowlist(path):
    entries = json.loads(path.read_text())
    if not isinstance(entries, list):
        raise ValueError('白名单必须是列表。')
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not entry.get('reason', '').strip()
            or entry.get('page') not in PAGE_NAMES
            or entry.get('check') not in CHECKS
        ):
            raise ValueError('每条白名单需要明确的 page、check 和非空 reason。')
        if entry['check'] in {'page_error', 'console_error'}:
            if not entry.get('message_pattern', '').startswith('^'):
                raise ValueError('异常白名单需要以 ^ 开头的具体 message_pattern。')
            re.compile(entry['message_pattern'])
        elif not entry.get('selector') or entry['selector'] == '*':
            raise ValueError('元素白名单需要具体 selector。')
    return entries


def allowed(finding, case, check, entries):
    for entry in entries:
        if entry['page'] != case['page'] or entry['check'] != check:
            continue
        if any(key in entry and entry[key] != case[key] for key in ('width', 'theme')):
            continue
        if entry.get('selector') == finding.get('selector') or (
            entry.get('message_pattern') and re.search(entry['message_pattern'], finding['text'])
        ):
            return entry['reason']
    return None


def parse_selection(values, choices):
    selected = [part for value in values for part in value.split(',')]
    unknown = set(selected) - set(map(str, choices))
    if unknown:
        raise ValueError('未知选项：' + ', '.join(sorted(unknown)))
    return selected


def audit(args, sync_playwright):
    started = time.perf_counter()
    selected_pages = parse_selection(args.pages, PAGE_NAMES)
    selected_widths = [int(width) for width in parse_selection(args.widths, VIEWPORTS)]
    script = (SCRIPTS / 'ui_audit.js').read_text()
    allowlist = load_allowlist(args.allow)
    report = {'thresholds': THRESHOLDS, 'results': [], 'inputs': {}}
    with fixture_server() as (service, rendered), sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=args.browser,
            args=['--no-sandbox', '--enable-unsafe-swiftshader', '--use-angle=swiftshader'],
        )
        report['browser'] = browser.version
        report['inputs'] = {
            'html_sha256': {
                name: hashlib.sha256(body).hexdigest() for name, body in rendered.items()
            },
            'audit_js_sha256': hashlib.sha256(script.encode()).hexdigest(),
            'audit_python_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'allowlist_sha256': hashlib.sha256(args.allow.read_bytes()).hexdigest(),
            'root_fixture_sha256': hashlib.sha256(
                (SCRIPTS / 'check_universe_browser.py').read_bytes()
            ).hexdigest(),
        }
        try:
            for name in selected_pages:
                for width in selected_widths:
                    for theme in ('dark',) if name == 'root' else ('dark', 'light'):
                        case = {
                            'page': name,
                            'width': width,
                            'height': VIEWPORTS[width],
                            'theme': theme,
                        }
                        context = browser.new_context(
                            viewport={'width': width, 'height': VIEWPORTS[width]},
                            color_scheme=theme,
                            reduced_motion='reduce',
                            is_mobile=width <= 390,
                            has_touch=width <= 390,
                            locale='en-US',
                            timezone_id='UTC',
                        )
                        try:
                            page = context.new_page()
                            errors, console = [], []
                            page.on(
                                'pageerror', lambda error, errors=errors: errors.append(str(error))
                            )
                            page.on(
                                'console',
                                lambda message, console=console: (
                                    console.append(message.text)
                                    if message.type == 'error'
                                    else None
                                ),
                            )
                            path = '/@root/web/' if name == 'root' else '/' + name
                            page.goto(service + path, wait_until='load')
                            page.evaluate('document.fonts.ready')
                            if name == 'root':
                                page.wait_for_function(
                                    "document.querySelector('#counts').textContent.includes('36 STARS')"
                                )
                            elif name == 'now':
                                page.wait_for_function(
                                    "document.querySelector('#live').textContent==='LIVE'"
                                )
                            elif name == 'terminal':
                                page.locator('#command').fill('feed')
                                page.locator('#run').click()
                                page.locator('.result-links a').wait_for()
                            findings = page.evaluate(script, THRESHOLDS)
                            findings['page_error'] = [{'text': message} for message in errors]
                            findings['console_error'] = [{'text': message} for message in console]
                            result = {**case, 'findings': {}, 'ignored': []}
                            for check in CHECKS:
                                result['findings'][check] = []
                                for finding in findings[check]:
                                    reason = allowed(finding, case, check, allowlist)
                                    if reason:
                                        result['ignored'].append({
                                            'check': check,
                                            **finding,
                                            'reason': reason,
                                        })
                                    else:
                                        result['findings'][check].append(finding)
                            report['results'].append(result)
                            if args.shots:
                                args.shots.mkdir(parents=True, exist_ok=True)
                                page.screenshot(
                                    path=str(args.shots / f'{name}-{width}-{theme}.png'),
                                    full_page=True,
                                )
                        finally:
                            context.close()
        finally:
            browser.close()
    report['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    return report


def print_report(report, *, verbose=False, limit=5):
    print('page          width theme font contrast target overflow pageerror consoleerror ignored')
    for result in report['results']:
        counts = [len(result['findings'][check]) for check in CHECKS]
        print(
            f'{result["page"]:13} {result["width"]:5} {result["theme"]:5} '
            + ' '.join(f'{count:8}' for count in counts)
            + f' {len(result["ignored"]):7}'
        )
        if verbose:
            for check in CHECKS:
                for finding in result['findings'][check][:limit]:
                    print(f'  {check}: {json.dumps(finding, ensure_ascii=False)}')
    total = sum(
        len(findings) for result in report['results'] for findings in result['findings'].values()
    )
    print(
        f'{len(report["results"])} 个组合；未豁免问题 {total}；耗时 {report["elapsed_seconds"]} 秒。'
    )
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pages', nargs='+', default=list(PAGE_NAMES))
    parser.add_argument('--widths', nargs='+', default=list(map(str, VIEWPORTS)))
    parser.add_argument('--shots', type=Path)
    parser.add_argument('--report', type=Path, default=DEFAULT_OUTPUT / 'report.json')
    parser.add_argument('--allow', type=Path, default=SCRIPTS / 'ui_audit_allow.json')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--require-browser', action='store_true')
    parser.add_argument(
        '--browser', default=os.environ.get('MSG_BROWSER_PATH', '/usr/bin/chromium')
    )
    args = parser.parse_args()
    if not Path(args.browser).is_file() or not os.access(args.browser, os.X_OK):
        print(f'SKIP：Chromium 不存在或不可执行：{args.browser}')
        return int(args.require_browser)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            'SKIP：缺少 Playwright；使用 uv run --with playwright python scripts/check_ui_audit.py。'
        )
        return int(args.require_browser)
    try:
        report = audit(args, sync_playwright)
    except ValueError as error:
        parser.error(str(error))
    except ModuleNotFoundError as error:
        print(
            f'缺少渲染依赖 {error.name}：请使用 uv run --extra server --with playwright python scripts/check_ui_audit.py。'
        )
        return 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return int(bool(print_report(report, verbose=args.verbose, limit=args.limit)))


if __name__ == '__main__':
    raise SystemExit(main())
