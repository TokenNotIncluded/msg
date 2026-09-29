"""SQL-authoritative market content roots, shared by backup and collection.

Order IDs, digests and pins never confer read access. Pins protect a blob before
SQL commits; this inventory protects it after the originating file is purged.
"""

from msg.core.codec import loads


def content_references(execute):
    for table, column in (('store_packages', 'payload_refs'), ('store_deliveries', 'payload_refs')):
        for (raw,) in execute(f'SELECT {column} FROM {table} ORDER BY id'):
            for ref in loads(raw):
                yield ref['blob']
    # Legacy archives can be inspected before the new schema is installed.
    exists = next(iter(execute("SELECT to_regclass('arbitration_evidence')")))[0]
    if exists is not None:
        for (raw,) in execute('SELECT body FROM arbitration_evidence ORDER BY id'):
            yield loads(raw)['blob']
