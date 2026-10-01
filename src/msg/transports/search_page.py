"""A quiet search surface using Google-style operators over authorized MSG reads."""

import re
import time
from datetime import UTC, datetime
from html import escape
from urllib.parse import quote, unquote, urlencode, urlsplit

from msg.core.codec import wire
from msg.core.post_preview import post_preview
from msg.core.requests import request_for
from msg.transports.home_page import document_html, markdown_text


class SearchSyntaxError(ValueError):
    pass


def parse_query(query):
    if len(query) > 512:
        raise SearchSyntaxError('查询最多 512 个字符。 / Query is limited to 512 characters.')
    query = query.replace('“', '"').replace('”', '"')
    if query.count('"') % 2:
        raise SearchSyntaxError('请补全引号。 / Close the quotation marks.')
    tokens = re.findall(r'-?(?:[A-Za-z]+:)?"[^"]*"|\(|\)|[^\s()]+', query)
    position = 0

    def merge(left, right):
        branches = [a + b for a in left for b in right]
        if len(branches) > 4 or any(len(branch) > 16 for branch in branches):
            raise SearchSyntaxError('查询过于复杂，请减少 OR 或关键词。 / Simplify the query.')
        return branches

    def conjunction():
        nonlocal position
        result = [[]]
        found = False
        while position < len(tokens) and tokens[position] not in {'OR', ')'}:
            token = tokens[position]
            position += 1
            if token == 'AND':
                if not found or position == len(tokens) or tokens[position] in {'AND', 'OR', ')'}:
                    raise SearchSyntaxError('AND 两侧需要关键词。 / AND needs terms on both sides.')
                continue
            if token == '(':
                factor = disjunction()
                if position >= len(tokens) or tokens[position] != ')':
                    raise SearchSyntaxError('请补全括号。 / Close the parentheses.')
                position += 1
            else:
                factor = [[token]]
            result = merge(result, factor)
            found = True
        if not found:
            raise SearchSyntaxError('请输入关键词。 / Enter a search term.')
        return result

    def disjunction():
        nonlocal position
        result = conjunction()
        while position < len(tokens) and tokens[position] == 'OR':
            position += 1
            result += conjunction()
            if len(result) > 4:
                raise SearchSyntaxError('最多支持 4 个 OR 分支。 / Up to four OR branches.')
        return result

    branches = disjunction()
    if position != len(tokens):
        raise SearchSyntaxError('括号不匹配。 / Unmatched parentheses.')
    return branches


def date_operator(value):
    parts = value.replace('/', '-').split('-')
    try:
        return (
            datetime(*([int(part) for part in parts] + [1] * (3 - len(parts))), tzinfo=UTC)
            .isoformat()
            .replace('+00:00', 'Z')
        )
    except ValueError, TypeError:
        raise SearchSyntaxError(
            '日期请使用 YYYY 或 YYYY-MM-DD。 / Use YYYY or YYYY-MM-DD.'
        ) from None


def site_matches(value, service_url):
    target = urlsplit(value if '://' in value else 'https://' + value)
    hostname = urlsplit(service_url).hostname or ''
    domain = target.hostname or ''
    return (hostname == domain or hostname.endswith('.' + domain)) and bool(domain), unquote(
        target.path or '/'
    )


def compile_branch(tokens, service_url):
    args = {
        'scope': '/',
        'mode': 'all',
        'limit': 100,
        'snippet': True,
        'fields': ['id', 'path', 'type', 'name', 'score', 'snippet'],
    }
    words, phrases, excluded, negative_phrases, sites, filetypes = [], [], [], [], [], []
    empty = False
    for raw in tokens:
        negative = raw.startswith('-')
        token = raw[1:] if negative else raw
        if token.startswith('site:'):
            matches, path = site_matches(token[5:].strip('"'), service_url)
            if negative:
                if matches:
                    sites.append(path)
            elif not matches:
                empty = True
            elif args['scope'] != '/' and args['scope'] != path:
                raise SearchSyntaxError(
                    '每个 OR 分支使用一个 site: 范围。 / One site scope per OR branch.'
                )
            else:
                args['scope'] = path
        elif token.startswith(('before:', 'after:')):
            if negative:
                raise SearchSyntaxError('日期操作符不能加减号。 / Do not negate date operators.')
            key, value = token.split(':', 1)
            args['updated_before' if key == 'before' else 'updated_after'] = date_operator(value)
        elif token.startswith('filetype:'):
            filetypes.append((token[9:].casefold().strip('.'), negative))
        elif token.startswith(('intitle:', 'inurl:', 'related:', 'cache:')):
            raise SearchSyntaxError(
                '当前支持引号、排除词、OR、site:、filetype:、before: 和 after:。 / This operator is not supported yet.'
            )
        elif token.startswith('"') and token.endswith('"'):
            (negative_phrases if negative else phrases).append(token[1:-1])
        else:
            (excluded if negative else words).append(token)
    if not words and not phrases:
        raise SearchSyntaxError('请在筛选条件之外输入关键词。 / Add a keyword to the filters.')
    if (
        len(words) > 8
        or len(excluded) > 8
        or any(not phrase for phrase in phrases + negative_phrases)
    ):
        raise SearchSyntaxError('关键词过多或引号为空。 / Too many terms or an empty phrase.')
    if words:
        args['terms'] = ' '.join(words)
    if excluded:
        args['not_terms'] = ' '.join(excluded)
    return args, phrases, negative_phrases, sites, filetypes, empty


async def search_results(query, *, service_url, execute):
    plans = [compile_branch(tokens, service_url) for tokens in parse_query(query)]
    deadline, calls = time.monotonic() + 20, 0
    results = {}

    async def collect(args):
        nonlocal calls
        found = {}
        for _ in range(3):
            calls += 1
            if calls > 20 or time.monotonic() > deadline:
                raise SearchSyntaxError('查询过于宽泛，请增加关键词。 / Narrow the query.')
            response = await execute(
                request_for('discovery.lexical_search', args, service_url, source='manual')
            )
            if response.error:
                if response.error.code in {'not_found', 'permission_denied', 'ancestor_inactive'}:
                    return {}
                raise SearchSyntaxError(
                    '搜索未完成：'
                    + response.error.code
                    + '。 / Search unavailable: '
                    + response.error.code
                )
            data = wire(response.data)
            found.update((item['id'], item) for item in data['items'])
            if not data.get('cursor'):
                return found
            args = {'cursor': data['cursor']}
        raise SearchSyntaxError('匹配内容过多，请增加关键词。 / Too many matches. Add a keyword.')

    for args, phrases, negatives, sites, filetypes, empty in plans:
        if empty:
            continue
        base = {**args, **({'exact': phrases[0]} if phrases else {})}
        found = await collect(base)
        for phrase in phrases[1:]:
            allowed = await collect({**args, 'exact': phrase})
            found = {rid: item for rid, item in found.items() if rid in allowed}
        for phrase in negatives:
            denied = await collect({**args, 'exact': phrase})
            found = {rid: item for rid, item in found.items() if rid not in denied}
        for rid, item in found.items():
            path = item['path']
            if any(path == scope or path.startswith(scope.rstrip('/') + '/') for scope in sites):
                continue
            if any(
                (item['name'].casefold().endswith('.' + ext)) == negative
                for ext, negative in filetypes
            ):
                continue
            results[rid] = item
    selected = sorted(results.values(), key=lambda item: (-item.get('score', 0), item['id']))[:20]
    for item in selected:
        snippet = item.get('snippet', {}).get('text', '')
        title = item['name']
        if item.get('type') == 'post':
            read = await execute(
                request_for(
                    'discovery.get',
                    {'id': item['id'], 'fields': ['content', 'name']},
                    service_url,
                    source='manual',
                )
            )
            if not read.error and isinstance(read.data.get('content'), str):
                preview = post_preview(title, read.data['content'])
                title = preview['title']
                snippet = preview['excerpt'] or snippet
            elif re.fullmatch(r'p_[0-9a-f]{32}(?:\.md)?', title):
                title = post_preview(title, snippet)['title']
        item['title'] = title
        item['excerpt'] = snippet
    return selected


WORDMARK = " _ __ ___   ___   __ _\n| '_ ` _ \\ / __| / _` |\n| | | | | |\\__ \\| (_| |\n|_| |_| |_||___/ \\__, |\n                 |___/"


def search_markdown(query, items, error=None):
    lines = ['# MSG Search / 搜索', '', f'Query / 关键词: {markdown_text(query)}', '']
    if error:
        lines += [markdown_text(error), '']
    for item in items:
        lines += [
            f'## [{markdown_text(item["title"])}]({quote(item["path"], safe="/@*&")})',
            '',
            markdown_text(item['excerpt']),
            '',
        ]
    if query and not items and not error:
        lines += ['No results. / 没有匹配结果。', '']
    return '\n'.join(lines)


def search_document(query, items, *, service_url, account=None, error=None):
    form = (
        '<form class="search-form" method="get" action="/search"><label for="msg-search-q" class="sr-only" data-i18n="search_query">Search</label><input id="msg-search-q" type="search" name="q" required maxlength="512" autofocus autocomplete="off" placeholder="Search MSG" data-i18n-placeholder="search_query" value="'
        + escape(query, quote=True)
        + '"><button type="submit" data-i18n="search_submit">Search</button></form>'
    )
    body = (
        '<section class="search-surface'
        + (' search-has-results' if query else '')
        + '"><h1 class="sr-only">MSG Search</h1><pre class="search-wordmark" aria-hidden="true">'
        + escape(WORDMARK)
        + '</pre>'
        + form
    )
    if error:
        body += '<p role="status" class="search-feedback">' + escape(str(error)) + '</p>'
    elif query:
        body += '<div class="search-results">'
        if not items:
            body += '<p data-i18n="search_empty">No results. Try different keywords.</p>'
        for item in items:
            path = quote(item['path'], safe='/@*&')
            body += (
                '<article><p class="search-result-path">'
                + escape(service_url + path)
                + '</p><h2><a href="'
                + escape(path, quote=True)
                + '">'
                + escape(item['title'])
                + '</a></h2><p>'
                + escape(item['excerpt'])
                + '</p></article>'
            )
        body += '</div>'
    template = service_url.rstrip('/') + '/search?q=%s'
    body += (
        '<details class="search-help"><summary data-i18n="search_help">Syntax & browser search engine</summary><p><code>"exact phrase" -word (python OR rust) site:'
        + escape(urlsplit(service_url).hostname or '')
        + ' after:2026-01-01</code></p><p data-i18n="search_local">Searches this MSG service and only content you can read.</p><p><code>'
        + escape(template)
        + '</code></p><button type="button" id="msg-copy-search-engine" data-search-template="'
        + escape(template, quote=True)
        + '" data-i18n="search_copy_engine">Copy search engine URL</button><p><a href="/opensearch.xml">OpenSearch</a> · <a href="https://support.google.com/websearch/answer/2466433">Google search syntax</a></p></details></section>'
    )
    return document_html(
        search_markdown(query, items, error),
        title='MSG Search' + (' · ' + query if query else ''),
        body_html=body,
        raw_path='/search',
        raw_query=urlencode({'q': query}) if query else '',
        service_url=service_url,
        account=account,
    )


def opensearch_document(service_url):
    template = escape(service_url.rstrip('/') + '/search?q={searchTerms}', quote=True)
    return (
        '<?xml version="1.0" encoding="UTF-8"?><OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/"><ShortName>MSG</ShortName><Description>Search MSG</Description><InputEncoding>UTF-8</InputEncoding><Url type="text/html" method="get" template="'
        + template
        + '"/></OpenSearchDescription>'
    ).encode()
