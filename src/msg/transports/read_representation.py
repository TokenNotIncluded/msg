"""Small public read views default to text; browsers explicitly opt into HTML."""

import math

from msg.core.errors import require


def representation(accept):
    ranges = []
    for index, item in enumerate(accept.casefold().split(',')):
        media, *parameters = item.strip().split(';')
        quality = 1.0
        for parameter in parameters:
            key, _, value = parameter.strip().partition('=')
            if key == 'q':
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
                if not math.isfinite(quality) or not 0 <= quality <= 1:
                    quality = 0.0
        ranges.append((media.strip(), quality, index))

    choices = []
    for priority, media in enumerate(('text/html', 'application/json', 'text/markdown')):
        matches = []
        for pattern, quality, index in ranges:
            specificity = (
                2
                if pattern == media
                else 1
                if pattern == media.split('/')[0] + '/*'
                else 0
                if pattern == '*/*'
                else -1
            )
            if specificity >= 0:
                matches.append((specificity, -index, quality))
        if matches:
            specificity, order, quality = max(matches)
            if quality > 0:
                choices.append((quality, specificity, order, priority, media))
    require(choices or not accept.strip(), 'not_acceptable')
    return max(choices)[-1] if choices else 'text/markdown'
