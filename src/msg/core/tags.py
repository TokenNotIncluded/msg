"""Canonical user tags; labels never participate in authorization."""

from __future__ import annotations

import unicodedata

from msg.core.errors import require


def normalize_tag(value: str) -> str:
    require(type(value) is str, 'invalid_tag')
    tag = unicodedata.normalize('NFKC', value).strip().casefold()
    tag = unicodedata.normalize('NFC', tag)
    require(1 <= len(tag) <= 48 and len(tag.encode('utf-8')) <= 96, 'invalid_tag')
    require(
        tag[0].isalnum() and all(char.isalnum() or char in {'-', '_'} for char in tag),
        'invalid_tag',
    )
    return tag


def normalize_tags(values) -> tuple[str, ...]:
    require(type(values) in {list, tuple} and len(values) <= 16, 'invalid_tags')
    return tuple(sorted({normalize_tag(value) for value in values}))
