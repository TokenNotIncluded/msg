"""JSON 的人类阅读布局；只调整结构符号外的空白。"""

import json
import re

_TOKENS = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\],:]|[^\s{}\[\],:]+')
_MAX_BYTES = 200000
_MAX_DEPTH = 64


def _not_json(value):
    raise ValueError(value)


def json_display(source):
    if len(source) > _MAX_BYTES or not source.lstrip().startswith(('{', '[')):
        return source
    try:
        if len(source.encode()) > _MAX_BYTES:
            return source
        # 校验不把数字变成浮点或整数，也不以解码后的对象重新序列化。
        json.loads(
            source,
            parse_int=str,
            parse_float=str,
            parse_constant=_not_json,
            object_pairs_hook=lambda pairs: None,
        )
    except ValueError, RecursionError, UnicodeEncodeError:
        return source

    tokens = _TOKENS.findall(source)
    depth = 0
    for token in tokens:
        if token in {'{', '['}:
            depth += 1
            if depth > _MAX_DEPTH:
                return source
        elif token in {'}', ']'}:
            depth -= 1

    output = []
    size = depth = 0
    for index, token in enumerate(tokens):
        if token in {'{', '['}:
            depth += 1
            next_token = tokens[index + 1] if index + 1 < len(tokens) else ''
            piece = token if next_token in {'}', ']'} else token + '\n' + '  ' * depth
        elif token in {'}', ']'}:
            depth -= 1
            previous = tokens[index - 1] if index else ''
            piece = token if previous in {'{', '['} else '\n' + '  ' * depth + token
        elif token == ',':
            piece = ',\n' + '  ' * depth
        elif token == ':':
            piece = ': '
        else:
            piece = token
        size += len(piece.encode())
        # 深层数组的缩进也必须有上限，避免把小原文展开成巨型 HTML。
        if size > _MAX_BYTES:
            return source
        output.append(piece)
    return ''.join(output)
