"""Exercise the real public terminal on a disposable loopback fixture.

Playwright and Chromium are required. Run serve_terminal_fixture.py first,
then pass its private --fixture-file and a task-owned --output directory.
No route, response, HTML or request is mocked; no production origin is accepted.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright


def check(options):
    fixture = json.loads(options.fixture_file.read_text())
    assert fixture['scope'] == 'disposable terminal loopback TestRoot only'
    origin = fixture['origin']
    parsed = urlsplit(origin)
    assert parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
    accounts_file = Path(fixture['accounts_file'])
    assert accounts_file.stat().st_mode & 0o077 == 0
    accounts = json.loads(accounts_file.read_text())
    assert accounts['scope'] == 'disposable loopback TestRoot only'
    public, private = fixture['public_post'], fixture['private_post']
    options.output.mkdir(parents=True, exist_ok=True)
    checks, requests, errors, dialogs = [], [], [], []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=os.environ.get('MSG_BROWSER_PATH', '/usr/bin/chromium')
        )
        try:
            for phone in (False, True):
                device = 'phone' if phone else 'desktop'
                context = browser.new_context(
                    viewport={'width': 390, 'height': 844}
                    if phone
                    else {'width': 1280, 'height': 900},
                    has_touch=phone,
                    is_mobile=phone,
                )
                # The owner can read the private post normally, but terminal requests omit this cookie.
                context.add_cookies(accounts['accounts'][1]['cookies'])
                page = context.new_page()
                page.set_default_timeout(15000)
                page.on('pageerror', lambda value: errors.append(str(value)))
                page.on('dialog', lambda value: (dialogs.append(value.type), value.dismiss()))
                terminal_requests = []
                page.on(
                    'request',
                    lambda request, collection=terminal_requests: (
                        collection.append(request)
                        if urlsplit(request.url).path == '/_terminal'
                        else None
                    ),
                )
                try:
                    response = page.goto(origin + '/terminal')
                    assert response.status == 200
                    assert "script-src 'sha256-" in response.headers['content-security-policy']
                    assert page.locator('#command').get_attribute('maxlength') == '512'

                    def run(command, expected_status=200, *, page=page, phone=phone, device=device):
                        page.locator('#command').fill(command)
                        with page.expect_response(
                            lambda item: urlsplit(item.url).path == '/_terminal'
                        ) as seen:
                            if phone:
                                page.locator('#run').tap()
                            else:
                                page.locator('#command').press('Enter')
                        result = seen.value
                        assert result.status == expected_status, (
                            command,
                            result.status,
                            result.text(),
                        )
                        page.wait_for_function('!document.querySelector("#command").readOnly')
                        value = result.json()
                        request = result.request
                        assert request.method == 'GET'
                        query = parse_qs(urlsplit(request.url).query)
                        assert list(query) == ['command']
                        assert not any(
                            name in request.all_headers()
                            for name in ('cookie', 'authorization', 'x-msg-request')
                        )
                        requests.append({
                            'device': device,
                            'command': query['command'][0],
                            'status': result.status,
                        })
                        return value

                    for command in (
                        'help',
                        'help read',
                        'status',
                        'time',
                        'server',
                        'stats',
                        'topics',
                        'ls /main',
                        'feed',
                        'users',
                        'user ' + fixture['user_handle'],
                        'rules',
                    ):
                        value = run(command)
                        assert isinstance(value['output'], str) and value['output']
                        assert private['marker'] not in value['output']
                    checks.append(
                        device + ': all read commands use one anonymous GET and real API output'
                    )

                    value = run('read ' + public['path'])
                    assert public['marker'] in value['output']
                    assert '<script>window.__TERMINAL_XSS=1</script>' in value['output']
                    assert page.evaluate('typeof window.__TERMINAL_XSS') == 'undefined'
                    assert page.locator('#transcript script, #transcript img').count() == 0
                    assert public['marker'] in run('read ' + public['id'])['output']
                    found = run('search ' + public['marker'])
                    assert found.get('links')
                    absent = run('search ' + private['marker'])
                    assert private['marker'] not in absent['output'] and not absent.get('links')
                    checks.append(
                        device
                        + ': public paths/IDs/search render literal text and exclude private content'
                    )

                    for target in (private['path'], private['id']):
                        page.locator('#command').fill('read ' + target)
                        with page.expect_response(
                            lambda item: urlsplit(item.url).path == '/_terminal'
                        ) as seen:
                            page.locator('#command').press('Enter')
                        assert seen.value.status in (403, 404)
                        page.wait_for_function('!document.querySelector("#command").readOnly')
                        assert private['marker'] not in seen.value.text()
                        assert page.locator('#command').input_value() == 'read ' + target
                        assert (
                            'not publicly readable' in page.locator('#transcript').inner_text()
                            or 'No public resource' in page.locator('#transcript').inner_text()
                        )
                    checks.append(
                        device + ': private path and ID stay denied even with owner browser cookies'
                    )

                    page.locator('#command').fill('st')
                    page.locator('#command').press('Tab')
                    assert page.locator('#command').input_value() == 'stat'
                    assert 'stats, status' in page.locator('#status').inner_text()
                    page.locator('#command').fill('help rea')
                    page.locator('#command').press('Tab')
                    assert page.locator('#command').input_value() == 'help read'
                    page.locator('#command').fill('read ' + public['path'][:-3])
                    page.locator('#command').press('Tab')
                    assert page.locator('#command').input_value() == 'read ' + public['path']
                    page.locator('#command').fill('user terminal-o')
                    page.locator('#command').press('Tab')
                    assert (
                        page.locator('#command').input_value() == 'user ' + fixture['user_handle']
                    )
                    page.locator('#command').fill('search draft')
                    page.locator('#command').press('ArrowUp')
                    assert page.locator('#command').input_value().startswith('read ')
                    page.locator('#command').press('ArrowDown')
                    assert page.locator('#command').input_value() == 'search draft'
                    before = len(terminal_requests)
                    page.locator('#command').dispatch_event('compositionstart')
                    page.locator('#command').press('Enter')
                    assert len(terminal_requests) == before
                    page.locator('#command').dispatch_event('compositionend')
                    checks.append(
                        device
                        + ': command/argument completion, multiple matches, history and IME remain usable'
                    )

                    for invalid in (
                        'read',
                        'help;status',
                        'read /main extra',
                        'search ' + 'x' * 81,
                    ):
                        before = len(terminal_requests)
                        page.locator('#command').fill(invalid)
                        page.locator('#command').press('Enter')
                        assert len(terminal_requests) == before
                    for unsafe in ('read /main;id', 'read $(id)', 'read javascript:alert(1)'):
                        value = run(unsafe, 400)
                        assert value['status'] == 'error'
                    checks.append(
                        device + ': usage and unsafe input never create writes or shell behavior'
                    )

                    run('feed')
                    hrefs = page.locator('#transcript .result-links a').evaluate_all(
                        '(nodes) => nodes.map(node => node.href)'
                    )
                    assert hrefs and all(urlsplit(href).netloc == parsed.netloc for href in hrefs)
                    assert all(not urlsplit(href).query for href in hrefs)
                    button = page.locator('#transcript .result-links button').last
                    with page.expect_response(
                        lambda item: urlsplit(item.url).path == '/_terminal'
                    ) as seen:
                        button.tap() if phone else button.click()
                    assert seen.value.status == 200
                    page.wait_for_function('!document.querySelector("#command").readOnly')
                    assert public['marker'] in seen.value.json()['output']
                    link = (
                        page
                        .locator('#transcript .result-links a')
                        .filter(has_text='terminal-browser-public')
                        .last
                    )
                    with page.expect_navigation() as navigation:
                        link.tap() if phone else link.click()
                    assert navigation.value.status == 200
                    assert public['marker'] in page.locator('body').inner_text()
                    assert page.evaluate('typeof window.__TERMINAL_XSS') == 'undefined'
                    checks.append(
                        device
                        + ': same-origin result links and Read here navigate real public content'
                    )

                    page.goto(origin + '/terminal')
                    run('help')
                    run('ls /main')
                    run('read ' + public['path'])
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    page.screenshot(path=str(options.output / (device + '.png')), full_page=True)
                    checks.append(
                        device
                        + ': CSP executes the real script and the transcript fits the viewport'
                    )
                finally:
                    context.close()
            assert not errors, errors
            assert not dialogs, dialogs
        finally:
            browser.close()
    result = {
        'scope': fixture['scope'],
        'source_sha256': fixture['source_sha256'],
        'checks': checks,
        'requests': requests,
        'page_errors': errors,
        'dialogs': dialogs,
    }
    (options.output / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {'checks': checks, 'page_errors': errors, 'dialogs': dialogs},
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('output/playwright/terminal'))
    check(parser.parse_args())


if __name__ == '__main__':
    main()
