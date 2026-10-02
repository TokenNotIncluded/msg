"""One shared SVG + text panel, with atomic revision and account/global budgets."""

import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from zoneinfo import ZoneInfo

from msg.core.codec import parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput
from msg.plugins.common import operation_id
from msg.plugins.schemas import obj

KEY = 'public_board_v1'
SVG_BYTES = 16384
TEXT_BYTES = 8192
TEXT_CHARACTERS = 2000
LIMITS = {
    'account_hour': 5,
    'account_day': 20,
    'global_hour': 30,
    'global_day': 300,
    'cooldown_seconds': 60,
    'svg_bytes': SVG_BYTES,
    'text_characters': TEXT_CHARACTERS,
    'text_bytes': TEXT_BYTES,
    'elements': 256,
    'animations': 32,
    'batch_size': 1,
}


def default_art():
    """ASCII lettering with a slow, staggered highlight; also valid user SVG."""
    glyphs = (
        ('10001', '11011', '10101', '10001', '10001', '10001', '10001'),
        ('01111', '10000', '10000', '01110', '00001', '00001', '11110'),
        ('01110', '10001', '10000', '10111', '10001', '10001', '01110'),
    )
    tokens = ('[]', '//', '::', '{}', '01', '++')
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 300">',
        '<title>MSG, written in ASCII</title>',
        '<g font-family="monospace" font-size="24" fill="#d6d6da">',
    ]
    for letter, rows in enumerate(glyphs):
        for column in range(5):
            parts.append('<g>')
            for row, pixels in enumerate(rows):
                if pixels[column] == '1':
                    token = tokens[(row + column + letter) % len(tokens)]
                    parts.append(
                        f'<text x="{60 + (letter * 6 + column) * 51}" y="{63 + row * 27}">{token}</text>'
                    )
            parts.append(
                f'<animate attributeName="fill" values="#d6d6da;#d6d6da;#88baff;#d6d6da" keyTimes="0;0.65;0.8;1" dur="12s" begin="-{(letter * 5 + column) * 0.4:.1f}s" repeatCount="indefinite"/></g>'
            )
    parts.append(
        '</g><g font-family="monospace" font-size="13" fill="#77777f">'
        '<text x="60" y="282">[ a shared surface. leave a signal. ]</text>'
        '<text x="900" y="282" text-anchor="end">svg + text</text></g></svg>'
    )
    return ''.join(parts)


DEFAULT_SVG = default_art()
DEFAULT_TEXT = '留下你的信号。\n下一位访客，接着写。'

ELEMENTS = {
    'svg',
    'g',
    'path',
    'rect',
    'circle',
    'ellipse',
    'line',
    'polyline',
    'polygon',
    'text',
    'tspan',
    'title',
    'desc',
    'animate',
    'animateTransform',
}
ATTRIBUTES = {
    'viewBox',
    'width',
    'height',
    'x',
    'y',
    'x1',
    'y1',
    'x2',
    'y2',
    'cx',
    'cy',
    'r',
    'rx',
    'ry',
    'd',
    'points',
    'fill',
    'stroke',
    'stroke-width',
    'opacity',
    'fill-opacity',
    'stroke-opacity',
    'stroke-linecap',
    'stroke-linejoin',
    'transform',
    'font-size',
    'font-family',
    'text-anchor',
    'dominant-baseline',
    'attributeName',
    'values',
    'from',
    'to',
    'dur',
    'begin',
    'repeatCount',
    'type',
    'keyTimes',
    'calcMode',
    'keySplines',
    'additive',
    'accumulate',
}
ANIMATED = {
    'x',
    'y',
    'x1',
    'x2',
    'y1',
    'y2',
    'cx',
    'cy',
    'r',
    'rx',
    'ry',
    'width',
    'height',
    'opacity',
    'fill-opacity',
    'stroke-opacity',
    'fill',
    'stroke',
    'stroke-width',
    'transform',
}


def validate_svg(value):
    require(len(value.encode()) <= SVG_BYTES, 'public_board_svg_too_large')
    require(not re.search(r'<!|<\?', value), 'public_board_unsafe_svg')
    try:
        root = ET.fromstring(value)
    except (ET.ParseError, ValueError) as exc:
        raise Failure('public_board_invalid_svg') from exc
    require(root.tag == '{http://www.w3.org/2000/svg}svg', 'public_board_invalid_svg')
    require(root.get('viewBox') == '0 0 960 300', 'public_board_viewbox_required')
    require(
        root.get('width', '960') == '960' and root.get('height', '300') == '300',
        'public_board_viewbox_required',
    )
    elements = list(root.iter())
    require(len(elements) <= LIMITS['elements'], 'public_board_svg_too_complex')
    animations = 0
    for element in elements:
        require(
            element.tag.startswith('{http://www.w3.org/2000/svg}')
            and element.tag.split('}', 1)[1] in ELEMENTS,
            'public_board_unsafe_svg',
        )
        tag = element.tag.split('}', 1)[1]
        for key, attr in element.attrib.items():
            require(
                key in ATTRIBUTES
                and len(attr) <= 4096
                and not re.search(r'url\s*\(|[<>]|[\x00-\x08\x0b\x0c\x0e-\x1f]', attr, re.I),
                'public_board_unsafe_svg',
            )
        if tag in {'animate', 'animateTransform'}:
            animations += 1
            require(element.get('attributeName') in ANIMATED, 'public_board_unsafe_svg')
            duration = element.get('dur', '')
            require(
                re.fullmatch(r'\d+(?:\.\d+)?s', duration) is not None
                and 1 <= float(duration[:-1]) <= 120,
                'public_board_animation_duration',
            )
            require(
                re.fullmatch(r'-?\d+(?:\.\d+)?s', element.get('begin', '0s')) is not None,
                'public_board_unsafe_svg',
            )
            require(
                element.get('repeatCount', '1') == 'indefinite'
                or re.fullmatch(r'[1-9]\d{0,2}', element.get('repeatCount', '1')) is not None,
                'public_board_unsafe_svg',
            )
            if tag == 'animateTransform':
                require(
                    element.get('attributeName') == 'transform'
                    and element.get('type') in {'translate', 'scale', 'rotate', 'skewX', 'skewY'},
                    'public_board_unsafe_svg',
                )
    require(animations <= LIMITS['animations'], 'public_board_svg_too_complex')
    return value


def default():
    return {
        'generation': 0,
        'svg': DEFAULT_SVG,
        'text': DEFAULT_TEXT,
        'updated_at': None,
        'updated_by': None,
        'history': [],
    }


def periods(now):
    local = now.astimezone(ZoneInfo('Asia/Taipei'))
    return local.strftime('%Y-%m-%dT%H'), local.strftime('%Y-%m-%d')


def quota(record, now):
    hour, day = periods(now)
    record = dict(record or {})
    if record.get('hour') != hour:
        record.update(hour=hour, hour_count=0)
    if record.get('day') != day:
        record.update(day=day, day_count=0)
    return record


def projection(record, account_quota=None):
    return {
        **{k: v for k, v in record.items() if k != 'history'},
        'limits': LIMITS,
        'timezone': 'Asia/Taipei',
        'quota': account_quota,
        'history': [
            {k: v for k, v in item.items() if k not in {'svg', 'text'}}
            for item in record['history']
        ],
    }


def install(app, op):
    @op('discovery.public_board', obj(), effect='read')
    async def get(ctx, request, tx):
        own = None
        if ctx.principal.subject:
            own = quota(tx.setting('public_board_quota:' + ctx.principal.subject), ctx.now)
        return HandlerOutput(data=projection(tx.setting(KEY, default()), own))

    @op(
        'content.public_board_update',
        obj(
            {
                'generation': {'type': 'integer', 'minimum': 0},
                'svg': {'type': 'string', 'minLength': 1, 'maxLength': SVG_BYTES},
                'text': {'type': 'string', 'maxLength': TEXT_CHARACTERS},
            },
            ('generation',),
        ),
    )
    async def update(ctx, request, tx):
        subject = ctx.principal.subject
        require(subject is not None, 'authentication_required')
        await app.authorizer.require_base(ctx.principal, operation_id(request), subject, tx)
        require(tx.setting('runtime_config', {}).get('accept_writes', True), 'writes_paused')
        args = request.arguments
        require('svg' in args or 'text' in args, 'public_board_content_required')
        record = tx.setting(KEY, default())
        require(args['generation'] == record['generation'], 'public_board_conflict')
        svg = validate_svg(args.get('svg', record['svg']))
        text = args.get('text', record['text'])
        require(len(text.encode()) <= TEXT_BYTES, 'public_board_text_too_large')
        require(not any(ord(char) < 32 and char not in '\n\t' for char in text), 'invalid_text')
        require(svg != record['svg'] or text != record['text'], 'public_board_unchanged')
        key = 'public_board_quota:' + subject
        own = quota(tx.setting(key), ctx.now)
        total = quota(tx.setting('public_board_global_quota'), ctx.now)
        require(
            own['hour_count'] < LIMITS['account_hour'] and own['day_count'] < LIMITS['account_day'],
            'public_board_rate_limited',
        )
        require(
            total['hour_count'] < LIMITS['global_hour']
            and total['day_count'] < LIMITS['global_day'],
            'public_board_global_rate_limited',
        )
        if own.get('last_update'):
            require(
                ctx.now
                >= parse_time(own['last_update']) + timedelta(seconds=LIMITS['cooldown_seconds']),
                'public_board_cooldown',
            )
        for budget in (own, total):
            budget['hour_count'] += 1
            budget['day_count'] += 1
            budget['last_update'] = wire(ctx.now)
        tx.set_setting(key, own)
        tx.set_setting('public_board_global_quota', total)
        saved = {k: v for k, v in record.items() if k != 'history'}
        record = {
            'generation': record['generation'] + 1,
            'svg': svg,
            'text': text,
            'updated_by': subject,
            'updated_at': wire(ctx.now),
            'history': [saved, *record['history']][:20],
        }
        tx.set_setting(KEY, record)
        return HandlerOutput(data=projection(record, own))
