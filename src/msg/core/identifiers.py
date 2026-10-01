"""Compact hexadecimal references without changing signed legacy identifiers."""

import hashlib
import re

HEX_ID_PATTERN = r'[0-9a-f]{32}'
PREFIXED_HEX_ID_PATTERN = r'[A-Za-z][A-Za-z0-9_]*_([0-9a-f]{32})'


def hex_id(value):
    if re.fullmatch(HEX_ID_PATTERN, value):
        return value
    match = re.fullmatch(PREFIXED_HEX_ID_PATTERN, value)
    return (
        match[1]
        if match
        else hashlib.sha256(b'msg.hex-reference/v1\0' + value.encode()).hexdigest()[:32]
    )


def hex_references(value):
    """Convert identifier fields in read projections; preserve content and paths."""
    if isinstance(value, list):
        return [hex_references(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {
            'id',
            'revision',
            'parent',
            'owner',
            'group',
            'actor',
            'author',
            'subject',
            'source_id',
            'resource_id',
            'subject_id',
            'created_by',
            'modified_by',
            'root',
            'reply_to',
            'thread_root',
            'old_revision',
            'new_revision',
        } and isinstance(item, str):
            result[key] = hex_id(item)
        elif key in {'parents', 'ids'} and isinstance(item, list):
            result[key] = [hex_id(entry) for entry in item]
        elif key in {'content', 'diff', 'signature', 'receipt'}:
            result[key] = item
        else:
            result[key] = hex_references(item)
    if isinstance(value.get('path'), str):
        ref = value.get('ref', {})
        rid = value.get('id') or ref.get('id')
        if rid:
            result['path'] = '/*' + hex_id(rid)
            if '/rev/' in value['path'] and ref.get('revision'):
                result['path'] += '/rev/' + hex_id(ref['revision'])
    return result
