"""Database-backed checks for the real OAuth-to-protocol transport boundary."""

import pytest
from test_browser_sessions import signed_browser as signed_browser


@pytest.mark.asyncio
async def test_non_ascii_csrf_is_bad_request_not_server_error(signed_browser):
    _, _, _, http = signed_browser
    assert (await http.get('/login')).status_code == 200
    response = await http.post(
        '/session/poll',
        json={'csrf': '非ASCII'},
        headers={'Origin': str(http.base_url).rstrip('/')},
    )
    assert response.status_code == 400 and response.json() == {'error': 'invalid_request'}
