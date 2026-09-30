"""Read-only installation/legacy-ledger inventory, not production acceptance.

Unlike opening Application/initializing a metadata store, this command performs
no DDL, migration, filesystem repair, Root unlock, effect emission or sequence
allocation. It never supplies missing operational evidence with fixture results.
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from msg.config import load_settings
from msg.core.codec import canonical, decode, digest, loads, wire
from msg.core.errors import Failure, require
from msg.core.models import Certificate

_FIELD_EVIDENCE = (
    ('installation_identity', '#64/#65', 'deployed source/configuration and authorized inventory'),
    ('ingress_log_chain', '#64', 'CDN/proxy/APM/nginx/service redaction and listener evidence'),
    ('protected_snapshot', '#65/#84', 'independent provenance and digest of the field backup'),
    ('restore_conservation', '#65/#84', 'isolated old/new ledger and signed-history comparison'),
    (
        'current_checkpoint',
        '#69',
        'independently current signed revocation head outside the rollback set',
    ),
    ('physical_console', '#70', 'real authorized local VT/serial console and rejection evidence'),
    ('ca_transition', '#70', 'finite old CA inventory and individually approved reissue/revoke'),
    (
        'bank_fund_boundary',
        '#70/B',
        'joint role versus injection gate evidence; no actual funding here',
    ),
    (
        'capacity_and_failure_drill',
        '#84',
        'measured capacity and isolated install/crash/rollback drill',
    ),
)


def inspect_database(connection):
    require(
        connection.execute('SHOW transaction_read_only').fetchone() == ('on',),
        'preflight_read_only_required',
    )
    tables = {
        row[0]
        for row in connection.execute(
            'SELECT tablename FROM pg_tables WHERE schemaname=current_schema()'
        )
    }
    result = {
        'read_only': True,
        'state': 'empty' if not tables else 'unclassified',
        'table_count': len(tables),
        'server_version': connection.info.server_version,
    }
    if not tables:
        return result
    required = {'money_ledger', 'money_accounts', 'store_orders', 'bounty_listings', 'identities'}
    if required <= tables:
        from msg.storage.ledger_migration import (
            _escrow_sources,
            _validate_identity_references,
            _validate_ledger,
        )

        escrow = _escrow_sources(connection)
        legacy = sum(
            connection.execute('SELECT 1 FROM identities WHERE id=%s', (key,)).fetchone()
            is not None
            for key in escrow
        )
        ledger = {
            'escrow_accounts': len(escrow),
            'legacy_escrow_identities': legacy,
            'checks': 'blocked',
            'migration_performed': False,
        }
        result['state'] = 'typed_ledger' if 'ledger_accounts' in tables else 'legacy_ledger'
        try:
            _validate_identity_references(connection, escrow)
            if 'ledger_accounts' in tables:
                _validate_ledger(connection)
                ledger['checks'] = 'passed'
            else:
                ledger['reason'] = 'isolated_typed_account_migration_required'
        except RuntimeError:
            ledger['reason'] = 'legacy_reference_or_conservation_review_required'
        # Preserve exact text cells, including historical receipts: do not decode
        # or re-sign them. A server cursor bounds memory for retained ledgers.
        hasher = hashlib.sha256()
        count = 0
        with connection.cursor(name='msg_preflight_ledger') as rows:
            rows.itersize = 256
            rows.execute('SELECT * FROM money_ledger ORDER BY seq')
            for row in rows:
                raw = canonical(row)
                hasher.update(len(raw).to_bytes(8, 'big'))
                hasher.update(raw)
                count += 1
        ledger.update(rows=count, history_digest='sha256:' + hasher.hexdigest())
        result['ledger'] = ledger
    if 'settings' in tables:
        gate = connection.execute(
            "SELECT value FROM settings WHERE key='recovery_quarantine'"
        ).fetchone()
        result['quarantine_present'] = gate is not None
        row = connection.execute(
            "SELECT value FROM settings WHERE key='active_root_certificate'"
        ).fetchone()
        result['active_root_certificate'] = loads(row[0]) if row else None
    if 'certificates' in tables:
        inventory = []
        for identifier, subject, parent, revoked, body in connection.execute(
            'SELECT id,subject,parent,revoked,body FROM certificates ORDER BY id'
        ):
            certificate = decode(Certificate, loads(body))
            require(
                (certificate.resource_id, certificate.subject_id, certificate.parent_certificate_id)
                == (identifier, subject, parent),
                'preflight_certificate_mismatch',
            )
            if certificate.kind != 'ca':
                continue
            policy = certificate.issuance
            grants = (*certificate.grants, *(policy.issue_grants if policy is not None else ()))
            operations = sorted({operation for grant in grants for operation in grant.operations})
            inventory.append({
                'id': identifier,
                'subject': subject,
                'parent': parent,
                'revoked': bool(revoked),
                'key_id': certificate.key_id,
                'issuer': certificate.issuer_id,
                'target_service': certificate.target_service,
                'not_before': wire(certificate.not_before),
                'expires_at': wire(certificate.expires_at),
                'delegation_depth': certificate.delegation_depth,
                'authority_sources': wire(certificate.authority_sources),
                'certificate_digest': digest(certificate),
                # Preserve finite signed scopes and constraints for operator
                # comparison; counts alone cannot support a CA transition.
                'signed_grants': wire(certificate.grants),
                'signed_issuance_policy': wire(policy),
                'signed_grants_digest': digest(certificate.grants),
                'issuance_digest': digest(policy),
                'operation_count': len(operations),
                'explicit_operations': all('*' not in operation for operation in operations),
                'max_child_ca_depth': policy.max_child_ca_depth if policy is not None else None,
                'max_cert_ttl_seconds': policy.max_cert_ttl_seconds if policy is not None else None,
                'next_step': 'explicit_operator_review_only',
            })
        result['ca_inventory'] = inventory
    return result


def preflight(config_dir):
    """The config directory is explicit; no accidental default production probe."""
    result = {
        'format': 'msg-preflight-v1',
        'captured_at': datetime.now(UTC).isoformat(),
        'decision': 'blocked',
        'source_kind': 'unattested',
        'mutation_performed': False,
        'root_private_material': 'not_opened',
        'outbound_effects': 'not_run',
        'field_evidence': [
            {'id': key, 'issue': issue, 'required': description, 'status': 'missing'}
            for key, issue, description in _FIELD_EVIDENCE
        ],
    }
    try:
        settings = load_settings(Path(config_dir))
        config = settings.config_dir / 'msgd.toml'
        if not config.is_file():
            config = settings.config_dir / 'server.toml'
        result['config_digest'] = digest(config.read_bytes())
    except OSError, Failure, ValueError, KeyError:
        return dict(result, error='preflight_configuration_unavailable')
    try:
        with psycopg.connect(settings.server.postgres_dsn, connect_timeout=5) as connection:
            connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            result['database'] = inspect_database(connection)
    except psycopg.Error, Failure, ValueError, KeyError:
        result['error'] = 'preflight_database_unavailable_or_inconsistent'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    report = preflight(args.config)
    print(canonical(report).decode())
    raise SystemExit(2 if report['decision'] == 'blocked' else 0)


if __name__ == '__main__':
    main()
