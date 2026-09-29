"""Complete, typed state commitments; no authority-bearing table is omitted."""
from decimal import Decimal
from importlib.resources import files
import hashlib

from msg.core.codec import canonical, loads
from msg.core.errors import require

CONTROL_SETTINGS = frozenset({'recovery_quarantine', 'recovery_replay', 'recovery_promotion'})
PAUSE_FIELDS = ('accept_writes', 'cleanup_enabled')


def catalogue(tx):
    namespaces = tx.rows("SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname<>'information_schema' ORDER BY nspname")
    require(namespaces == [('public',)], 'recovery_schema_unknown_namespace')
    relations = tx.rows("SELECT c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY c.relname")
    require(all(kind in ('r', 'i', 'S') for _, kind in relations), 'recovery_schema_unknown_relation')
    require(not tx.rows("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND (c.relrowsecurity OR c.relforcerowsecurity OR c.relpersistence<>'p')"), 'recovery_schema_unsupported')
    require(not tx.rows("SELECT 1 FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'"), 'recovery_schema_unsupported')
    require(not tx.rows("SELECT 1 FROM pg_rewrite r JOIN pg_class c ON c.oid=r.ev_class JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'"), 'recovery_schema_unsupported')
    require(not tx.rows("SELECT 1 FROM pg_collation c JOIN pg_namespace n ON n.oid=c.collnamespace WHERE n.nspname='public'"), 'recovery_schema_unsupported')
    require(not tx.rows("SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND t.tgisinternal AND t.tgenabled<>'O'"), 'recovery_schema_unsupported')
    tables = [name for name, kind in relations if kind == 'r']
    result = {'tables': {}, 'constraints': [], 'indexes': [], 'triggers': [], 'functions': [], 'sequences': []}
    for table in tables:
        result['tables'][table] = tx.rows('''SELECT a.attname,format_type(a.atttypid,a.atttypmod),
            a.attnotnull,a.attidentity,pg_get_expr(d.adbin,d.adrelid),
            (SELECT n.nspname||'.'||co.collname FROM pg_collation co JOIN pg_namespace n ON n.oid=co.collnamespace WHERE co.oid=a.attcollation)
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
            WHERE n.nspname='public' AND c.relname=? AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY a.attnum''', (table,))
    result['constraints'] = tx.rows('''SELECT c.relname,k.conname,k.contype,pg_get_constraintdef(k.oid),k.convalidated
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' ORDER BY c.relname,k.conname''')
    result['indexes'] = tx.rows("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname")
    result['triggers'] = tx.rows('''SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid)
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname''')
    result['functions'] = tx.rows('''SELECT p.proname,pg_get_function_identity_arguments(p.oid),
        pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)''')
    result['sequences'] = tx.rows("SELECT sequencename,data_type,start_value,min_value,max_value,increment_by,cycle FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename")
    result['database_collation'] = tx.one("SELECT encoding,datcollate,datctype FROM pg_database WHERE datname=current_database()")
    return loads(canonical(result))


def shape(catalog):
    """Stable allowed object/column vocabulary, independent of PG deparser whitespace."""
    return {'tables': {table: [column[:4] for column in columns] for table, columns in catalog['tables'].items()},
            'constraints': [row[:3] for row in catalog['constraints']],
            'indexes': [row[:2] for row in catalog['indexes']],
            'triggers': [row[:2] for row in catalog['triggers']],
            'functions': [row[:2] for row in catalog['functions']],
            'sequences': [row[:2] for row in catalog['sequences']]}


def known_catalogue(tx):
    actual = catalogue(tx)
    expected = loads(files('msg.data').joinpath('recovery-state-schema.json').read_bytes())
    require(shape(actual) == expected, 'recovery_schema_unsupported')
    return actual


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def cell(value):
    if value is None:
        return ['null']
    if type(value) in (str, int, bool):
        return [type(value).__name__, value]
    if type(value) is Decimal:
        require(value.is_finite(), 'recovery_state_invalid_number')
        return ['decimal', str(value)]
    require(type(value) is bytes, 'recovery_state_unsupported_type')
    return ['bytes', value.hex()]


def runtime_controls(tx):
    current = tx.setting('runtime_config')
    require(current is None or type(current) is dict, 'recovery_runtime_invalid')
    values = current or {}
    require(all(key not in values or type(values[key]) is bool for key in PAUSE_FIELDS), 'recovery_runtime_invalid')
    return {'present': current is not None,
            'fields': {key: {'present': key in values, 'value': values.get(key, False)} for key in PAUSE_FIELDS}}


def _runtime_row(row, controls):
    value = loads(row[1])
    require(type(value) is dict and all(value.get(key) is False for key in PAUSE_FIELDS), 'recovery_runtime_not_paused')
    for key in PAUSE_FIELDS:
        wanted = controls['fields'][key]
        require(type(wanted['present']) is bool and type(wanted['value']) is bool, 'recovery_runtime_invalid')
        value.pop(key)
        if wanted['present']:
            value[key] = wanted['value']
    if not controls['present']:
        require(not value, 'recovery_runtime_mismatch')
        return None
    return ('runtime_config', canonical(value).decode())


def snapshot(tx, *, controls=None, resume=None):
    if resume is not None:
        require(tx.setting('recovery_runtime_generation') == resume['generation'],
                'recovery_promotion_receipt_mismatch')
    catalog = known_catalogue(tx)
    result = {'catalogue': catalog, 'tables': {}, 'sequences': {}}
    # The application writer lock plus these relation locks serialize every DB
    # authority/effect change until capture or promotion commits.
    tx.execute('LOCK TABLE ' + ','.join('public.' + quote(name) for name in catalog['tables']) + ' IN ACCESS EXCLUSIVE MODE', write=True)
    for table, columns in catalog['tables'].items():
        rows = tx.rows('SELECT ' + ','.join(quote(column[0]) for column in columns) + ' FROM public.' + quote(table))
        if table == 'settings':
            kept = []
            for row in rows:
                if row[0] in CONTROL_SETTINGS:
                    continue
                if resume is not None and row[0] == 'recovery_runtime_generation':
                    require(loads(row[1]) == resume['generation'], 'recovery_promotion_receipt_mismatch')
                    if resume['prior_generation'] is None:
                        continue
                    row = (row[0], canonical(resume['prior_generation']).decode())
                if controls is not None and row[0] == 'runtime_config':
                    row = _runtime_row(row, controls)
                if row is not None:
                    kept.append(row)
            rows = kept
        if resume is not None and table == 'audit':
            audit = resume['audit_row']
            require(sum(loads(canonical(row)) == audit for row in rows) == 1, 'recovery_promotion_receipt_mismatch')
            require(max(row[0] for row in rows) == audit[0], 'recovery_promotion_state_changed')
            rows = [row for row in rows if row[0] != audit[0]]
        encoded = sorted(canonical([cell(value) for value in row]) for row in rows)
        hasher = hashlib.sha256()
        for row in encoded:
            hasher.update(len(row).to_bytes(8, 'big'))
            hasher.update(row)
        result['tables'][table] = {'rows': len(rows), 'sha256': hasher.hexdigest()}
    for name, *_ in catalog['sequences']:
        state = loads(canonical(tx.one('SELECT last_value,is_called FROM public.' + quote(name))))
        if resume is not None and name == 'audit_seq_seq':
            require(state in (resume['prior_audit_sequence'], [resume['audit_row'][0], True]), 'recovery_promotion_state_changed')
            state = resume['prior_audit_sequence']
        result['sequences'][name] = state
    return result
