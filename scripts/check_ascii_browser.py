"""Check character art against a disposable MSG HTTP fixture and its real CSP.

Start scripts/serve_flight_fixture.py, then pass its origin with --origin.
Only reads and local display controls are exercised; no messages or writes.
"""

import argparse
import json
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright

from msg.bootstrap import ROOT_WEB_SAMPLE
from msg.core.codec import digest
from msg.transports.ascii_art import SCRIPT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--output', type=Path, default=Path('artifacts/ascii'))
    args = parser.parse_args()
    origin = args.origin.rstrip('/')
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shutil.which('chromium'), headless=True)
        for label, width, height in [('desktop', 1440, 1000), ('mobile', 390, 844)]:
            context = browser.new_context(viewport={'width': width, 'height': height})
            page = context.new_page()
            errors, external = [], []
            page.on('pageerror', lambda e, errors=errors: errors.append(str(e)))
            page.on(
                'request',
                lambda r, external=external: (
                    external.append(r.url)
                    if not r.url.startswith(origin + '/') and not r.url.startswith('data:')
                    else None
                ),
            )
            page.add_init_script(
                "window.cspViolations=[]; addEventListener('securitypolicyviolation', "
                'e=>cspViolations.push(e.violatedDirective));'
            )
            page.goto(origin + '/', wait_until='networkidle')
            page.wait_for_selector('[data-ascii-ready=true]')
            page.screenshot(path=str(args.output / f'{label}-home.png'), full_page=True)
            scenes = []
            for name in ('galaxy', 'flow', 'aurora'):
                page.locator(f'[data-ascii-piece={name}]').click()
                scenes.append({'name': name, 'animated': not still(page)})
            page.locator('[data-menu] > summary').click()
            page.locator('[data-pause]').click()
            paused = still(page)
            page.locator('[data-ascii-piece=community]').click()
            svg = page.locator('.signal-stage img').is_visible()
            page.locator('[data-ascii-piece=flow]').click()
            page.evaluate(
                "document.querySelector('#public-board').dispatchEvent("
                "new CustomEvent('msg:board-updated',{detail:{svgChanged:true}}))"
            )
            updated = page.locator('.signal-stage img').is_visible()
            frame_text = page.locator('[data-ascii-canvas]').text_content()
            home_overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
            home_csp = page.evaluate('cspViolations')
            page.goto(origin + '/@root/web/', wait_until='networkidle')
            page.locator('[data-ascii-toggle]').click()
            page.wait_for_selector('#ascii-studies:not([hidden])')
            page.screenshot(path=str(args.output / f'{label}-root.png'))
            page.locator('#pause').click()
            root_paused = still(page)
            page.locator('[data-ascii-close]').click()
            closed = page.locator('#ascii-studies').is_hidden()
            overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
            csp = home_csp + page.evaluate('cspViolations')
            result = {
                'device': label,
                'scenes': scenes,
                'paused': paused,
                'community_svg': svg,
                'updated_svg_visible': updated,
                'frame_text': frame_text,
                'root_paused': root_paused,
                'root_closed': closed,
                'overflow': home_overflow or overflow,
                'errors': errors,
                'external': external,
                'csp_violations': csp,
            }
            results.append(result)
            context.close()
        context = browser.new_context(reduced_motion='reduce')
        page = context.new_page()
        page.goto(origin + '/', wait_until='networkidle')
        results.append({'reduced_motion_still': still(page)})
        context.close()
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page()
        page.goto(origin + '/')
        results.append({'no_js_svg_visible': page.locator('.signal-stage img').is_visible()})
        context.close()
        browser.close()
    report = {
        'scope': 'disposable HTTP fixture; synthetic identities; no production writes',
        'root_artifact_digest': digest(ROOT_WEB_SAMPLE),
        'ascii_bundle_digest': digest(SCRIPT.encode()),
        'results': results,
    }
    (args.output / 'browser-results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    for row in results[:2]:
        assert all(scene['animated'] for scene in row['scenes']), row
        assert row['paused'] and row['community_svg'] and row['updated_svg_visible'], row
        assert row['root_paused'] and row['root_closed'] and row['frame_text'] == '', row
        assert not any(row[key] for key in ('overflow', 'errors', 'external', 'csp_violations')), (
            row
        )
    assert results[2]['reduced_motion_still'] and results[3]['no_js_svg_visible']


def still(page):
    page.wait_for_timeout(150)
    return page.evaluate(
        """async()=>{const c=document.querySelector('[data-ascii-canvas]');
        const a=c.toDataURL();await new Promise(r=>setTimeout(r,350));return a===c.toDataURL()}"""
    )


if __name__ == '__main__':
    main()
