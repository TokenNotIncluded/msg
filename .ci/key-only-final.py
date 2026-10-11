from pathlib import Path
import json

path = Path('src/msg/data/recovery-state-schema.json')
schema = json.loads(path.read_text())
assert 'login_bindings' in schema['tables']
del schema['tables']['login_bindings']
# Preserve strict schema checking, with only the removed table's objects gone.
for kind in ('constraints', 'indexes'):
    schema[kind] = sorted(row for row in schema[kind] if row[0] != 'login_bindings')
path.write_text(json.dumps(schema, sort_keys=True, separators=(',', ':')) + '\n')

path = Path('src/msg/security/sessions.py')
text = path.read_text()
old = 'parent.subject_id == subject.resource_id\n        and not subject.local_only'
new = "parent.subject_id == subject.resource_id\n        and not parent.id.startswith(('t_oauth_', 't_browser_'))\n        and not subject.local_only"
assert text.count(old) == 1
path.write_text(text.replace(old, new))

Path('src/msg/workers/sequenzy.py').unlink()
Path('tests/test_sequenzy_login_mail.py').unlink()
path = Path('tests/test_oauth.py')
text = path.read_text()
old = "        'msg.security.passkey',"
assert text.count(old) == 1
path.write_text(text.replace(old, old + "\n        'msg.workers.sequenzy',"))
path = Path('tests/test_oauth_custodial_ceiling.py')
path.write_text(path.read_text() + '''\n\n@pytest.mark.parametrize('prefix', ['t_oauth_', 't_browser_'])
async def test_retired_parent_cannot_back_a_restored_native_api_source(prefix):
    tx = SourceSession(custodial=False)
    tx.parent.id = prefix + 'restored'
    tx.body['parent'] = tx.parent.id
    with pytest.raises(Failure, match='invalid_grant'):
        await require_source(tx, tx.body, NOW, custodial_ceiling=())
''')
