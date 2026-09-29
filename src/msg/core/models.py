from __future__ import annotations


from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import field
from .codec import record as dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NewType, Protocol


ResourceId = NewType('ResourceId', str)
RevisionId = NewType('RevisionId', str)
RequestId = NewType('RequestId', str)
type ID = str
type Digest = str
type Json = (
    None | bool | int | float | str | tuple[Json, ...]
    | Mapping[str, Json]
)
type JsonMap = Mapping[str, Json]
type Entry = Literal['network', 'local_admin', 'worker']
type ResourceState = Literal['active', 'archived', 'purged']
type ByteRange = tuple[int, int]  # [start, end)，单位为字节


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceRef:
    id: ResourceId
    revision: RevisionId | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BlobRef:
    digest: Digest
    size: int
    media_type: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Resource:
    id: ResourceId
    type: str
    type_version: int
    name: str
    parent: ResourceId | None
    owner: ResourceId
    group: ResourceId
    mode: int
    generation: int
    revision: RevisionId | None
    state: ResourceState
    created_at: datetime
    created_by: ResourceId
    modified_at: datetime
    modified_by: ResourceId
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Relation:
    type: str
    target: ResourceRef
    excerpt: ByteRange | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Signature:
    key_id: ID
    algorithm: str
    value: bytes = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class Revision:
    format_version: int
    id: RevisionId
    resource_id: ResourceId
    parents: tuple[RevisionId, ...]
    content: BlobRef
    relations: tuple[Relation, ...]
    actor: ResourceId
    subject: ResourceId
    author: ResourceId
    created_at: datetime
    manifest_digest: Digest
    signature: Signature | None = None
    change_note: str | None = field(default=None, metadata={'omit_if_none': True})
    source_kind: Literal['release','user','operation'] | None = field(
        default=None, metadata={'omit_if_none': True})
    source_version: int | None = field(default=None, metadata={'omit_if_none': True})
    source_digest: Digest | None = field(default=None, metadata={'omit_if_none': True})
    signature_source: Literal['custodial'] | None = field(default=None,metadata={'omit_if_none':True})


@dataclass(frozen=True, slots=True, kw_only=True)
class Page[T]:
    items: tuple[T, ...]
    next_cursor: str | None = None
@dataclass(frozen=True, slots=True, kw_only=True)
class Subject:
    resource_id: ResourceId
    kind: Literal['temporary', 'registered', 'custodial', 'system']
    primary_group: ResourceId
    auth_version: int
    local_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Organization:
    resource_id: ResourceId
    membership_version: int
    builtin: Literal['public', 'admins'] | None = None
    membership_policy: Literal['open', 'approval', 'invite', 'managed'] = 'invite'


@dataclass(frozen=True, slots=True, kw_only=True)
class Membership:
    organization_id: ResourceId
    subject_id: ResourceId
    role: Literal['owner', 'maintainer', 'member', 'admin']
    version: int
    status: Literal['active', 'pending', 'invited', 'rejected'] = 'active'
    joined_at: datetime | None = None
    invited_by: ResourceId | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class EmailSettings:
    subject_id: ResourceId
    address: str | None = field(repr=False)
    verified_at: datetime | None
    enabled_events: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True, kw_only=True)
class Scope:
    resource_id: ResourceId
    descendants: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityGrant:
    capability: str
    version: int
    scope: Scope
    operations: frozenset[str]
    constraints: JsonMap


@dataclass(frozen=True, slots=True, kw_only=True)
class IssuancePolicy:
    issue_grants: tuple[CapabilityGrant, ...]
    max_cert_ttl_seconds: int
    max_child_ca_depth: int
    max_delegation_depth: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Credential:
    id: ID
    subject_id: ResourceId
    kind: Literal['signing_key', 'ssh_key', 'token']
    verifier: bytes = field(repr=False)
    ceiling: tuple[CapabilityGrant, ...]
    not_before: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Certificate:
    resource_id: ResourceId
    serial: str
    subject_id: ResourceId
    key_id: ID
    issuer_id: ResourceId
    parent_certificate_id: ResourceId | None
    authority_sources: tuple[ResourceRef, ...]
    kind: Literal['identity', 'delegation', 'capability', 'ca']
    grants: tuple[CapabilityGrant, ...]
    not_before: datetime
    expires_at: datetime
    target_service: str
    delegation_depth: int
    issuance: IssuancePolicy | None
    signature: Signature


@dataclass(frozen=True, slots=True, kw_only=True)
class CertificateRequest:
    resource_id: ResourceId  # csr_id 对应的资源
    applicant: ResourceId
    subject_id: ResourceId
    requested_issuer: ResourceId
    public_key: bytes
    kind: Literal['identity', 'delegation', 'capability', 'ca']
    grants: tuple[CapabilityGrant, ...]
    issuance: IssuancePolicy | None
    requested_ttl_seconds: int
    target_service: str
    delegation_depth: int
    authority_sources: tuple[ResourceRef, ...]
    request_digest: Digest
    possession_proof: Signature


@dataclass(frozen=True, slots=True, kw_only=True)
class CertificateRequestState:
    request_id: ResourceId
    status: Literal['pending', 'issued', 'rejected', 'cancelled', 'expired']
    generation: int
    certificate_id: ResourceId | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SignatureProof:
    signature: Signature
    certificates: tuple[ResourceId, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenProof:
    credential_id: ID
    token: bytes = field(repr=False)


type RequestProof = SignatureProof | TokenProof | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Principal:
    actor: ResourceId | None
    subject: ResourceId | None
    credential_id: ID | None
    method: Literal['anonymous', 'signature', 'token', 'ssh', 'local']
    certificates: tuple[ResourceId, ...]
    ceiling: tuple[CapabilityGrant, ...]
@dataclass(frozen=True, slots=True, kw_only=True)
class OperationRequest:
    request_id: RequestId
    protocol_version: int
    operation: str
    contract_version: int
    target_service: str
    subject: ResourceId | None
    arguments: JsonMap
    expected_generations: tuple[tuple[ResourceId, int], ...]
    expires_at: datetime | None
    payload_digest: Digest
    proof: RequestProof = field(repr=False)
    return_fields: tuple[str, ...] = ()
    source: Literal['msg', 'manual', 'mcp', 'unknown'] = 'unknown'


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationError:
    code: str
    retryable: bool
    field_path: str | None = None
    retry_after_seconds: int | None = None
    # Omit absent historical messages to preserve canonical receipt bytes.
    message: str | None = field(default=None, metadata={'omit_if_none': True})


class OperationFailure(Exception):
    def __init__(self, error: OperationError) -> None:
        self.error = error
        super().__init__(error.code)


@dataclass(frozen=True, slots=True, kw_only=True)
class HandlerOutput:
    resources: tuple[ResourceRef, ...] = ()
    data: JsonMap | None = None
    output: ResourceRef | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationResult:
    request_id: RequestId
    operation: str
    status: Literal['ok', 'accepted', 'error', 'uncertain']
    actor: ResourceId | None
    subject: ResourceId | None
    resources: tuple[ResourceRef, ...] = ()
    committed_at: datetime | None = None
    replayed: bool = False
    receipt: Signature | None = None
    error: OperationError | None = None
    data: JsonMap | None = None
    output: ResourceRef | None = None
    prefer_cli: bool = False
    cli_url: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionContext:
    request_id: RequestId
    principal: Principal
    entry: Entry
    now: datetime
    deadline_monotonic: float


@dataclass(frozen=True, slots=True, kw_only=True)
class AccessRequirement:
    resource_id: ResourceId
    operation: str
    check: Literal[
        'read', 'write', 'list', 'traverse', 'create', 'remove',
        'chmod', 'chgrp', 'chown', 'manage', 'certgate', 'tool_use', 'purge'
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceTypeSpec:
    name: str
    version: int
    container: bool
    content_schema: ResourceRef | None
    operations: frozenset[str]
    relations: frozenset[str]
    taggable: bool = False
    purchasable: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilitySpec:
    name: str
    version: int
    scope_types: frozenset[str]
    operations: frozenset[str]
    replaces_checks: frozenset[str]
    delegatable: bool
    ca_only: bool
    constraints_schema: ResourceRef | None


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationSpec:
    name: str
    version: int
    input_schema: ResourceRef
    output_schema: ResourceRef
    effect: Literal['read', 'transaction', 'external']
    entries: frozenset[Entry]
    require_signature: bool
    requirements: Callable[
        [OperationRequest, MetadataSession],
        Awaitable[tuple[AccessRequirement, ...]]
    ]
    handler: Callable[
        [ExecutionContext, OperationRequest, MetadataSession],
        Awaitable[HandlerOutput]
    ]
    requires_rules: tuple[str, ...] = ('msg.protocol',)


@dataclass(frozen=True, slots=True, kw_only=True)
class TransportLimits:
    max_request_bytes: int
    max_response_bytes: int
    max_path_bytes: int | None
    encodings: frozenset[str]


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferSession:
    id: ID
    subject_id: ResourceId
    direction: Literal['upload', 'download']
    state: Literal['open', 'sealed', 'cancelled', 'expired']
    target: ResourceRef | None
    expected_size: int | None
    expected_digest: Digest | None
    expires_at: datetime
    generation: int
    output: ResourceRef | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TransferChunk:
    transfer_id: ID
    offset: int
    content: BlobRef


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    id: ID
    type: str
    time: datetime
    request_id: RequestId
    actor: ResourceId
    subject: ResourceId
    resources: tuple[ResourceRef, ...]
    data: JsonMap


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEvent:
    event: Event
    authority: tuple[ResourceRef, ...]
    before_digest: Digest | None
    after_digest: Digest | None
    previous_digest: Digest | None
    entry_digest: Digest
    result: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectJob:
    id: ID
    event_id: ID
    kind: str
    dedupe_key: str
    principal: Principal
    operation: str
    arguments: JsonMap
    state: Literal['pending', 'running', 'done', 'failed', 'uncertain']
    attempts: int
    next_attempt_at: datetime
    lease_until: datetime | None
    result: ResourceRef | None = None
@dataclass(frozen=True, slots=True, kw_only=True)
class FieldSpec:
    name: str
    type: Literal['str', 'text', 'int', 'bool', 'enum', 'ref', 'file']
    required: bool
    default_json: bytes | None = None  # None=无默认，b'null'=默认null
    choices: tuple[str, ...] = ()
    constraints: JsonMap = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateSpec:
    resource: ResourceRef
    digest: Digest
    fields: tuple[FieldSpec, ...]
    renderer_version: int


@dataclass(frozen=True, slots=True, kw_only=True)
class NetworkPolicy:
    schemes: frozenset[str]
    hosts: tuple[str, ...]
    ports: frozenset[int]
    methods: frozenset[str]
    allow_private: bool
    timeout_ms: int
    max_response_bytes: int
    max_redirects: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolSpec:
    resource: ResourceRef
    operation: str
    input_schema: ResourceRef
    output_schema: ResourceRef
    executor_key: str
    network: NetworkPolicy


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginManifest:
    name: str
    version: str
    dependencies: tuple[str, ...]
    resource_types: tuple[ResourceTypeSpec, ...]
    capabilities: tuple[CapabilitySpec, ...]
    operations: tuple[OperationSpec, ...]
    migrations: tuple[str, ...]
    feature_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class BootstrapManifest:
    version: int
    digest: Digest
    resources: tuple[JsonMap, ...]  # 经固定 seed schema 校验
    organizations: tuple[JsonMap, ...]
    templates: tuple[BlobRef, ...]
    features: tuple[JsonMap, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class MailConfig:
    enabled: bool
    host: str
    port: int
    tls: Literal['starttls', 'tls']
    sender: str
    credential_file: Path | None = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ServerConfig:
    config_dir: Path
    postgres_dsn: str = field(repr=False)
    valkey_url: str | None = field(repr=False)
    content_dir: Path
    repositories_dir: Path
    blob_dir: Path
    staging_dir: Path
    service_keys_dir: Path
    plugins: tuple[str, ...]
    limits: TransportLimits
    mail: MailConfig | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientConfig:
    config_dir: Path
    server_url: str
    subject_id: ResourceId | None
    credential_file: Path | None = field(repr=False)
    transport: str
    limits: TransportLimits
