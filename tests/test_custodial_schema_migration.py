"""Upgrade the actual old constraint, without losing rows or unknown policy."""

import psycopg
import pytest

from msg.core.errors import Failure
from msg.storage.postgres import PostgresMetadataStore


def legacy(dsn, *, changed=False):
    with psycopg.connect(dsn) as conn:
        conn.execute('ALTER TABLE custodial_vault DROP CONSTRAINT custodial_vault_lifecycle_check')
        conn.execute(
            "ALTER TABLE custodial_vault ADD CONSTRAINT custodial_vault_status_check CHECK(status IN ('active','destroyed'"
            + (",'custom'" if changed else '')
            + '))'
        )
        conn.execute(
            "INSERT INTO custodial_vault VALUES ('subject','sign','age','n','c','an','ac','active','2026',NULL)"
        )


async def test_old_constraint_migrates_transactionally_and_restarts(pg_dsn):
    PostgresMetadataStore(pg_dsn)
    legacy(pg_dsn)
    store = PostgresMetadataStore(pg_dsn)
    async with store.transaction(write=True) as tx:
        assert tx.one('SELECT signing_ciphertext,age_ciphertext FROM custodial_vault') == (
            'c',
            'ac',
        )
        tx.execute(
            "UPDATE custodial_vault SET status='decrypt_only',signing_nonce=NULL,signing_ciphertext=NULL",
            write=True,
        )
    restarted = PostgresMetadataStore(pg_dsn)
    async with restarted.transaction(write=False) as tx:
        assert tx.one('SELECT status,signing_ciphertext,age_ciphertext FROM custodial_vault') == (
            'decrypt_only',
            None,
            'ac',
        )
    with pytest.raises(Failure, match='constraint_conflict'):
        async with restarted.transaction(write=True) as tx:
            tx.execute(
                "UPDATE custodial_vault SET signing_ciphertext='must-not-return'", write=True
            )


async def test_unknown_old_constraint_is_not_silently_dropped(pg_dsn):
    PostgresMetadataStore(pg_dsn)
    legacy(pg_dsn, changed=True)
    with pytest.raises(Failure, match='custodial_vault_unknown_constraint'):
        PostgresMetadataStore(pg_dsn)
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute(
            "SELECT 1 FROM pg_constraint WHERE conrelid='custodial_vault'::regclass AND conname='custodial_vault_status_check'"
        ).fetchone() == (1,)
