"""Bounded, byte-preserving text edits. No fuzzy matching or executable syntax.

A heading edit replaces its complete Markdown section, including its heading.
A block is a heading, fenced code block, or contiguous nonblank text. Empty
separator lines are not included. Digests cover the selected original UTF-8.
Unified patches describe one file; their filenames never select a resource.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from msg.core.codec import digest, wire
from msg.core.errors import require

PATCH_LIMIT = 1048576
PATCH_CONTEXT_LIMIT = 256
PATCH_CANDIDATE_LIMIT = 4096
MAX_HUNKS = 256
MAX_LINES = 65536

_TEXT = {'type':'string', 'maxLength':PATCH_LIMIT}
_DIGEST = {'type':'string', 'pattern':r'^sha256:[0-9a-f]{64}$'}


def _variant(kind, properties, required):
    return {'type':'object', 'properties':{'kind':{'const':kind}, **properties},
            'required':['kind', *required], 'additionalProperties':False}


PATCH_SCHEMA = {'oneOf':[
    _variant('exact', {'exact':{**_TEXT, 'minLength':1}, 'replacement':_TEXT,
        'before':{'type':'string','maxLength':PATCH_CONTEXT_LIMIT},
        'after':{'type':'string','maxLength':PATCH_CONTEXT_LIMIT}}, ('exact','replacement')),
    _variant('unified', {'diff':{**_TEXT, 'minLength':1}}, ('diff',)),
    _variant('heading', {'heading':{'type':'string','minLength':1,'maxLength':256},
        'digest':_DIGEST, 'replacement':_TEXT}, ('heading','digest','replacement')),
    _variant('block', {'digest':_DIGEST, 'replacement':_TEXT}, ('digest','replacement')),
]}


def validate_patch(patch):
    # The public registry validates the same closed schema. Keep this guard for
    # direct callers and batch preparation, before any content is persisted.
    from jsonschema import Draft202012Validator
    require(Draft202012Validator(PATCH_SCHEMA).is_valid(wire(patch)), 'invalid_text_patch')
    require(sum(len(value.encode('utf-8')) for value in patch.values()
                if isinstance(value,str)) <= PATCH_LIMIT, 'patch_too_large')


def apply_text_patch(source, exact, replacement, before='', after=''):
    """Legacy contextual replacement, with unchanged ambiguity semantics."""
    require(bool(exact), 'patch_exact_required')
    match = None
    start = candidates = 0
    while True:
        index = source.find(exact, start)
        if index < 0:
            break
        candidates += 1
        require(candidates <= PATCH_CANDIDATE_LIMIT, 'patch_too_complex')
        end = index + len(exact)
        if (index >= len(before) and source.startswith(before,index-len(before))
                and source.startswith(after,end)):
            require(match is None, 'patch_ambiguous')
            match = index
        start = index + 1
    require(match is not None, 'patch_no_match')
    result = source[:match] + replacement + source[match+len(exact):]
    require(len(result.encode('utf-8')) <= PATCH_LIMIT, 'patch_too_large')
    return result


@dataclass(frozen=True)
class Hunk:
    old_start: int
    new_start: int
    old: tuple[str, ...]
    new: tuple[str, ...]


_HUNK = re.compile(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:[^\r\n]*)\r?\n?\Z')
_NO_NEWLINE = '\\ No newline at end of file'


def _diff_lines(text):
    # Git/unified diff separates lines only at LF. str.splitlines also splits
    # Unicode separators and CR, changing the signed bytes and hunk counts.
    return re.findall(r'[^\n]*\n|[^\n]+$', text)


def _hunks(text):
    lines = _diff_lines(text)
    require(len(lines) <= MAX_LINES, 'patch_too_complex')
    i = 0
    if lines and lines[0].startswith('--- '):
        require(len(lines)>1 and lines[1].startswith('+++ '), 'invalid_unified_patch')
        i = 2
    hunks = []
    while i < len(lines):
        match = _HUNK.fullmatch(lines[i])
        require(match is not None and len(hunks)<MAX_HUNKS, 'invalid_unified_patch')
        old_no, old_count, new_no, new_count = (
            int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1))
        require((not old_count or old_no>0) and (not new_count or new_no>0),
                'invalid_unified_patch')
        i += 1
        old, new = [], []
        previous = None
        while i < len(lines) and not lines[i].startswith('@@ '):
            line = lines[i]
            if line.rstrip('\r\n') == _NO_NEWLINE:
                require(previous is not None, 'invalid_unified_patch')
                for side in previous:
                    require(side[-1].endswith('\n'), 'invalid_unified_patch')
                    side[-1] = side[-1][:-1]
                previous = None
            else:
                require(line[:1] in {' ','-','+'}, 'invalid_unified_patch')
                # Once counts are exhausted, another file header is not a hunk.
                require(len(old)<old_count or len(new)<new_count, 'invalid_unified_patch')
                previous = []
                if line[0] in ' -':
                    old.append(line[1:]); previous.append(old)
                if line[0] in ' +':
                    new.append(line[1:]); previous.append(new)
            i += 1
        require(len(old)==old_count and len(new)==new_count, 'invalid_unified_patch')
        hunks.append(Hunk(old_no-1 if old_count else old_no,
                          new_no-1 if new_count else new_no, tuple(old), tuple(new)))
    require(bool(hunks), 'invalid_unified_patch')
    return hunks


def _unified(source, hunks, *, rebase=False):
    lines = _diff_lines(source)
    require(len(lines) <= MAX_LINES, 'patch_too_complex')
    require(not rebase or len(lines)*len(hunks)<=1000000, 'patch_too_complex')
    result = []
    last = delta = 0
    for hunk in hunks:
        start = hunk.old_start
        if rebase:
            # Insertions without an unchanged context have no safe locator.
            require(bool(hunk.old), 'patch_rebase_unsupported')
            # Exact sequence search in linear time (KMP), not quadratic scanning
            # of a large context at every repeated line.
            starts = _sequence_matches(lines,hunk.old)
            require(bool(starts), 'patch_no_match')
            require(len(starts)==1, 'patch_ambiguous')
            start = starts[0]
        else:
            require(hunk.new_start==hunk.old_start+delta, 'invalid_unified_patch')
        require(last<=start<=len(lines), 'invalid_unified_patch')
        end = start+len(hunk.old)
        require(tuple(lines[start:end])==hunk.old, 'patch_no_match')
        result.extend(lines[last:start]); result.extend(hunk.new)
        last = end
        delta += len(hunk.new)-len(hunk.old)
    result.extend(lines[last:])
    return ''.join(result)


def _sequence_matches(lines, needle):
    prefix = [0]*len(needle)
    j = 0
    for i in range(1,len(needle)):
        while j and needle[i]!=needle[j]:
            j = prefix[j-1]
        if needle[i]==needle[j]:
            j += 1
        prefix[i] = j
    result = []
    j = 0
    for i,line in enumerate(lines):
        while j and line!=needle[j]:
            j = prefix[j-1]
        if line==needle[j]:
            j += 1
        if j==len(needle):
            result.append(i-j+1)
            if len(result)==2:
                return result
            j = prefix[j-1]
    return result


_ATX = re.compile(r'^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
_SETEXT = re.compile(r'^ {0,3}(=+|-+)[ \t]*$')


def _markdown(source):
    """Locate raw spans; this is intentionally not a semantic Markdown AST."""
    # Markdown admits CR/LF/CRLF, not Unicode paragraph/line separators.
    lines = re.findall(r'[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$', source)
    require(len(lines)<=MAX_LINES, 'patch_too_complex')
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1]+len(line))
    headings, blocks = [], []
    i = 0
    while i<len(lines):
        text = lines[i].rstrip('\r\n')
        if not text.strip():
            i += 1
            continue
        fence = _FENCE.match(text)
        if fence and not (fence[1][0]=='`' and '`' in fence[2]):
            start = i
            marker = fence[1]
            i += 1
            while i<len(lines):
                end = _FENCE.match(lines[i].rstrip('\r\n'))
                i += 1
                if end and end[1][0]==marker[0] and len(end[1])>=len(marker) and not end[2].strip():
                    break
            blocks.append((offsets[start],offsets[i]))
            continue
        atx = _ATX.match(text)
        setext = _SETEXT.match(lines[i+1].rstrip('\r\n')) if i+1<len(lines) else None
        # Four-space-indented code is not a setext heading.
        if atx or (setext and not text.startswith(('    ','\t'))):
            level = len(atx[1]) if atx else (1 if setext[1][0]=='=' else 2)
            title = (atx[2] or '').strip() if atx else text.strip()
            if atx:
                title = re.sub(r'[ \t]+#+[ \t]*$','',title).rstrip()
            start = i
            i += 1 if atx else 2
            headings.append((title,level,offsets[start]))
            blocks.append((offsets[start],offsets[i]))
            continue
        start = i
        i += 1
        while i<len(lines) and lines[i].strip():
            upcoming = lines[i].rstrip('\r\n')
            underline = _SETEXT.match(lines[i+1].rstrip('\r\n')) if i+1<len(lines) else None
            if _ATX.match(upcoming) or _FENCE.match(upcoming) or underline:
                break
            i += 1
        blocks.append((offsets[start],offsets[i]))
    require(len(blocks)<=PATCH_CANDIDATE_LIMIT, 'patch_too_complex')
    return headings,blocks


def _structured(source, patch):
    headings, blocks = _markdown(source)
    if patch['kind']=='heading':
        matches = [i for i,(title,_,_) in enumerate(headings) if title==patch['heading']]
        require(bool(matches), 'patch_no_match')
        require(len(matches)==1, 'patch_ambiguous')
        index = matches[0]
        _, level, start = headings[index]
        end = next((pos for _,depth,pos in headings[index+1:] if depth<=level),len(source))
        matches = [(start,end)]
    else:
        matches = [(start,end) for start,end in blocks
                   if digest(source[start:end].encode('utf-8'))==patch['digest']]
        require(bool(matches), 'patch_no_match')
        require(len(matches)==1, 'patch_ambiguous')
    start,end = matches[0]
    require(digest(source[start:end].encode('utf-8'))==patch['digest'], 'patch_no_match')
    return source[:start]+patch['replacement']+source[end:]


def apply_patch(source, patch, *, base_source=None):
    validate_patch(patch)
    require(len(source.encode('utf-8'))<=PATCH_LIMIT, 'patch_too_large')
    if base_source is not None:
        require(len(base_source.encode('utf-8'))<=PATCH_LIMIT, 'patch_too_large')
        # Base validation is independent of current text: no forged old anchor
        # can become a successful edit merely because it matches the new text.
        apply_patch(base_source,patch)
    kind = patch['kind']
    if kind=='exact':
        result = apply_text_patch(source,patch['exact'],patch['replacement'],
                                  patch.get('before',''),patch.get('after',''))
    elif kind=='unified':
        result = _unified(source,_hunks(patch['diff']),rebase=base_source is not None)
    else:
        result = _structured(source,patch)
    require(len(result.encode('utf-8'))<=PATCH_LIMIT, 'patch_too_large')
    return result
