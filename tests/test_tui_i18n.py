"""Locale precedence and localized UI behavior without changing read contracts."""

from io import StringIO

import pytest
from test_tui import FakeClient

from msg.tui import run_tui
from msg.tui_i18n import MESSAGES, locale_language


@pytest.mark.parametrize(
    ('env', 'expected'),
    [
        ({}, 'en'),
        ({'LANG': 'zh_CN.UTF-8'}, 'zh_Hans'),
        ({'LANG': 'zh-TW.UTF-8'}, 'zh_Hant'),
        ({'LANG': 'zh_HK'}, 'zh_Hant'),
        ({'LANG': 'zh-Hant'}, 'zh_Hant'),
        ({'LC_ALL': 'C', 'LC_MESSAGES': 'zh_CN', 'LANG': 'zh_TW'}, 'en'),
        ({'LC_ALL': '', 'LC_MESSAGES': 'zh_TW', 'LANG': 'en_US'}, 'zh_Hant'),
        ({'LC_MESSAGES': 'en_GB.UTF-8', 'LANG': 'zh_CN'}, 'en'),
        ({'LANG': 'fr_FR.UTF-8'}, 'en'),
        ({'LANG': 'POSIX'}, 'en'),
    ],
)
def test_message_locale_precedence(env, expected):
    assert locale_language(env) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('locale', 'home', 'anonymous', 'failure'),
    [
        ('en_US.UTF-8', 'Home', 'Not signed in', 'Read failed: access_denied'),
        ('zh_CN.UTF-8', '首页', '未登录', '读取失败：access_denied'),
        ('zh_TW.UTF-8', '首頁', '未登入', '讀取失敗：access_denied'),
    ],
)
async def test_environment_localizes_full_session(monkeypatch, locale, home, anonymous, failure):
    monkeypatch.setenv('LC_ALL', locale)
    client = FakeClient()
    client.responses['discovery.get'] = 'access_denied'
    output = StringIO()
    await run_tui(client, stdin=StringIO('id\ni\nr p_missing\nq\n'), stdout=output, width=160)
    rendered = output.getvalue()
    assert home in rendered and anonymous in rendered and failure in rendered
    assert client.calls == [
        ('discovery.read_query', {'parent': '/', 'limit': 20}),
        ('discovery.get', {'id': 'p_missing'}),
    ]


def test_catalogs_have_matching_coverage():
    assert MESSAGES['zh_Hans'].keys() == MESSAGES['zh_Hant'].keys()
