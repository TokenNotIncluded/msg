"""Small explicit JSON schemas; no open-ended executable configuration."""

STRING = {'type': 'string'}
IDENTIFIER = {'type': 'string', 'minLength': 1, 'maxLength': 160}
INTEGER = {'type': 'integer', 'minimum': 0}
BOOLEAN = {'type': 'boolean'}
BYTES = {'type': 'string', 'pattern': '^[A-Za-z0-9_-]*$'}
REF = {
    'type': 'object',
    'properties': {'id': IDENTIFIER, 'revision': {'type': ['string', 'null']}},
    'required': ['id'],
    'additionalProperties': False,
}
SCOPE = {
    'type': 'object',
    'properties': {'resource_id': IDENTIFIER, 'descendants': BOOLEAN},
    'required': ['resource_id'],
    'additionalProperties': False,
}
NETWORK_CONSTRAINTS = {
    'type': 'object',
    'properties': {
        'hosts': {'type': 'array', 'items': STRING},
        'schemes': {'type': 'array', 'items': {'enum': ['http', 'https']}},
        'methods': {
            'type': 'array',
            'items': {'enum': ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']},
        },
        'ports': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1, 'maximum': 65535}},
        'timeout_ms': {'type': 'integer', 'minimum': 1},
        'max_response_bytes': {'type': 'integer', 'minimum': 1},
        'max_redirects': {'type': 'integer', 'minimum': 0},
    },
    'additionalProperties': False,
}
GRANT = {
    'type': 'object',
    'properties': {
        'capability': IDENTIFIER,
        'version': {'const': 1},
        'scope': SCOPE,
        'operations': {'type': 'array', 'items': IDENTIFIER, 'minItems': 1, 'uniqueItems': True},
        'constraints': {'type': 'object'},
    },
    'required': ['capability', 'version', 'scope', 'operations', 'constraints'],
    'additionalProperties': False,
}
GRANTS = {'type': 'array', 'items': GRANT}
ISSUANCE = {
    'type': ['object', 'null'],
    'properties': {
        'issue_grants': GRANTS,
        'max_cert_ttl_seconds': INTEGER,
        'max_child_ca_depth': INTEGER,
        'max_delegation_depth': INTEGER,
    },
    'required': [
        'issue_grants',
        'max_cert_ttl_seconds',
        'max_child_ca_depth',
        'max_delegation_depth',
    ],
    'additionalProperties': False,
}
SIGNATURE = {
    'type': 'object',
    'properties': {'key_id': IDENTIFIER, 'algorithm': {'const': 'ed25519'}, 'value': BYTES},
    'required': ['key_id', 'algorithm', 'value'],
    'additionalProperties': False,
}
OUTPUT = {
    'type': 'object',
    'properties': {
        'resources': {'type': 'array', 'items': REF},
        'data': {'type': ['object', 'null']},
        'output': {'anyOf': [REF, {'type': 'null'}]},
    },
    'required': ['resources', 'data', 'output'],
    'additionalProperties': False,
}


def obj(properties=None, required=()):
    return {
        'type': 'object',
        'properties': properties or {},
        'required': list(required),
        'additionalProperties': False,
    }
