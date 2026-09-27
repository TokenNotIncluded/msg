"""Transactional migration from inert escrow identities to typed ledger accounts.

Every stage runs on the caller's connection under the schema advisory lock.
This module never commits and never edits a historical ledger or signed receipt.
Reference paths describe protocol facts, not a heuristic scan of free text.
"""
from __future__ import annotations

import json
from psycopg import sql

_LEDGER_ACCOUNT_FKS = (
    ('money_accounts', 'money_accounts_subject_id_fkey',
     'money_accounts_ledger_account_fkey', 'subject_id'),
    ('money_ledger', 'money_ledger_debit_account_fkey',
     'money_ledger_debit_ledger_account_fkey', 'debit_account'),
    ('money_ledger', 'money_ledger_credit_account_fkey',
     'money_ledger_credit_ledger_account_fkey', 'credit_account'),
    ('store_orders', 'store_orders_escrow_subject_fkey',
     'store_orders_escrow_account_fkey', 'escrow_subject'),
    ('bounty_listings', 'bounty_listings_escrow_subject_fkey',
     'bounty_listings_escrow_account_fkey', 'escrow_subject'),
)


def _escrow_sources(conn):
    escrow = {}
    for table, kind, source in (('store_orders', 'order_escrow', 'id'),
                                ('bounty_listings', 'bounty_escrow', 'listing_id')):
        for account_id, source_id in conn.execute(sql.SQL(
                'SELECT escrow_subject,{} FROM {}').format(
                    sql.Identifier(source), sql.Identifier(table))):
            expected = (kind, None, source_id)
            if account_id in escrow and escrow[account_id] != expected:
                raise RuntimeError('ledger escrow account collision')
            escrow[account_id] = expected

    return escrow


def _install_accounts(conn, escrow):
    ids = {row[0] for row in conn.execute('''SELECT subject_id FROM money_accounts
        UNION SELECT debit_account FROM money_ledger WHERE debit_account IS NOT NULL
        UNION SELECT credit_account FROM money_ledger WHERE credit_account IS NOT NULL''')}
    ids.update(escrow)
    for account_id in sorted(ids):
        if account_id in escrow:
            expected = escrow[account_id]
        else:
            identity = conn.execute('SELECT kind,body FROM identities WHERE id=%s',
                                    (account_id,)).fetchone()
            if identity is None or identity[0] != 'subject':
                raise RuntimeError('ledger account has no subject or escrow source')
            body = json.loads(identity[1])
            if not ((account_id == 'u_root' and body.get('kind') == 'system' and
                     body.get('local_only') is True) or
                    (body.get('kind') in {'registered', 'custodial'} and
                     body.get('local_only') is not True)):
                raise RuntimeError('invalid subject ledger account')
            expected = ('subject', account_id, None)
        conn.execute('''INSERT INTO ledger_accounts(id,kind,subject_id,source_id)
            VALUES (%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING''',
            (account_id, *expected))
        actual = conn.execute('''SELECT kind,subject_id,source_id FROM ledger_accounts
            WHERE id=%s''', (account_id,)).fetchone()
        if actual != expected:
            raise RuntimeError('ledger account type/source mismatch')
    for account_id, kind, subject_id, source_id in conn.execute('''
        SELECT id,kind,subject_id,source_id FROM ledger_accounts
        WHERE kind IN ('order_escrow','bounty_escrow')'''):
        if escrow.get(account_id) != (kind, subject_id, source_id):
            raise RuntimeError('orphan or mistyped escrow account')


def _validate_ledger(conn):
    # All historical account IDs must now have typed LedgerAccount rows. The
    # amount, sequence, receipt and account IDs in money_ledger stay untouched.
    missing = conn.execute('''SELECT COUNT(*) FROM money_ledger l
        LEFT JOIN ledger_accounts d ON d.id=l.debit_account
        LEFT JOIN ledger_accounts c ON c.id=l.credit_account
        WHERE (l.debit_account IS NOT NULL AND d.id IS NULL) OR
              (l.credit_account IS NOT NULL AND c.id IS NULL)''').fetchone()[0]
    if missing:
        raise RuntimeError('unmapped historical ledger account')
    supply = conn.execute("""SELECT COALESCE(SUM(CASE kind WHEN 'mint' THEN amount_minor
        WHEN 'burn' THEN -amount_minor ELSE 0 END),0) FROM money_ledger""").fetchone()[0]
    balances = conn.execute('''SELECT COALESCE(SUM(credit),0)-COALESCE(SUM(debit),0)
        FROM (SELECT COALESCE(SUM(amount_minor),0) AS credit,0 AS debit
              FROM money_ledger WHERE credit_account IS NOT NULL
              UNION ALL
              SELECT 0,COALESCE(SUM(amount_minor),0)
              FROM money_ledger WHERE debit_account IS NOT NULL) b''').fetchone()[0]
    if supply != balances:
        raise RuntimeError('ledger conservation mismatch')
    account_balances = conn.execute('''SELECT account_id,SUM(delta) FROM (
        SELECT credit_account AS account_id,amount_minor AS delta FROM money_ledger
          WHERE credit_account IS NOT NULL
        UNION ALL
        SELECT debit_account AS account_id,-amount_minor AS delta FROM money_ledger
          WHERE debit_account IS NOT NULL) entries GROUP BY account_id''')
    if any(balance < 0 or balance > 2**63-1 for _, balance in account_balances):
        raise RuntimeError('invalid historical ledger balance')


def _validate_identity_references(conn, escrow):
    # Refuse to delete a legacy escrow identity if it ever acquired identity,
    # authority, credential or live resource references beyond the old FKs.
    protected_names = {'subject', 'subject_id', 'owner', 'owner_subject',
                       'grantor', 'grantee', 'sender', 'recipient',
                       'recipient_subject', 'actor', 'claimant', 'holder',
                       'from_subject', 'to_subject', 'publisher', 'buyer', 'seller',
                       'parent', 'source_id', 'target_id', 'participant_a',
                       'participant_b', 'initiator', 'blocker', 'blocked',
                       'invited_by', 'granted_by'}
    columns = [(table, column) for table, column in conn.execute('''
        SELECT table_name,column_name FROM information_schema.columns
        WHERE table_schema=current_schema()''')
        if column in protected_names and table not in
           {'money_accounts', 'ledger_accounts'}]
    columns.extend((('resources', 'id'),))
    legacy = set()
    for account_id in escrow:
        identity = conn.execute('SELECT kind,body FROM identities WHERE id=%s',
                                (account_id,)).fetchone()
        if identity is None:
            continue  # A repeat startup after the migration.
        legacy.add(account_id)
        body = json.loads(identity[1])
        if (identity[0] != 'subject' or body.get('kind') != 'system' or
                body.get('local_only') is not True):
            raise RuntimeError('legacy escrow identity is not inert')
        for table, column in columns:
            found = conn.execute(sql.SQL('SELECT 1 FROM {} WHERE {}=%s LIMIT 1').format(
                sql.Identifier(table), sql.Identifier(column)), (account_id,)).fetchone()
            if found:
                raise RuntimeError('legacy escrow identity has active reference')

    _validate_json_references(conn, legacy)


def _switch_account_foreign_keys(conn):
    for table, old_name, new_name, column in _LEDGER_ACCOUNT_FKS:
        conn.execute(sql.SQL('ALTER TABLE {} DROP CONSTRAINT IF EXISTS {}').format(
            sql.Identifier(table), sql.Identifier(old_name)))
        exists = conn.execute('''SELECT 1 FROM pg_constraint WHERE conrelid=%s::regclass
            AND conname=%s''', (table, new_name)).fetchone()
        if not exists:
            conn.execute(sql.SQL('ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) '
                                 'REFERENCES ledger_accounts(id)').format(
                sql.Identifier(table), sql.Identifier(new_name),
                sql.Identifier(column)))


def _retire_legacy_identities(conn, escrow):
    for account_id in escrow:
        conn.execute('DELETE FROM identities WHERE id=%s AND kind=\'subject\'',
                     (account_id,))


def _install_subject_account_trigger(conn):
    conn.execute('''CREATE OR REPLACE FUNCTION msg_register_subject_ledger_account()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE identity_body jsonb;
        BEGIN
          IF EXISTS (SELECT 1 FROM ledger_accounts WHERE id=NEW.subject_id) THEN
            RETURN NEW;
          END IF;
          SELECT body::jsonb INTO identity_body FROM identities
            WHERE id=NEW.subject_id AND kind='subject';
          IF identity_body IS NULL OR NOT (
             (NEW.subject_id='u_root' AND identity_body->>'kind'='system'
              AND identity_body->>'local_only'='true') OR
             (identity_body->>'kind' IN ('registered','custodial')
              AND COALESCE(identity_body->>'local_only','false')='false')) THEN
            RAISE EXCEPTION 'invalid_subject_ledger_account' USING ERRCODE='23514';
          END IF;
          INSERT INTO ledger_accounts(id,kind,subject_id,source_id)
            VALUES (NEW.subject_id,'subject',NEW.subject_id,NULL);
          RETURN NEW;
        END $$''')
    conn.execute('''DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='money_account_register_subject'
                       AND tgrelid='money_accounts'::regclass) THEN
          CREATE TRIGGER money_account_register_subject BEFORE INSERT ON money_accounts
            FOR EACH ROW EXECUTE FUNCTION msg_register_subject_ledger_account();
        END IF;
        END $$''')


# Paths are relative to the named JSON body. '*' traverses an array, never
# arbitrary dictionary keys. Event.data, result.data, change_note and content
# text are deliberately excluded: historical financial receipts legitimately
# contain escrow account IDs without granting those accounts an identity.
_GRANT_PATHS = ('grants.*.scope.resource_id', 'issuance.issue_grants.*.scope.resource_id',
                'authority_sources.*.id')
_JSON_IDENTITY_REFS = {
    'chunks': (),  # TransferChunk contains a transfer ID and BlobRef, not an identity.
    'resources': ('id', 'parent', 'owner', 'group', 'created_by', 'modified_by'),
    'revisions': ('resource_id', 'actor', 'subject', 'author', 'relations.*.target.id'),
    'relations': ('target.id',),
    'identities': ('resource_id', 'primary_group'),
    'memberships': ('organization_id', 'subject_id', 'invited_by'),
    'emails': ('subject_id',),
    'credentials': ('subject_id', 'ceiling.*.scope.resource_id'),
    'certificates': ('resource_id', 'subject_id', 'issuer_id', 'parent_certificate_id',
                     *_GRANT_PATHS),
    'csrs': ('resource_id', 'applicant', 'subject_id', 'requested_issuer', *_GRANT_PATHS),
    'results': ('actor', 'subject', 'resources.*.id', 'output.id'),
    'transfers': ('subject_id', 'target.id', 'output.id'),
    'events': ('actor', 'subject', 'resources.*.id'),
    'audit': ('event.actor', 'event.subject', 'event.resources.*.id', 'authority.*.id'),
    'jobs': ('principal.subject', 'principal.actor', 'principal.certificates.*',
             'principal.ceiling.*.scope.resource_id', 'result.id'),
    'messages': ('sender', 'recipient', 'subject', 'resource.id'),
    'reactions': ('subject', 'resource.id'),
    'achievement_ceremonies': ('subject_id',),
    'achievement_grants': ('subject_id', 'issuer'),
    'handoffs': ('from_subject', 'to_subject', 'resources.*.id'),
    'collaboration_leases': ('holder', 'target.id'),
    'presence': ('subject',),
    'claims': ('subject',),
    'recovery_policies': ('subject', 'owner_subject'),
    'recovery_envelopes': ('owner_subject', 'custodian_ref.id'),
    'custodial_upgrades': ('subject', 'subject_id'),
    'legacy_directive_versions': ('subject',),
    'sync_checkpoints': ('subject',),
}


def _values_at(value, parts):
    if not parts:
        yield value
    elif parts[0] == '*':
        if isinstance(value, list):
            for item in value:
                yield from _values_at(item, parts[1:])
    elif isinstance(value, dict) and parts[0] in value:
        yield from _values_at(value[parts[0]], parts[1:])


def _validate_json_references(conn, legacy):
    if not legacy:
        return
    body_tables = {row[0] for row in conn.execute("""SELECT table_name
        FROM information_schema.columns WHERE table_schema=current_schema()
        AND column_name='body'""")}
    if body_tables - _JSON_IDENTITY_REFS.keys():
        raise RuntimeError('unclassified legacy reference table')
    for table, paths in _JSON_IDENTITY_REFS.items():
        if table not in body_tables:
            continue
        query = sql.SQL('SELECT body FROM {}').format(sql.Identifier(table))
        # These two self fields are the exact inert identities being retired;
        # references to them from any OTHER identity are still unsafe.
        parameters = None
        if table == 'identities':
            query += sql.SQL(' WHERE NOT (id=ANY(%s))')
            parameters = (sorted(legacy),)
        # A named cursor bounds client memory while reading retained history.
        # It is scoped to this migration transaction, never a persistent cursor.
        with conn.cursor(name='msg_legacy_refs_' + table) as rows:
            rows.itersize = 256
            rows.execute(query, parameters)
            for (raw,) in rows:
                try:
                    value = json.loads(raw)
                except (TypeError, ValueError):
                    raise RuntimeError('invalid legacy reference document') from None
                if not isinstance(value, dict):
                    raise RuntimeError('invalid legacy reference document')
                for path in paths:
                    if any(isinstance(ref, str) and ref in legacy
                           for ref in _values_at(value, path.split('.'))):
                        # Never include a private body or arbitrary field value.
                        raise RuntimeError('legacy escrow identity has active reference')



def migrate_ledger_accounts(conn):
    """All-or-nothing stages; fault injection belongs in tests, not config."""
    escrow = _escrow_sources(conn)
    _install_accounts(conn, escrow)
    _validate_ledger(conn)
    _validate_identity_references(conn, escrow)
    _switch_account_foreign_keys(conn)
    _retire_legacy_identities(conn, escrow)
    _install_subject_account_trigger(conn)
