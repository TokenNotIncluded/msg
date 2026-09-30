"""Exact phrases must occur within a real searchable field."""

import pytest
from test_service import call, register

from msg.core.codec import wire


@pytest.mark.asyncio
async def test_exact_phrase_does_not_cross_name_body_boundary(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'phrase-boundary-owner')
    split = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'name': 'boundaryalpha', 'body': 'boundarybeta'},
        key=key,
        subject=subject,
    )
    complete = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'name': 'complete', 'body': 'boundaryalpha.md boundarybeta'},
        key=key,
        subject=subject,
    )
    assert split.status == complete.status == 'ok'
    result = await call(
        app,
        'discovery.lexical_search',
        {
            'scope': '/main',
            'exact': 'boundaryalpha.md boundarybeta',
            'snippet': True,
            'explain': 'compact',
        },
    )
    assert result.status == 'ok', wire(result)
    assert [item['id'] for item in result.data['items']] == [complete.resources[0].id]
    item = result.data['items'][0]
    assert item['snippet']['field'] == 'body'
    assert list(item['rank_reason']['matched_fields']) == ['body']

    # Independent terms can still match different fields of the same resource.
    terms = await call(
        app,
        'discovery.lexical_search',
        {'scope': '/main', 'terms': 'boundaryalpha boundarybeta', 'mode': 'all'},
    )
    assert terms.status == 'ok', wire(terms)
    assert {item['id'] for item in terms.data['items']} == {
        split.resources[0].id,
        complete.resources[0].id,
    }
