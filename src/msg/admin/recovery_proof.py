"""Strict complete-state recovery proof; physical local promotion is a separate ceremony."""

from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import uuid4

from msg.admin.recovery_state import _runtime_row, runtime_controls, snapshot
from msg.constants import ONLINE_CA, ROOT_SUBJECT
from msg.core.codec import canonical, decode, digest, loads, parse_time, unb64, wire
from msg.core.errors import Failure, require
from msg.core.models import AuditEvent, Certificate, Event, ResourceRef, Signature
from msg.security.crypto import Ed25519Signer, verify
from msg.security.quarantine import SETTING, active

FORMAT = 'msg-complete-recovery-state-v1'
PURPOSE = 'complete-recovery-state-v1'
RECEIPT_PURPOSE = 'complete-recovery-promotion-v1'


@dataclass(frozen=True, kw_only=True)
class IndependentRecoveryPin:
    service: str
    public_key: bytes
    digest: str
    sequence: int
    source_backup_sha256: str


def verify_proof(packet, pin, now):
    require(isinstance(pin, IndependentRecoveryPin), 'recovery_independent_pin_required')
    try:
        require(
            type(packet) is dict and set(packet) == {'state', 'signature'}, 'recovery_proof_invalid'
        )
        body = packet['state']
        require(
            type(body) is dict
            and set(body)
            == {
                'format',
                'service',
                'source_backup_sha256',
                'sequence',
                'issued_at',
                'expires_at',
                'runtime_controls',
                'metadata',
                'files',
            },
            'recovery_proof_invalid',
        )
        require(
            body['format'] == FORMAT
            and body['service'] == pin.service
            and body['source_backup_sha256'] == pin.source_backup_sha256
            and type(pin.public_key) is bytes
            and len(pin.public_key) == 32
            and type(pin.sequence) is int
            and pin.sequence >= 1
            and type(body['sequence']) is int
            and body['sequence'] == pin.sequence
            and digest(body) == pin.digest,
            'recovery_proof_pin_mismatch',
        )
        controls = body['runtime_controls']
        require(
            type(controls) is dict
            and set(controls) == {'present', 'fields'}
            and type(controls['present']) is bool
            and type(controls['fields']) is dict
            and set(controls['fields']) == {'accept_writes', 'cleanup_enabled'},
            'recovery_proof_invalid',
        )
        for field in controls['fields'].values():
            require(
                type(field) is dict
                and set(field) == {'present', 'value'}
                and type(field['present']) is bool
                and type(field['value']) is bool,
                'recovery_proof_invalid',
            )
        require(
            type(body['metadata']) is dict and type(body['files']) is dict, 'recovery_proof_invalid'
        )
        start, end = parse_time(body['issued_at']), parse_time(body['expires_at'])
        require(start <= now < end and end - start <= timedelta(hours=24), 'recovery_proof_expired')
        verify(
            pin.public_key, canonical(body), decode(Signature, packet['signature']), purpose=PURPOSE
        )
        return loads(canonical(body))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise Failure('recovery_proof_invalid') from exc


async def verify_current_authority(app, tx, public_key):
    from msg.security.backup_retirement import root_verifier

    require(
        await root_verifier(tx, now=app.clock()) == public_key == app.certificates.root_public_key,
        'recovery_root_mismatch',
    )
    generation = tx.rows("SELECT value FROM settings WHERE key='recovery_runtime_generation'")
    require(
        not generation or (type(loads(generation[0][0])) is str and bool(loads(generation[0][0]))),
        'recovery_runtime_invalid',
    )
    root = await tx.subject(ROOT_SUBJECT)
    require(root.local_only, 'root_policy_corrupt')
    for (identifier,) in tx.rows('SELECT id FROM certificates WHERE revoked=0 ORDER BY id'):
        certificate = await tx.certificate(identifier)
        # Expired signed history remains history. Every currently usable chain
        # is revalidated from current credentials, authority sources and scope.
        if certificate.not_before <= app.clock() < certificate.expires_at:
            await app.certificates.validate(identifier, tx)
    online = tx.setting('online_ca_certificate')
    require(type(online) is str, 'recovery_online_issuer_missing')
    certificate = await app.certificates.validate(online, tx)
    require(
        certificate.subject_id == ONLINE_CA and certificate.key_id == app.online_signer.key_id,
        'recovery_online_key_mismatch',
    )
    # The receipt signer has a registered credential, independent of online CA.
    receipt = tx.setting('receipt_public_key')
    require(
        type(receipt) is dict
        and receipt.get('key_id') == app.receipt_signer.key_id
        and unb64(receipt.get('public_key', ''), limit=32) == app.receipt_signer.public_key,
        'recovery_receipt_key_mismatch',
    )
    for (identifier,) in tx.rows('SELECT id FROM resources ORDER BY id'):
        await tx.ancestors(identifier)
    from msg.admin.market_check import inspect_market
    from msg.storage.ledger_migration import _validate_ledger

    _validate_ledger(tx._connection)
    await inspect_market(tx)


def configuration_state(settings):
    """Bind policy, while naming every deployment-only field explicitly."""
    value = wire(settings)
    for key in ('listen', 'port'):
        value.pop(key)
    value['hosting_recovery_marker'] = settings.hosting_recovery_marker is not None
    for key in (
        'config_dir',
        'postgres_dsn',
        'valkey_url',
        'mail',
        'content_dir',
        'repositories_dir',
        'blob_dir',
        'staging_dir',
        'service_keys_dir',
    ):
        value['server'].pop(key)
    return value


def file_state(app, tx):
    from msg.admin.backups import _db_refs, _hash, _regular_tree, _verify_storage

    settings = app.settings
    from msg.config import load_settings

    actual_settings = load_settings(settings.config_dir)
    if active(tx):
        require(
            actual_settings.server.mail is None and actual_settings.server.valkey_url is None,
            'recovery_target_not_isolated',
        )
    policy = configuration_state(actual_settings)
    require(actual_settings == settings, 'recovery_cached_configuration_mismatch')
    require(
        settings.trust_file.is_file() and not settings.trust_file.is_symlink(),
        'invalid_trust_anchor',
    )
    trees = {}
    for name, path in (
        ('content', settings.server.content_dir),
        ('blobs', settings.server.blob_dir),
        ('repositories', settings.server.repositories_dir),
        ('staging', settings.server.staging_dir),
        ('service', settings.service_keys),
    ):
        if name in {'repositories', 'staging'} and not path.exists() and not path.is_symlink():
            trees[name] = {}
            continue
        _regular_tree(path)
        trees[name] = {
            str(file.relative_to(path)): {'size': file.stat().st_size, 'sha256': _hash(file)}
            for file in sorted(path.rglob('*'))
            if file.is_file()
        }
    refs = _db_refs(tx._connection)
    for (identifier,) in tx.rows("SELECT id FROM resources WHERE type='repo' AND state<>'purged'"):
        require(
            (settings.server.repositories_dir / (identifier + '.git')).is_dir(),
            'backup_repository_missing',
        )
    _verify_storage(
        settings.server.content_dir.parent, refs, settings=settings, repair_git_layout=False
    )
    require(
        Ed25519Signer.from_bytes((settings.service_keys / 'online.key').read_bytes()).public_key
        == app.online_signer.public_key
        and Ed25519Signer.from_bytes(
            (settings.service_keys / 'receipt.key').read_bytes()
        ).public_key
        == app.receipt_signer.public_key
        and (settings.service_keys / 'tokens.key').read_bytes() == app._token_secret,
        'recovery_cached_keys_mismatch',
    )
    keys = {
        name: digest((settings.service_keys / name).read_bytes())
        for name in ('online.key', 'receipt.key', 'tokens.key')
    }
    require(len((settings.service_keys / 'tokens.key').read_bytes()) == 32, 'invalid_service_key')
    # Digests commit random service key material without exporting it.
    return {
        'configuration': policy,
        'references': refs,
        'trees': trees,
        'service_key_digests': keys,
        'trust': loads(settings.trust_file.read_bytes()),
    }


async def draft(app, *, public_key, source_backup_sha256, sequence):
    """Internal use case; public RootAdmin entry first enforces console and PIN."""
    require(
        type(sequence) is int
        and sequence > 0
        and type(source_backup_sha256) is str
        and len(source_backup_sha256) == 64
        and all(c in '0123456789abcdef' for c in source_backup_sha256),
        'recovery_proof_invalid',
    )
    async with app.metadata.transaction(write=True) as tx:
        require(
            not active(tx)
            and not (app.settings.recovery_marker).exists()
            and not (app.settings.recovery_marker).is_symlink(),
            'recovery_source_is_quarantined',
        )
        state = snapshot(tx)
        await verify_current_authority(app, tx, public_key)
        body = {
            'format': FORMAT,
            'service': app.settings.service_url,
            'source_backup_sha256': source_backup_sha256,
            'sequence': sequence,
            'issued_at': wire(app.clock()),
            'expires_at': wire(app.clock() + timedelta(hours=24)),
            'runtime_controls': runtime_controls(tx),
            'metadata': state,
            'files': file_state(app, tx),
        }
        return body


async def seal(app, body, signer):
    async with app.metadata.transaction(write=True) as tx:
        require(
            not active(tx)
            and not (app.settings.recovery_marker).exists()
            and not (app.settings.recovery_marker).is_symlink(),
            'recovery_source_is_quarantined',
        )
        require(
            body['service'] == app.settings.service_url
            and snapshot(tx) == body['metadata']
            and runtime_controls(tx) == body['runtime_controls']
            and file_state(app, tx) == body['files'],
            'recovery_complete_state_mismatch',
        )
        await verify_current_authority(app, tx, signer.public_key)
        require(
            parse_time(body['issued_at']) <= app.clock() < parse_time(body['expires_at']),
            'recovery_proof_expired',
        )
        return {'state': body, 'signature': wire(signer.sign(canonical(body), purpose=PURPOSE))}


async def capture(app, signer, *, source_backup_sha256, sequence):
    body = await draft(
        app,
        public_key=signer.public_key,
        source_backup_sha256=source_backup_sha256,
        sequence=sequence,
    )
    return await seal(app, body, signer)


def _receipt(value, pin):
    require(
        type(value) is dict and set(value) == {'receipt', 'signature'},
        'recovery_promotion_receipt_invalid',
    )
    body = value['receipt']
    require(
        type(body) is dict
        and set(body)
        == {
            'manifest_digest',
            'sequence',
            'source_backup_sha256',
            'generation',
            'prior_generation',
            'audit_row',
            'prior_audit_sequence',
        },
        'recovery_promotion_receipt_invalid',
    )
    require(
        type(body['generation']) is str
        and bool(body['generation'])
        and (
            body['prior_generation'] is None
            or (type(body['prior_generation']) is str and bool(body['prior_generation']))
        )
        and type(body['audit_row']) is list
        and len(body['audit_row']) == 4
        and type(body['audit_row'][0]) is int
        and body['audit_row'][0] > 0
        and type(body['prior_audit_sequence']) is list
        and len(body['prior_audit_sequence']) == 2
        and type(body['prior_audit_sequence'][0]) is int
        and type(body['prior_audit_sequence'][1]) is bool,
        'recovery_promotion_receipt_invalid',
    )
    verify(
        pin.public_key,
        canonical(body),
        decode(Signature, value['signature']),
        purpose=RECEIPT_PURPOSE,
    )
    require(
        body['manifest_digest'] == pin.digest
        and body['sequence'] == pin.sequence
        and body['source_backup_sha256'] == pin.source_backup_sha256,
        'recovery_promotion_receipt_mismatch',
    )
    return body


async def promote(app, packet, *, pin, signer, operator):
    """Internal fixture-capable use case. Caller must enforce physical console ceremony."""
    body = verify_proof(packet, pin, app.clock())
    require(
        app.settings.server.mail is None and app.settings.server.valkey_url is None,
        'recovery_target_not_isolated',
    )
    require(signer.public_key == pin.public_key, 'recovery_root_mismatch')
    require(type(operator) is str and bool(operator), 'recovery_operator_required')
    marker = app.settings.recovery_marker
    from msg.security.root_files import read_private

    marker_present = marker.exists() or marker.is_symlink()
    if marker_present:
        marker_value = loads(read_private(marker))
        require(
            type(marker_value) is dict
            and marker_value.get('source_backup_sha256') == pin.source_backup_sha256,
            'recovery_checkpoint_backup_mismatch',
        )
    async with app.metadata.transaction(write=True) as tx:
        previous = tx.setting('recovery_promotion')
        # A backup of an already recovered source contains a historical receipt.
        # Only the exact current manifest can resume its pending delta; a new
        # recovery must independently match the entire initial source state.
        pending = (
            type(previous) is dict
            and type(previous.get('receipt')) is dict
            and previous['receipt'].get('manifest_digest') == pin.digest
        )
        resume = _receipt(previous, pin) if pending else None
        gate = tx.setting(SETTING)
        require(
            active(tx)
            and type(gate) is dict
            and gate.get('source_backup_sha256') == pin.source_backup_sha256,
            'recovery_quarantine_required',
        )
        if resume is None:
            require(marker_present, 'recovery_marker_required')
            require(
                active(tx)
                and type(gate) is dict
                and gate.get('source_backup_sha256') == pin.source_backup_sha256
                and gate.get('format') == 'msg-recovery-quarantine-v1'
                and gate.get('authority') == 'health_only'
                and gate.get('outbound_enabled') is False,
                'recovery_quarantine_required',
            )
        actual = snapshot(
            tx, controls=body['runtime_controls'] if resume is None else None, resume=resume
        )
        require(actual == body['metadata'], 'recovery_complete_state_mismatch')
        require(file_state(app, tx) == body['files'], 'recovery_complete_files_mismatch')
        await verify_current_authority(app, tx, pin.public_key)
        verify_proof(packet, pin, app.clock())
        if resume is None:
            prior_generation = tx.setting('recovery_runtime_generation')
            require(
                prior_generation is None or type(prior_generation) is str,
                'recovery_runtime_invalid',
            )
            generation = uuid4().hex
            event = Event(
                id='audit_' + uuid4().hex,
                type='recovery.complete_promotion',
                time=app.clock(),
                request_id=pin.digest,
                actor=ROOT_SUBJECT,
                subject=ROOT_SUBJECT,
                resources=(),
                data={
                    'manifest_digest': pin.digest,
                    'sequence': pin.sequence,
                    'operator': operator,
                    'runtime_generation': generation,
                    'requires_runtime_restart': True,
                },
            )
            audit_event = AuditEvent(
                event=event,
                authority=(ResourceRef(id=tx.setting('active_root_certificate')),),
                before_digest=pin.digest,
                after_digest=pin.digest,
                previous_digest=None,
                entry_digest='',
                result='verified_complete_state',
            )
            previous = tx.one('SELECT seq,digest FROM audit ORDER BY seq DESC LIMIT 1')
            prior_sequence = actual['sequences']['audit_seq_seq']
            audit_seq = max(
                prior_sequence[0] + int(prior_sequence[1]), (previous[0] + 1) if previous else 1
            )
            audit_data = wire(
                replace(
                    audit_event, previous_digest=previous[1] if previous else None, entry_digest=''
                )
            )
            audit_data.pop('entry_digest')
            audit_event = replace(
                audit_event,
                previous_digest=previous[1] if previous else None,
                entry_digest=digest(audit_data),
            )
            # Explicit allocation is transactional. PostgreSQL nextval is not:
            # using it here would invalidate the source commitment on rollback.
            tx.execute(
                'INSERT INTO audit (seq,digest,previous,body) VALUES (?,?,?,?)',
                (
                    audit_seq,
                    audit_event.entry_digest,
                    audit_event.previous_digest,
                    canonical(audit_event).decode(),
                ),
                write=True,
            )
            audit_row = tx.one(
                'SELECT seq,digest,previous,body FROM audit ORDER BY seq DESC LIMIT 1'
            )
            receipt = {
                'manifest_digest': pin.digest,
                'sequence': pin.sequence,
                'source_backup_sha256': pin.source_backup_sha256,
                'generation': generation,
                'prior_generation': prior_generation,
                'audit_row': loads(canonical(audit_row)),
                'prior_audit_sequence': actual['sequences']['audit_seq_seq'],
            }
            tx.set_setting(
                'recovery_promotion',
                {
                    'receipt': receipt,
                    'signature': wire(signer.sign(canonical(receipt), purpose=RECEIPT_PURPOSE)),
                },
            )
            tx.set_setting('recovery_runtime_generation', generation)
            runtime = tx.setting('runtime_config')
            normalized = _runtime_row(
                ('runtime_config', canonical(runtime).decode()), body['runtime_controls']
            )
            if normalized is None:
                tx.execute("DELETE FROM settings WHERE key='runtime_config'", write=True)
            else:
                tx.set_setting('runtime_config', loads(normalized[1]))
            resume = receipt
    # Keep the database quarantine until marker unlink AND directory durability
    # succeed. A crash between unlink and commit remains closed through the DB;
    # only an authenticated exact receipt can resume with the marker missing.
    import os

    async with app.metadata.transaction(write=True) as tx:
        require(active(tx), 'recovery_quarantine_required')
        require(
            _receipt(tx.setting('recovery_promotion'), pin) == resume,
            'recovery_promotion_receipt_mismatch',
        )
        require(snapshot(tx, resume=resume) == body['metadata'], 'recovery_complete_state_mismatch')
        require(file_state(app, tx) == body['files'], 'recovery_complete_files_mismatch')
        verify_proof(packet, pin, app.clock())
        tx.execute(
            "SELECT setval('public.audit_seq_seq', ?, true)", (resume['audit_row'][0],), write=True
        )
        try:
            fd = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                if marker_present:
                    marker.unlink()
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise Failure('recovery_promotion_finish_required') from exc
        tx.execute('DELETE FROM settings WHERE key=?', (SETTING,), write=True)
    return {
        'status': 'recovery_promoted',
        'manifest_digest': pin.digest,
        'runtime_generation': resume['generation'],
        'requires_runtime_restart': True,
    }


async def open_for_proof(config_dir):
    """Load only existing authority; no DDL, release-source sync, repair or worker."""
    from msg.application import Application
    from msg.config import load_settings
    from msg.security.certificates import CertificateValidator
    from msg.storage.postgres import PostgresMetadataStore

    settings = load_settings(config_dir)
    app = Application(settings)
    app.metadata = PostgresMetadataStore(settings.server.postgres_dsn, initialize=False)
    require(
        settings.trust_file.is_file() and not settings.trust_file.is_symlink(),
        'invalid_trust_anchor',
    )
    trust = loads(settings.trust_file.read_bytes())
    require(
        set(trust) == {'version', 'public_key', 'certificate'} and trust['version'] == 1,
        'invalid_trust_anchor',
    )
    app.certificates = CertificateValidator(
        app.registry,
        decode(Certificate, trust['certificate']),
        unb64(trust['public_key'], limit=32),
        settings.service_url,
        app.clock,
    )
    from msg.admin.backups import _regular_tree

    _regular_tree(settings.service_keys)
    app.online_signer = Ed25519Signer.from_bytes(
        (settings.service_keys / 'online.key').read_bytes()
    )
    app.receipt_signer = Ed25519Signer.from_bytes(
        (settings.service_keys / 'receipt.key').read_bytes()
    )
    app._token_secret = (settings.service_keys / 'tokens.key').read_bytes()
    return app
