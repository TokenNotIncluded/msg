"""Accepted configuration vocabulary and its diagnostic links.

Values and defaults remain owned by load_settings. This inventory owns allowed
field names; diagnostics report validation separately from deployment checks.
"""

from msg.core.errors import require

# section -> field -> (Settings attribute path, related doctor check)
_SECTIONS = {
    'oauth': {
        name: ('oauth.' + name, None)
        for name in ('enabled', 'access_ttl', 'session_ttl', 'clients')
    },
    'server': {
        'service_url': ('service_url', None),
        'service_aliases': ('service_aliases', None),
        'listen': ('listen', None),
        'port': ('port', None),
        'public_web_origin': ('public_web_origin', 'hosting'),
        'temporary_ttl': ('temporary_ttl', None),
        'transfer_ttl': ('transfer_ttl', None),
    },
    'websub': {'hubs': ('websub_hubs', None)},
    'storage': {
        'postgres_dsn': ('server.postgres_dsn', 'storage'),
        'valkey_url': ('server.valkey_url', 'valkey'),
        'content': ('server.content_dir', 'content_layout'),
        'repositories': ('server.repositories_dir', None),
        'blobs': ('server.blob_dir', None),
        'staging': ('server.staging_dir', 'capacity'),
        'service_keys': ('server.service_keys_dir', 'online_ca'),
    },
    'limits': {
        'request_bytes': ('server.limits.max_request_bytes', None),
        'response_bytes': ('server.limits.max_response_bytes', None),
        'path_bytes': ('server.limits.max_path_bytes', None),
        'part_bytes': ('max_part_bytes', None),
    },
    'plugins': {'enabled': ('server.plugins', None)},
    'tools': {
        'isolation': ('worker_isolation', None),
        'timeout_ms': ('tool_timeout_ms', None),
        'max_response_bytes': ('tool_max_response_bytes', None),
        'methods': ('tool_methods', None),
        'ports': ('tool_ports', None),
    },
    'identity': {
        'handle_rename_enabled': ('handle_rename_enabled', 'authority_snapshot'),
        'credential_delivery_recovery_window': (
            'credential_delivery_recovery_window',
            'credential_delivery',
        ),
    },
    'money': {
        name: ('money.' + name, 'money_config')
        for name in (
            'enabled',
            'currency_id',
            'display_name',
            'code',
            'scale',
            'transfer_fee',
            'allow_overdraft',
        )
    },
    'hosting': {
        'base_capacity_bytes': ('hosting_base_capacity_bytes', 'hosting'),
        'recovery_marker': ('hosting_recovery_marker', None),
        'content_group_read': ('hosting_content_group_read', None),
    },
    'recovery': {'custodians': ('recovery_custodians', None)},
    'mail': {
        name: ('server.mail.' + name, None)
        for name in ('enabled', 'host', 'port', 'tls', 'sender', 'credential_file')
    },
    'recovery.custodians': {
        name: ('recovery_custodians.0.' + name, None)
        for name in ('id', 'name', 'recipient', 'description', 'policy_ref')
    },
}
CONFIGURATION_FIELDS = {
    section + '.' + name: {'setting': setting, 'doctor_check': check}
    for section, fields in _SECTIONS.items()
    for name, (setting, check) in fields.items()
}


def configuration_keys(section):
    return frozenset(_SECTIONS[section])


def validate_sections(data):
    require(
        set(data) <= set(_SECTIONS) - {'mail', 'recovery.custodians'},
        'unknown_configuration_section',
    )
    require(
        all(isinstance(value, dict) for value in data.values()), 'invalid_configuration_section'
    )
