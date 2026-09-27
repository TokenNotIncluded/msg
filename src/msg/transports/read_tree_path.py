"""Readable v3 paths: x/{collection} enters a node; up/1 closes it."""
import re
from urllib.parse import quote,unquote_to_bytes
from msg.core.errors import Failure,require
from msg.core.read_query import MAX_READ_DEPTH,NESTED_FIELDS,ROOT_FIELDS
from msg.transports.dictionary import READ_QUERY_V1_SORT,READ_QUERY_V1_FIELDS


def decode_read_tree_path(raw_path):
    parts=raw_path.split(b'/')
    require(len(parts)>=6 and parts[:4] in
            ([b'',b'_r',b'q',b'3'],[b'',b'_read',b'q',b'3']),'invalid_path')
    pieces=parts[4:]
    proof=None
    if len(pieces)>=2 and pieces[-2]==b'p':
        proof=pieces[-1];pieces=pieces[:-2]
    require(pieces and len(pieces)%2==0,'invalid_path')
    root={'query_version':3}
    stack=[root]
    fields={value:key for key,value in READ_QUERY_V1_FIELDS.items()}
    sorts={value:key for key,value in READ_QUERY_V1_SORT.items()}
    for code,encoded in zip(pieces[::2],pieces[1::2],strict=True):
        require(encoded and re.search(rb'%(?![0-9A-Fa-f]{2})',encoded) is None,'invalid_path')
        try:value=unquote_to_bytes(encoded).decode('utf-8')
        except UnicodeDecodeError as exc:raise Failure('invalid_path') from exc
        require(quote(value,safe=',').encode()==encoded,'invalid_path')
        node=stack[-1]
        if code==b'x':
            require(value in {'children','replies'},'invalid_nested_query')
            require(len(stack)<=MAX_READ_DEPTH,'query_cost_exceeded')
            tree=node.setdefault('expand',{})
            require(value not in tree,'duplicate_query_parameter')
            tree[value]={};stack.append(tree[value])
            continue
        if code==b'up':
            require(value=='1' and len(stack)>1,'invalid_path')
            stack.pop();continue
        names={b'r':'parent',b't':'type',b's':'sort',b'f':'fields',b'n':'limit',b'a':'cursor',
               b'co':'collection',b'pa':'parent'}
        require(code in names,'unknown_query_parameter')
        field=names[code]
        require(len(stack)==1 or field in {'limit','fields'},'invalid_nested_query')
        require(field not in node,'duplicate_query_parameter')
        if field=='limit':
            require(value.isdecimal() and 1<=int(value)<=(100 if len(stack)==1 else 10),
                    'query_cost_exceeded')
            value=int(value)
        elif field=='fields':
            selected=value.split(',')
            allowed=ROOT_FIELDS if len(stack)==1 else NESTED_FIELDS
            require(selected and len(selected)==len(set(selected)) and
                    all(code in fields and fields[code] in allowed for code in selected),
                    'unknown_projection_field')
            value=[fields[code] for code in selected]
        elif field=='sort':
            require(value in sorts,'invalid_sort');value=sorts[value]
        elif field=='collection':
            require(value in {'children','replies'},'invalid_nested_query')
        node[field]=value
    require(len(stack)==1,'invalid_path')
    if 'cursor' in root:
        require(set(root)=={'query_version','cursor'},'cursor_query_mismatch')
        return {'cursor':root['cursor']},proof
    require(root.get('parent'),'read_query_root_required')
    return root,proof
