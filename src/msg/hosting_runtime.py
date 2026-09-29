"""Read-only hosting composition. Public trust and current metadata are sufficient.

This is a process role, not a Python sandbox. Its database login and OS identity
must independently deny writes and access to the writer's configuration/keys.
"""
from dataclasses import replace
from datetime import UTC, datetime

import psycopg

from msg.composition import install_contracts
from msg.constants import ROOT_SPACE, ROOT_SUBJECT
from msg.core.codec import decode, loads, unb64
from msg.core.errors import Failure, require
from msg.core.models import Certificate, Scope
from msg.core.registry import Registry
from msg.security.authentication import AuthenticationService
from msg.security.authorization import AuthorizationService
from msg.security.capabilities import primary_ceiling, temporary_ceiling
from msg.security.certificates import CertificateValidator
from msg.security.quarantine import require_live_authority
from msg.storage.git import GitContentStore
from msg.storage.postgres import PostgresMetadataStore


async def _not_executable(*args, **kwargs):
    raise Failure('read_only_role')


class HostingRegistry(Registry):
    """Preserve signed contract metadata, discard every executable closure.

    Manifests as well as the operation index must shed callbacks; otherwise a
    manifest would retain a second path to business handlers and their context.
    """
    def add_operation(self, spec):
        super().add_operation(replace(spec, handler=_not_executable,
                                      requirements=_not_executable))

    def add(self, manifest):
        operations = tuple(replace(spec, handler=_not_executable, requirements=_not_executable)
                           for spec in manifest.operations)
        super().add(replace(manifest, operations=operations))


def _require_readonly_database(tx):
    """Check effective rights, not a resettable READ ONLY transaction default."""
    role = tx.one('SELECT oid, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls '
                  'FROM pg_roles WHERE rolname=current_user')
    require(role is not None and not any(role[1:]), 'hosting_database_role_not_readonly')
    oid = role[0]
    # NOINHERIT alone does not prevent SET ROLE. Ownership can restore revoked ACLs.
    require(tx.one('SELECT 1 FROM pg_auth_members WHERE member=? LIMIT 1', (oid,)) is None,
            'hosting_database_role_not_readonly')
    require(not any(tx.one("SELECT datdba=?, has_database_privilege(oid, 'CREATE'), "
                           "has_database_privilege(oid, 'TEMPORARY') FROM pg_database "
                           "WHERE datname=current_database()", (oid,))),
            'hosting_database_role_not_readonly')
    require(tx.one("SELECT 1 FROM pg_namespace WHERE nspowner=? OR "
                   "has_schema_privilege(oid, 'CREATE') LIMIT 1", (oid,)) is None,
            'hosting_database_role_not_readonly')
    # CASE is required: PostgreSQL may otherwise evaluate a privilege function
    # before its relkind filter. Column-only UPDATE is not a table-level grant.
    require(tx.one("SELECT 1 FROM pg_class c WHERE CASE WHEN c.relkind IN ('r','p','v','m','f') "
                   "THEN c.relowner=? OR "
                   "has_table_privilege(c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
                   "has_any_column_privilege(c.oid, 'INSERT,UPDATE,REFERENCES') "
                   "ELSE FALSE END LIMIT 1", (oid,)) is None,
            'hosting_database_role_not_readonly')
    require(tx.one("SELECT 1 FROM pg_class c WHERE CASE WHEN c.relkind='S' "
                   "THEN c.relowner=? OR has_sequence_privilege(c.oid, 'USAGE,UPDATE') "
                   "ELSE FALSE END LIMIT 1", (oid,)) is None,
            'hosting_database_role_not_readonly')
    require(tx.one("SELECT 1 FROM pg_proc WHERE proowner=? OR "
                   "(prosecdef AND has_function_privilege(oid, 'EXECUTE')) LIMIT 1", (oid,)) is None,
            'hosting_database_role_not_readonly')


class HostingRuntime:
    """Only contract discovery, signature verification, current ACLs and content reads."""
    def __init__(self, settings, *, clock=None):
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.namespace_root = ROOT_SPACE
        self.registry = HostingRegistry()
        self.metadata = None
        self.contents = None
        self._loaded = False
        install_contracts(self)

    def primary_ceiling(self):
        return primary_ceiling(self.registry, Scope(resource_id=ROOT_SPACE, descendants=True))

    def temporary_ceiling(self):
        return temporary_ceiling(self.registry, Scope(resource_id=ROOT_SPACE, descendants=True))

    async def load(self):
        require(self.settings.trust_file.is_file(), 'root_not_initialized')
        trust = loads(self.settings.trust_file.read_bytes())
        require(set(trust) == {'version', 'public_key', 'certificate'} and trust['version'] == 1,
                'invalid_trust_anchor')
        certificate = decode(Certificate, trust['certificate'])
        public_key = unb64(trust['public_key'], limit=32)
        self.metadata = PostgresMetadataStore(self.settings.server.postgres_dsn,
                                              initialize=False, read_only=True)
        self.contents = GitContentStore(self.settings.server.content_dir,
                                        binary_dir=self.settings.server.blob_dir,
                                        staging_dir=self.settings.server.staging_dir,
                                        read_only=True)
        self.certificates = CertificateValidator(self.registry, certificate, public_key,
                                                self.settings.service_url, self.clock)
        from msg.bootstrap import require_current_system_sources
        try:
            async with self.metadata.transaction(write=False) as tx:
                _require_readonly_database(tx)
                require(tx.rows('SELECT version FROM schema_version') == [(1,)],
                        'hosting_installation_not_ready')
                require_live_authority(tx)
                await self.certificates.validate(certificate.resource_id, tx)
                require((await tx.subject(ROOT_SUBJECT)).local_only, 'root_policy_corrupt')
                require(tx.setting('active_root_certificate') == certificate.resource_id,
                        'root_trust_restart_required')
                await require_current_system_sources(tx, self.contents)
        except psycopg.Error:
            # Do not expose SQL, connection credentials or internal database paths.
            raise Failure('hosting_installation_not_ready') from None
        marker = self.settings.config_dir/'recovery-drill.json'
        require(not marker.exists() and not marker.is_symlink(), 'recovery_quarantined')
        self.authenticator = AuthenticationService(self.registry, self.certificates,
            self.settings.service_url, self.clock, self.primary_ceiling, self.temporary_ceiling)
        self.authorizer = AuthorizationService(self.registry, self.certificates)
        self._loaded = True
        return self

    async def close(self):
        self._loaded = False
        if self.metadata is not None:
            await self.metadata.close()
