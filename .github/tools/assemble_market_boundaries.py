"""Exact-base source extraction for PR #168; never imports project modules."""
from pathlib import Path
import ast
import copy
import difflib
import hashlib
import io
import json
import re
import subprocess
import tokenize

ROOT = Path.cwd()
BASE = '8692ad6c0056e793bbe7b0efc9f5fd0b08ab89c8'
EXPECTED = {
    'orders':'f9abe3da981c548a60d0e1b1630d82e02d0e8491',
    'store':'09a1e14328d512e91913bf00e987cf234816348e',
    'money':'424a03f1e6de882aba425f857ab602df818f90c1',
    'delivery':'4500a65ed55e5ea06426c9ac3011f4317ecdc70b',
}
NAMES = {
    'orders':('order_records',{
        '_subject':'require_signed_subject','_viewer':'require_order_viewer',
        '_order_id':'new_order_id','_row':'read_order','_view':'legacy_order_view'}),
    'store':('catalog',{
        '_listing':'read_listing','_body':'read_listing_body','_package_row':'read_package_record'}),
    'money':('ledger',{
        'account_requirements':'account_requirements','_owner':'require_money_subject',
        '_amount':'checked_amount','_balance':'balance','_supply':'total_supply',
        'clearing_decision':'clearing_decision','_post_entry':'append_entry','_post_transfer':'post_transfer'}),
    'delivery':('managed_delivery',{
        '_delivery':'read_delivery','_buyer_order':'read_buyer_order',
        '_verified_delivery':'verify_managed_delivery','_package':'read_managed_package',
        '_payload':'read_managed_payloads','prepare_managed':'prepare_managed',
        'delivery_summary':'delivery_summary'}),
}
CONSTANTS = {'orders':('_COLUMNS',),'store':(),
    'money':('CURRENCY_ID','CODE','SCALE','MAX_MINOR','POLICY_VERSION','POLICY_DIGEST'),
    'delivery':('MAX_INLINE_BYTES','_COLUMNS')}
HEADERS = {
    'orders':'''"""Shared order identity, authorized records and legacy-compatible projections.

These functions own record access, not operation registration or settlement.
"""
from __future__ import annotations
import base64
import os
from msg.core.codec import loads
from msg.core.errors import require

''',
    'store':'''"""Authorized catalog reads shared by published checkout versions.

Version-specific schemas, creation and updates stay at catalog entrypoints.
"""
from __future__ import annotations
from msg.core.codec import loads
from msg.core.errors import Failure, require
from msg.core.models import ResourceRef
from msg.plugins.common import check_access, resolve
from msg.market.order_records import require_signed_subject

''',
    'money':'''"""One append-only ledger posting path in the caller's transaction.

This module grants no commit rights. Public transfers cannot supply the internal
escrow guard; Root issuance remains a separate local-console use case.
"""
from __future__ import annotations
from uuid import uuid4
from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, digest, wire
from msg.core.errors import require
from msg.core.models import AccessRequirement, SignatureProof

''',
    'delivery':'''"""Shared bounded managed-package verification and local preparation.

These preserve the managed checkout contracts; versioned manual/service delivery,
arbitration and operation registration retain their existing entrypoints.
"""
from __future__ import annotations
from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.models import BlobRef
from msg.plugins.common import new_id
from msg.market.ledger import CURRENCY_ID, balance
from msg.market.catalog import read_package_record
from msg.market.order_records import read_order
from msg.market.delivery_targets import validate_target

''',
}
EXTRA_RENAMES = {'store':{'_subject':'require_signed_subject'},
    'delivery':{'order_row':'read_order','_package_row':'read_package_record','_balance':'balance'}}
before = {}
after = {}
audit = []


def read(path):
    text = (ROOT/path).read_text()
    before.setdefault(path,text)
    return text


def nodes(text):
    return ast.parse(text).body


def functions(text):
    return {node.name:node for node in nodes(text)
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))}


def assignments(text):
    return {target.id:node for node in nodes(text) if isinstance(node,ast.Assign)
            for target in node.targets if isinstance(target,ast.Name)}


def segment(text,node):
    return ast.get_source_segment(text,node)


def rename(text,mapping):
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    return tokenize.untokenize(token._replace(string=mapping.get(token.string,token.string))
        if token.type==tokenize.NAME else token for token in tokens)


def splice(text,edits):
    starts=[0]
    for line in text.splitlines(True):
        starts.append(starts[-1]+len(line))
    indexed=[]
    for node,value in edits:
        indexed.append((starts[node.lineno-1]+node.col_offset,
                        starts[node.end_lineno-1]+node.end_col_offset,value))
    for start,end,value in sorted(indexed,reverse=True):
        text=text[:start]+value+text[end:]
    return text


def clean(text):
    return re.sub(r'\n{4,}','\n\n\n',text).rstrip()+'\n'


sources={name:read('src/msg/plugins/'+name+'.py') for name in NAMES}
for name,expected in EXPECTED.items():
    actual=subprocess.check_output(['git','hash-object','src/msg/plugins/'+name+'.py'],text=True).strip()
    assert actual==expected,(name,actual,expected)
assert segment(sources['orders'],functions(sources['orders'])['_subject']) == segment(
    sources['store'],functions(sources['store'])['_subject'])

# Move the unique in-process guard with the posting invariant, not with an API version.
escrow_path='src/msg/market/escrow.py'
escrow=read(escrow_path)
assert subprocess.check_output(['git','hash-object',escrow_path],text=True).strip() == '92e0bb595c8fec8665285f0d131630a52500d9fa'
guard_start=escrow.index('# Not a credential, account or public capability.')
guard_end=escrow.index('TRANSITIONS =')
guard=escrow[guard_start:guard_end]
assert '_ESCROW_WRITE = object()' in guard
assert guard.count('object()')==1
after[escrow_path]=escrow[:guard_start]+'from msg.market.ledger import _ESCROW_WRITE\n\n'+escrow[guard_end:]

providers={}
for name,(owner,mapping) in NAMES.items():
    exports={old:('msg.market.'+owner,new) for old,new in mapping.items()}
    exports.update({constant:('msg.market.'+owner,constant) for constant in CONSTANTS[name]})
    if name=='store':
        exports['_subject']=('msg.market.order_records','require_signed_subject')
    providers['msg.plugins.'+name]=exports

for name,(owner,mapping) in NAMES.items():
    text=sources[name]
    fs,cs=functions(text),assignments(text)
    renames={**mapping,**EXTRA_RENAMES.get(name,{})}
    pieces=[HEADERS[name]]
    pieces.extend(segment(text,cs[constant])+'\n' for constant in CONSTANTS[name])
    if name=='money':
        pieces.append('\n'+guard)
    for old,new in mapping.items():
        body=segment(text,fs[old])
        if name=='money' and old=='_post_transfer':
            line='        from msg.market.escrow import _ESCROW_WRITE\n'
            assert body.count(line)==1
            body=body.replace(line,'')
        pieces.append('\n\n'+rename(body,renames)+'\n')
    target='src/msg/market/'+owner+'.py'
    assert not (ROOT/target).exists(),target
    before[target]=''
    after[target]=clean(''.join(pieces))
    for old,new in mapping.items():
        original=segment(text,fs[old])
        if name=='money' and old=='_post_transfer':
            original=original.replace('        from msg.market.escrow import _ESCROW_WRITE\n','')
        expected=ast.dump(ast.parse(rename(original,renames)),include_attributes=False)
        actual=ast.dump(ast.parse(segment(after[target],functions(after[target])[new])),include_attributes=False)
        assert actual==expected,(name,old)
        audit.append(name+'.'+old+' -> '+owner+'.'+new)
    edits=[(fs[old],'') for old in mapping]+[(cs[c],'') for c in CONSTANTS[name]]
    if name=='store':
        edits.append((fs['_subject'],''))
    adapter=splice(text,edits)
    # Existing entrypoint code still names the compatibility imports. Each alias
    # is the exact owned function, not a wrapper or a second implementation.
    imports=['# Compatibility imports; shared implementation has one market owner.']
    for old,(module,new) in providers['msg.plugins.'+name].items():
        imports.append('from '+module+' import '+new+(' as '+old if new!=old else ''))
    adapter=adapter.replace('def install(app):','\n'.join(imports)+'\n\n\ndef install(app):',1)
    after['src/msg/plugins/'+name+'.py']=clean(adapter)


def rewire(text):
    edits=[]
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node,ast.ImportFrom) or node.module not in providers:
            continue
        groups={}
        for item in node.names:
            target,new=providers[node.module].get(item.name,(node.module,item.name))
            local=item.asname or item.name
            groups.setdefault(target,[]).append(new+(' as '+local if new!=local else ''))
        replacement=('\n'+' '*node.col_offset).join(
            'from '+module+' import '+', '.join(names) for module,names in groups.items())
        edits.append((node,replacement))
    return splice(text,edits)


# Only import wiring changes in existing market use cases and versioned handlers.
# Source outside these four entrypoints and market remains untouched.
paths={str(p.relative_to(ROOT)) for p in (ROOT/'src/msg/market').rglob('*.py')}
paths.update('src/msg/plugins/'+name+'.py' for name in NAMES)
paths.update(after)
for path in sorted(paths):
    text=after[path] if path in after else read(path)
    candidate=rewire(text)
    if candidate!=before.get(path,''):
        after[path]=clean(candidate)


class WithoutImports(ast.NodeTransformer):
    def visit_ImportFrom(self,node):
        return None
    def visit_Import(self,node):
        return None


def stable(node):
    return ast.dump(WithoutImports().visit(copy.deepcopy(node)),include_attributes=False)


for name in NAMES:
    old=functions(sources[name])
    new=functions(after['src/msg/plugins/'+name+'.py'])
    removed=set(NAMES[name][1])|({'_subject'} if name=='store' else set())
    assert set(new)==set(old)-removed,(name,set(new),set(old)-removed)
    for function in new:
        assert stable(old[function])==stable(new[function]),(name,function)
        audit.append(name+'.'+function+' body/schema unchanged')
for path,text in after.items():
    if not before.get(path) or not path.startswith('src/msg/market/'):
        continue
    left=ast.parse(before[path]);right=ast.parse(text)
    if path==escrow_path:
        left.body=[n for n in left.body if not (isinstance(n,ast.Assign) and
            any(isinstance(t,ast.Name) and t.id=='_ESCROW_WRITE' for t in n.targets))]
    assert stable(left)==stable(right),path
    audit.append(path+' business AST unchanged')
for path,text in after.items():
    if path.startswith('src/msg/market/'):
        assert not [n.module for n in ast.walk(ast.parse(text))
            if isinstance(n,ast.ImportFrom) and n.module in providers],path
# All old constants are moved verbatim; the changed authority import is singular.
assert after['src/msg/market/ledger.py'].count('_ESCROW_WRITE = object()')==1
assert '_ESCROW_WRITE = object()' not in after[escrow_path]

notes='docs/MARKET_BOUNDARY_PROGRESS.md'
after[notes]=read(notes)+'''

## Implementation and source audit

PR #168 extracts the existing shared implementation into `order_records`,
`catalog`, `ledger` and `managed_delivery`, with canonical public function names.
Old plugin names re-export those same function objects. All existing market use
cases, including function-local imports, and legacy plugin handlers use these
owners. The duplicate store subject check becomes the same order-subject check.

The exact-base cloud assembly checks source blob identities, compares every
moved function after identifier-only renaming, and checks every remaining handler
and market state machine after ignoring import statements. SQL strings, schemas,
IDs, receipt purposes, positional order and existing algorithm bodies are not
rewritten. The only guard change is its owner: `ledger` defines `_ESCROW_WRITE`
once, escrow re-exports it, and protected posting reads that same object directly.
The guard is still supplied only by the existing checked escrow release path.

No generated source is imported or executed during assembly. Cloud test jobs run
both new real-database contracts and existing published-version/recovery suites.
The assembly artifact contains the exact changed paths, audit and patch. Its
one-shot helper files are removed from the submitted tree before final validation.
No successful final-head result is claimed by this source-only audit.
'''

out=Path('/tmp/market-boundary-assembly');out.mkdir(parents=True,exist_ok=True)
changed={path:text for path,text in after.items() if text!=before.get(path,'')}
patch=''.join(''.join(difflib.unified_diff(before.get(path,'').splitlines(True),text.splitlines(True),
    fromfile='a/'+path if before.get(path) else '/dev/null',tofile='b/'+path)) for path,text in sorted(changed.items()))
for path,text in changed.items():
    (ROOT/path).write_text(text)
(out/'changes.patch').write_text(patch)
(out/'audit.json').write_text(json.dumps({'baseline_commit':BASE,'baseline_blobs':EXPECTED,
    'checked_invariants':audit,'changed_paths':sorted(changed),
    'patch_sha256':hashlib.sha256(patch.encode()).hexdigest()},indent=2)+'\n')
