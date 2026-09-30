"""Explicit read-only composition for the independent hosting HTTP process."""

import os
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace

import psycopg

from msg.constants import ROOT_SUBJECT
from msg.core.codec import decode, digest, loads, unb64
from msg.core.errors import Failure, require
from msg.core.models import Certificate, ResourceRef
from msg.core.registry import Registry
from msg.plugins import install_registry
from msg.security.authentication import AuthenticationService
from msg.security.authorization import AuthorizationService
from msg.security.capabilities import primary_ceiling, temporary_ceiling
from msg.security.certificates import CertificateValidator
from msg.security.quarantine import RuntimeGeneration, require_live_authority
from msg.storage.read_only import GitContentReader, ReadOnlyPostgresStore


async def _no_execution(*args, **kwargs):
    raise Failure('readonly_registry')


def _contract_only(spec):
    if spec.handler is _no_execution and spec.requirements is _no_execution:
        return spec
    return replace(spec, handler=_no_execution, requirements=_no_execution)


class _ContractRegistry(Registry):
    """Use the original contracts/validation but retain no business closures."""

    def add_operation(self, spec):
        super().add_operation(_contract_only(spec))

    def add(self, manifest):
        super().add(
            replace(
                manifest, operations=tuple(_contract_only(spec) for spec in manifest.operations)
            )
        )


async def require_prepared_sources(tx):
    """The installer owns publication; this role only compares its release inputs."""
    from msg.bootstrap import (
        RULE_SPECS,
        SOURCE_HEADER,
        SOURCE_PATH_OVERRIDES,
        SOURCE_RETIREMENTS,
        system_source_root,
    )

    root = system_source_root()
    expected = set()
    for rule_id, rid, _, _, default_path, _ in RULE_SPECS:
        pin = SOURCE_RETIREMENTS.get(rule_id)
        if pin is not None:
            # A retired release has no source file; compare its pinned history.
            row = tx.one(
                """SELECT source_path,rule_id,source_kind,source_version,source_digest
                FROM system_sources WHERE resource_id=?""",
                (rid,),
            )
            require(
                row
                == (
                    'docs/system/' + pin['source_path'],
                    rule_id,
                    'release_retired',
                    pin['version'],
                    pin['digest'],
                )
                and (await tx.resource(rid)).state == 'archived',
                'hosting_installation_stale',
            )
            expected.add(rid)
            continue
        path = SOURCE_PATH_OVERRIDES.get(rule_id, default_path)
        raw = root.joinpath(path).read_bytes()
        header = SOURCE_HEADER.match(raw.decode('utf-8'))
        require(header is not None and header.group(1) == rule_id, 'hosting_installation_stale')
        row = tx.one(
            """SELECT source_path,rule_id,source_kind,source_version,source_digest,revision_id
            FROM system_sources WHERE resource_id=?""",
            (rid,),
        )
        require(
            row is not None
            and row[:5]
            == ('docs/system/' + path, rule_id, 'release', int(header.group(2)), digest(raw)),
            'hosting_installation_stale',
        )
        resource = await tx.resource(rid)
        require(resource.revision == row[5], 'hosting_installation_stale')
        revision = await tx.revision(ResourceRef(id=rid, revision=row[5]))
        require(
            revision.source_kind == 'release'
            and revision.source_digest == row[4]
            and revision.source_version == row[3],
            'hosting_installation_stale',
        )
        expected.add(rid)
    require(
        {row[0] for row in tx.rows('SELECT resource_id FROM system_sources')} == expected,
        'hosting_installation_stale',
    )


class HostingRuntime:
    """Current verification + existing bytes. No Application, issuer or executor."""

    readonly_hosting = True

    def __init__(self, settings, *, clock=None):
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.registry = _ContractRegistry()
        # Installers only register trusted code. The throw-away context has no
        # storage, signer, policy service or parent Application to capture.
        install_registry(SimpleNamespace(registry=self.registry), settings.server.plugins)
        self.metadata = None
        self.contents = None
        self.runtime_generation = None
        self._loaded = False
        self._quarantined = False
        self._marker = settings.hosting_recovery_marker or settings.recovery_marker

    def require_ready(self, *, loading=False):
        require(loading or self._loaded, 'hosting_not_ready')
        self._marker = self.settings.hosting_recovery_marker or self.settings.recovery_marker
        try:
            self._marker.lstat()  # includes a dangling symlink: never a bypass
        except FileNotFoundError:
            require(
                self._marker.parent.is_dir() and os.access(self._marker.parent, os.X_OK),
                'hosting_recovery_state_unavailable',
            )
        except OSError as exc:
            raise Failure('hosting_recovery_state_unavailable') from exc
        else:
            self._quarantined = True
        require(not self._quarantined, 'recovery_quarantined')

    async def load(self):
        require(not self._loaded, 'hosting_already_loaded')
        require(self.settings.public_web_origin is not None, 'hosting_origin_not_configured')
        self.require_ready(loading=True)
        self.metadata = ReadOnlyPostgresStore(self.settings.server.postgres_dsn)
        try:
            async with self.metadata.transaction(write=False) as tx:
                require_live_authority(tx)
                if self.runtime_generation is None:
                    self.runtime_generation = RuntimeGeneration.capture(tx)
                else:
                    self.runtime_generation.require_current(tx)
        except (
            psycopg.errors.UndefinedTable,
            psycopg.errors.UndefinedColumn,
            psycopg.errors.InsufficientPrivilege,
        ) as exc:
            raise Failure('hosting_installation_stale') from exc
        require(self.settings.trust_file.is_file(), 'root_not_initialized')
        trust = loads(self.settings.trust_file.read_bytes())
        require(
            type(trust) is dict
            and set(trust) == {'version', 'public_key', 'certificate'}
            and trust['version'] == 1,
            'invalid_trust_anchor',
        )
        root = decode(Certificate, trust['certificate'])
        public = unb64(trust['public_key'], limit=32)
        require(len(public) == 32, 'invalid_trust_anchor')
        self.contents = GitContentReader(
            self.settings.server.content_dir, binary_dir=self.settings.server.blob_dir
        )
        self.certificates = CertificateValidator(
            self.registry, root, public, self.settings.service_url, self.clock
        )
        try:
            async with self.metadata.transaction(write=False) as tx:
                require_live_authority(tx)
                self.runtime_generation.require_current(tx)
                require(
                    tx.setting('active_root_certificate', root.resource_id) == root.resource_id,
                    'service_restart_required',
                )
                await self.certificates.validate(root.resource_id, tx)
                require((await tx.subject(ROOT_SUBJECT)).local_only, 'root_policy_corrupt')
                await require_prepared_sources(tx)
        except (
            psycopg.errors.UndefinedTable,
            psycopg.errors.UndefinedColumn,
            psycopg.errors.InsufficientPrivilege,
        ) as exc:
            raise Failure('hosting_installation_stale') from exc
        self.authenticator = AuthenticationService(
            self.registry,
            self.certificates,
            self.settings.service_url,
            self.clock,
            partial(primary_ceiling, self.registry),
            partial(temporary_ceiling, self.registry),
        )
        self.authorizer = AuthorizationService(self.registry, self.certificates)
        self.authenticator.runtime_generation = self.runtime_generation
        self.authenticator.oauth_config = self.settings.oauth
        self.authorizer.runtime_generation = self.runtime_generation
        self.require_ready(loading=True)
        self._loaded = True
        return self

    async def close(self):
        self._loaded = False
        if self.metadata is not None:
            await self.metadata.close()
