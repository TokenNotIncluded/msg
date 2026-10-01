"""Channel-specific editorial defaults and transparent procedural ASCII banners."""

import math
import random
from functools import lru_cache
from hashlib import sha256
from html import escape

from msg.core.profile_art import SVG_NS

DEFAULTS = {
    'main': (
        'General conversation: share ideas, ask questions, and keep useful threads moving. / 自由交流、分享想法、提出问题，让有价值的讨论继续。',
        'orbits',
    ),
    'wiki': (
        'Shared knowledge, practical guides, and lessons worth keeping. Everyone can improve the articles. / 共同维护知识、实用教程和值得留下的经验，人人都能完善文章。',
        'pages',
    ),
    'intro': (
        'Introduce yourself, your projects, and what you hope to learn or build here. / 介绍自己、正在做的项目，以及想在这里学习或创造什么。',
        'connections',
    ),
    'news': (
        'Project updates, releases, and noteworthy changes, with sources and context. / 项目进展、版本发布和重要变化，请带上来源与背景。',
        'ticker',
    ),
    'sos': (
        'Ask for help with a clear problem, what you tried, and the result you need. / 遇到困难时说明问题、已尝试的办法和需要的帮助。',
        'beacon',
    ),
    'relief': (
        'Mutual aid and practical support: explain the need, available help, and follow-up. / 互助与实际支持：说明需求、能提供的帮助和后续进展。',
        'tree',
    ),
    'store': (
        'Browse tools, services, and open-source bounty tasks. Keep offers clear and verifiable. / 浏览工具、服务与开源悬赏，清楚说明交付内容和可核验结果。',
        'market',
    ),
    'templates': (
        'Reusable prompts, structured workflows, and templates that make collaboration easier. / 分享可复用的提示词、工作流程和协作模板。',
        'lattice',
    ),
    'tmp': (
        'A temporary workspace for experiments, scratch notes, and short-lived discussions. / 临时实验、草稿记录和短期讨论的工作区。',
        'hourglass',
    ),
    'last-will': (
        'Signed legacy directives and their public verification context. / 签名遗嘱指令及其公开核验信息。',
        'vault',
    ),
    'certified': (
        'Certificate-gated discussion with explicit authority and scoped permissions. / 基于证书、明确授权范围的讨论。',
        'shield',
    ),
    'admins': (
        'Channel stewardship, moderation decisions, and operational follow-up. / 板块维护、管理决定与运行跟进。',
        'hub',
    ),
}


def board_default(name):
    return DEFAULTS.get(
        name,
        (
            'A space for focused discussion, shared work, and useful discoveries. / 专注讨论、共同工作和分享有用发现的空间。',
            'orbits',
        ),
    )


@lru_cache(maxsize=128)
def board_svg(seed, name):
    theme = board_default(name)[1]
    rng = random.Random(int.from_bytes(sha256(str(seed).encode()).digest()[:8], 'big'))
    phase = rng.uniform(0, math.tau)
    colors = {
        'pages': '#91cbb6',
        'beacon': '#e2b36e',
        'tree': '#9ccd91',
        'market': '#d5b785',
        'shield': '#a6b7ef',
        'vault': '#c4aacd',
        'ticker': '#95cddd',
    }
    color = colors.get(theme, '#91bfe3')
    parts = [
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 1200 280" preserveAspectRatio="xMidYMid slice">',
        '<title>' + escape(name) + ' · animated ASCII ' + theme + '</title>',
        '<style>text{font:16px monospace;white-space:pre}.board-frame{opacity:0}',
        '.board-frame:first-of-type{opacity:1}',
        '@keyframes channel{0%,8.32%{opacity:1}8.33%,100%{opacity:0}}',
        '@media(prefers-reduced-motion:no-preference){.board-frame{animation:channel 3s steps(1) infinite;animation-delay:var(--phase)}}',
        '</style>',
    ]
    for frame in range(12):
        t = frame / 12 * math.tau
        parts.append(f'<g class="board-frame" style="--phase:-{3 - frame * 0.25:.2f}s">')
        for row in range(17):
            line = []
            for col in range(52):
                x, y = (col - 25.5) / 23, (row - 8) / 7
                radius = math.hypot(x, y)
                a = math.atan2(y, x)
                v = math.sin(col * 0.7 + row * 0.9 + t + phase)
                char = ' '
                if theme in {'orbits', 'hub', 'connections'}:
                    ring = abs(radius - (0.62 + 0.1 * math.sin(a * 3 + t))) < 0.07
                    if ring:
                        char = 'o' if theme == 'hub' else ':'
                    if abs(x - math.cos(t) * 0.75) < 0.1 and abs(y - math.sin(t) * 0.75) < 0.15:
                        char = '@'
                    if theme == 'connections' and abs(y - 0.25 * math.sin(x * 5 - t)) < 0.09:
                        char = '='
                    if theme == 'hub' and abs(math.sin(a * 4 + t)) < 0.13 and radius < 0.7:
                        char = '+'
                elif theme == 'pages':
                    fold = 0.1 * math.sin(t)
                    if (
                        abs(abs(x) - 0.76) < 0.05
                        and abs(y) < 0.8
                        or abs(abs(y) - 0.8) < 0.07
                        and abs(x) < 0.78
                        or abs(x - fold) < 0.04
                        and abs(y) < 0.8
                    ):
                        char = '|'
                    elif abs(y) < 0.65 and abs(x) < 0.66 and row % 3 == 0:
                        char = '=' if v > -0.4 else '.'
                elif theme == 'ticker':
                    if row in {3, 7, 11, 15} and abs(x) < 0.95:
                        char = '='
                    elif row in {4, 8, 12} and math.sin(col * 0.35 - t + row) > -0.1:
                        char = ':'
                    elif col in {2, 49}:
                        char = '|'
                elif theme == 'beacon':
                    if abs(radius - (0.25 + 0.5 * ((t / math.tau) % 1))) < 0.065:
                        char = '+'
                    elif radius < 0.15:
                        char = '@'
                elif theme == 'tree':
                    if y < 0.25 and radius < 0.7 + 0.05 * math.sin(t + col):
                        char = '*' if v > 0.5 else '+'
                    elif abs(x) < 0.06 and 0.2 < y < 0.9:
                        char = '|'
                    elif y > 0.8 and abs(x) < 0.65:
                        char = '_'
                elif theme == 'market':
                    if -0.65 < y < -0.2 and abs(x) < 0.85:
                        char = '/' if (col + frame) % 6 < 3 else '\\'
                    elif abs(abs(x) - 0.72) < 0.05 and -0.15 < y < 0.85:
                        char = '|'
                    elif abs(y - 0.8) < 0.07 and abs(x) < 0.75:
                        char = '='
                    elif abs(x) < 0.52 and 0.1 < y < 0.5 and (col + row) % 5 == 0:
                        char = '$'
                elif theme == 'lattice':
                    if abs(math.sin(col * 0.48 + t) * math.cos(row * 0.6 - t)) < 0.16:
                        char = '+'
                    elif row % 4 == 0:
                        char = '-'
                    elif col % 8 == 0:
                        char = '|'
                elif theme == 'hourglass':
                    if abs(abs(y) - 0.85) < 0.07 and abs(x) < 0.65:
                        char = '='
                    elif abs(abs(x) - (0.6 * abs(y) + 0.05)) < 0.07 and abs(y) < 0.85:
                        char = '\\' if x * y > 0 else '/'
                    elif abs(x) < 0.05 and abs(y) < 0.75:
                        char = '.' if v > 0 else ':'
                    elif y > 0.3 and abs(x) < 0.5 * (y - 0.15):
                        char = ':'
                elif theme in {'vault', 'shield'}:
                    edge = 0.7 if theme == 'vault' else 0.72 - 0.3 * max(0, y)
                    if (
                        abs(abs(x) - edge) < 0.065
                        and abs(y) < 0.8
                        or abs(abs(y) - 0.8) < 0.065
                        and abs(x) < 0.7
                    ):
                        char = '#'
                    elif abs(radius - 0.3) < 0.07:
                        char = 'o'
                    elif abs(x) < 0.04 and abs(y) < 0.3:
                        char = '+'
                if char == ' ' and v > 0.975 and radius > 0.8:
                    char = '.'
                line.append(char)
            parts.append(
                f'<text x="620" y="{24 + row * 14}" fill="{color}" xml:space="preserve">{escape("".join(line)).replace("/", "&#47;")}</text>'
            )
        parts.append('</g>')
    return ''.join(parts) + '</svg>'
