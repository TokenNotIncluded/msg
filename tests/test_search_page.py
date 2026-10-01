"""Browser search compiles common operators and preserves authorized reads."""

from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import httpx
import pytest
from test_service import call, register

from msg.transports.http import create_app
from msg.transports.search_page import SearchSyntaxError, compile_branch, parse_query


@pytest.mark.parametrize(
    'query', ['alpha AND OR beta', 'alpha OR', '(alpha', '"alpha', 'alpha AND )']
)
def test_invalid_search_syntax(query):
    with pytest.raises(SearchSyntaxError):
        parse_query(query)


def test_search_operators_compile_without_changing_literal_terms():
    branches = parse_query(
        '"exact phrase" -skip (python OR rust) site:example.test after:2026-01-01'
    )
    assert len(branches) == 2
    args, phrases, negatives, sites, types, empty = compile_branch(
        branches[0], 'https://example.test'
    )
    assert args['terms'] == 'python' and args['not_terms'] == 'skip'
    assert phrases == ['exact phrase'] and not negatives and not sites and not types and not empty
    assert args['updated_after'] == '2026-01-01T00:00:00Z'


@pytest.mark.asyncio
async def test_search_page_results_syntax_raw_opensearch_and_readonly(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'search-page-user')
    for body in (
        '# Uniquequartz\n\npython async collaboration',
        '# Uniqueruby\n\nrust async collaboration',
        '# Uniquequiet\n\npython slow collaboration',
    ):
        result = await call(
            app, 'content.post_create', {'parent': '/main', 'body': body}, key=key, subject=uid
        )
        assert result.status == 'ok', result.error
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get('/search', headers={'Accept': 'text/html'})
        assert response.status_code == 200 and 'search-wordmark' in response.text
        assert "script-src 'sha256-" in response.headers['content-security-policy']
        assert 'sandbox' not in response.headers['content-security-policy']
        assert "form-action 'self'" in response.headers['content-security-policy']
        assert (
            'rel="search"' in response.text
            and 'data-search-template="' + app.settings.service_url + '/search?q=%s"'
            in response.text
        )
        for query, expected in [
            ('collaboration (python OR rust) -slow', ['Uniquequartz', 'Uniqueruby']),
            ('"async collaboration" -rust', ['Uniquequartz']),
            ('collaboration site:external.invalid', []),
        ]:
            response = await http.get(
                '/search?' + urlencode({'q': query}), headers={'Accept': 'text/html'}
            )
            assert response.status_code == 200, response.text
            for title in ('Uniquequartz', 'Uniqueruby', 'Uniquequiet'):
                assert (f'>{title}</a>' in response.text) == (title in expected), response.text[
                    response.text.index('<section class="search-surface') : response.text.index(
                        '</section>', response.text.index('<section class="search-surface')
                    )
                ]
        raw = await http.get('/search?q=Uniquequartz&format=raw', headers={'Accept': 'text/html'})
        assert raw.headers['content-type'].startswith('text/plain') and '<!doctype' not in raw.text
        assert 'Uniquequartz' in raw.text
        malformed = await http.get('/search?q=%22incomplete', headers={'Accept': 'text/html'})
        assert 'Close the quotation marks' in malformed.text
        assert (await http.post('/search', json={})).status_code == 405
        assert (await http.get('/search?q=a&q=b')).status_code == 400
        xml = await http.get('/opensearch.xml')
        document = ET.fromstring(xml.content)
        assert (
            document.find('{http://a9.com/-/spec/opensearch/1.1/}Url').attrib['template']
            == app.settings.service_url + '/search?q={searchTerms}'
        )
        assert (await http.head('/opensearch.xml')).content == b''
        feed = await http.get('/feed', headers={'Accept': 'text/html'})
        assert feed.status_code == 200 and '<form class="feed-filter"' not in feed.text
