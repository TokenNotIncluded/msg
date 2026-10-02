"""The diagnostic path opens real metadata without migrations or private keys."""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from test_service import register

from msg import daemon
from msg.admin.follow_authority import (
    FOLLOW_OPERATIONS,
    follow_preview,
    open_for_repair,
    preview_configuration,
    repair_follows,
)
from msg.admin.root import RootAdmin
from msg.application import Application
from msg.core.codec import canonical, digest, loads
from msg.core.errors import Failure
from msg.storage.postgres import PostgresMetadataStore


def forbidden(*args, **kwargs):
    raise AssertionError('diagnosis must not start a service or unlock Root')


@pytest.mark.asyncio
async def test_configuration_preview_and_admin_open_are_read_only(installed, monkeypatch):
    app, _ = installed
    key, uid, _ = await register(app, 'follow-diagnostic')
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(key.key_id)
        subject = await tx.subject(uid)
        await tx.save_credential(
            replace(
                credential,
                ceiling=tuple(
                    replace(g, operations=g.operations - FOLLOW_OPERATIONS)
                    if g.capability == 'communication.basic'
                    else g
                    for g in credential.ceiling
                ),
            ),
            subject.auth_version,
        )

    async def snapshot():
        async with app.metadata.transaction(write=False) as tx:
            return digest({
                'tables': {
                    table: tx.rows(f'SELECT * FROM {table} ORDER BY 1')
                    for table in ('credentials', 'identity_keys', 'identities', 'settings', 'audit')
                },
                'relations': tx.rows(
                    "SELECT relname,relkind FROM pg_class JOIN pg_namespace n ON n.oid=relnamespace WHERE n.nspname='public' ORDER BY relname"
                ),
                'sequences': tx.rows(
                    "SELECT sequencename,last_value FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename"
                ),
            })

    observed = []

    class ExistingStore(PostgresMetadataStore):
        def __init__(self, dsn, **kwargs):
            assert kwargs == {'initialize': False}
            super().__init__(dsn, **kwargs)

        @asynccontextmanager
        async def transaction(self, *, write):
            observed.append(write)
            assert write is False
            async with super().transaction(write=write) as tx:
                yield tx

    before = await snapshot()
    monkeypatch.setattr(Application, 'load', forbidden)
    monkeypatch.setattr(Application, 'open_storage', forbidden)
    monkeypatch.setattr('msg.storage.postgres.PostgresMetadataStore', ExistingStore)
    monkeypatch.setattr('msg.admin.root.root_envelope', forbidden)
    monkeypatch.setattr('msg.security.root_files.read_private', forbidden)
    monkeypatch.setattr('getpass.getpass', forbidden)
    preview = await preview_configuration(app.settings.config_dir, '@follow-diagnostic')
    assert preview['read_only'] and preview['applied'] is False
    assert preview['preview']['subject_id'] == uid
    assert preview['preview']['key_id'] == key.key_id
    assert preview['preview']['additions'] == sorted(FOLLOW_OPERATIONS)
    assert preview['digest'] == digest(preview['preview'])
    assert 'verifier' not in canonical(preview).decode()
    opened = await open_for_repair(app.settings.config_dir)
    try:
        assert opened.contents is None and opened.executor is None
        assert not hasattr(opened, 'online_signer')
    finally:
        await opened.close()
    assert observed == [False, False]
    assert await snapshot() == before


def test_daemon_preview_dispatch_never_archives(tmp_path, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(RootAdmin, 'archive_account', forbidden)

    def preview(self, subject, **options):
        seen.append((self.config_dir, self.allow_ssh, subject, options))
        return {'read_only': True, 'applied': False}

    monkeypatch.setattr(RootAdmin, 'repair_follows', preview)
    assert (
        daemon.main([
            '--config-dir',
            str(tmp_path),
            'account',
            'repair-follows',
            '@old',
            '--key-id',
            'key_old',
        ])
        == 0
    )
    assert seen == [
        (
            tmp_path,
            False,
            '@old',
            {
                'key_id': 'key_old',
                'apply': False,
                'expected_digest': None,
            },
        )
    ]
    assert loads(capsys.readouterr().out) == {'read_only': True, 'applied': False}


def test_root_preview_never_checks_console_or_unlocks(tmp_path, monkeypatch):
    async def preview(config_dir, subject, *, key_id):
        assert config_dir == tmp_path and subject == '@old' and key_id == 'key_old'
        return {'read_only': True}

    monkeypatch.setattr('msg.admin.follow_authority.preview_configuration', preview)
    monkeypatch.setattr(RootAdmin, '_provisioning_operator', forbidden)
    monkeypatch.setattr('msg.admin.root.root_envelope', forbidden)
    monkeypatch.setattr('getpass.getpass', forbidden)
    assert RootAdmin(tmp_path).repair_follows('@old', key_id='key_old') == {'read_only': True}


@pytest.mark.parametrize('options', [{'apply': True}, {'expected_digest': 'unexpected'}])
def test_apply_requires_the_prior_preview_before_touching_authority(tmp_path, monkeypatch, options):
    monkeypatch.setattr(RootAdmin, '_provisioning_operator', forbidden)
    monkeypatch.setattr(RootAdmin, '_app', forbidden)
    monkeypatch.setattr('msg.admin.root.root_envelope', forbidden)
    with pytest.raises(Failure, match='follow_repair_expected_preview_required'):
        RootAdmin(tmp_path).repair_follows('@old', **options)


@pytest.mark.asyncio
async def test_late_recovery_markers_block_apply_without_business_or_sequence_writes(installed):
    from read_only_evidence import business_snapshot

    app, root = installed
    key, uid, _ = await register(app, 'follow-late-marker')
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(key.key_id)
        await tx.save_credential(
            replace(
                credential,
                ceiling=tuple(
                    replace(g, operations=g.operations - FOLLOW_OPERATIONS)
                    if g.capability == 'communication.basic'
                    else g
                    for g in credential.ceiling
                ),
            ),
            (await tx.subject(uid)).auth_version,
        )
    async with app.metadata.transaction(write=False) as tx:
        plan = await follow_preview(app, tx, uid)

    async def snapshot():
        async with app.metadata.transaction(write=False) as tx:
            sequence_state = tx.rows(
                "SELECT sequencename,last_value FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename"
            )
        return await business_snapshot(app), sequence_state

    marker = app.settings.recovery_marker
    marker.parent.mkdir(parents=True, exist_ok=True)
    for mode in ('file', 'dangling-symlink', 'during-signature'):
        before = await snapshot()
        if mode == 'file':
            marker.write_text('{}')
        elif mode == 'dangling-symlink':
            marker.symlink_to(marker.parent / 'absent-marker-target')

        class MarkerSigner:
            public_key = root.public_key

            def sign(self, value, *, purpose):
                marker.write_text('{}')
                return root.sign(value, purpose=purpose)

        signer = MarkerSigner() if mode == 'during-signature' else root
        try:
            with pytest.raises(Failure, match='recovery_quarantined'):
                await repair_follows(
                    app, uid, signer, expected_digest=digest(plan), operator='late-marker-test'
                )
            assert await snapshot() == before, mode
        finally:
            marker.unlink(missing_ok=True)
