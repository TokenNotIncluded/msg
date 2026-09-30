"""Malformed Range fields have bounded parsing and stable client errors."""

import pytest
from starlette.requests import Request

from msg.core.errors import Failure
from msg.extensions.hosting import hosted_range


@pytest.mark.parametrize(
    'value',
    ['bytes=' + '9' * 5000 + '-', 'bytes=0-' + '9' * 5000, 'bytes=-' + '9' * 5000],
    ids=['start', 'end', 'suffix'],
)
def test_hosted_range_rejects_oversized_numbers_with_contract_error(value):
    request = Request({'type': 'http', 'headers': [(b'range', value.encode())]})
    with pytest.raises(Failure, match='range_not_satisfiable'):
        hosted_range(request, 12, '"etag"')


@pytest.mark.parametrize(
    'value,expected',
    [('bytes=0-4', (0, 5)), ('bytes=5-', (5, 12)), ('bytes=-5', (7, 12)), ('bytes=-99', (0, 12))],
)
def test_hosted_range_keeps_normal_prefix_open_and_suffix_ranges(value, expected):
    request = Request({'type': 'http', 'headers': [(b'range', value.encode())]})
    interval, status, _ = hosted_range(request, 12, '"etag"')
    assert interval == expected and status == 206


def test_stale_if_range_ignores_range_before_parsing():
    request = Request({
        'type': 'http',
        'headers': [(b'range', b'bytes=' + b'9' * 5000 + b'-'), (b'if-range', b'"stale"')],
    })
    assert hosted_range(request, 12, '"etag"') == ((0, 12), 200, {})


@pytest.mark.asyncio
async def test_raw_http_rejects_oversized_range_without_read_effects(installed, monkeypatch):
    import httpx
    from read_only_evidence import readonly_evidence
    from test_service import call, register

    from msg.transports.http import create_app

    app, _ = installed
    key, subject, _ = await register(app, 'range-reader')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'range content'},
        key=key,
        subject=subject,
    )
    assert created.status == 'ok'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        async with readonly_evidence(app, monkeypatch):
            for value in (
                'bytes=' + '9' * 5000 + '-',
                'bytes=0-' + '9' * 5000,
                'bytes=-' + '9' * 5000,
            ):
                result = await http.get(
                    '/_id/' + created.resources[0].id + '/raw', headers={'Range': value}
                )
                assert result.status_code == 416
                assert result.json()['error']['code'] == 'range_not_satisfiable'
