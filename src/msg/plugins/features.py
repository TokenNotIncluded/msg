"""Installed feature ownership; defaults remain owned by bootstrap.json.

These are diagnostic result keys, never import paths or executable resource data.
One plugin can own several independently reported features. Dependencies remain
owned by PluginManifest and are checked by Registry.add before registration.
"""
from msg.core.errors import require


FEATURE_SOURCES = {
    'money': ('money', 'market_clearing', 'market_e2e'),
    'bounty': ('bounty', 'market_clearing', 'market_e2e'),
    'orders': ('orders', 'market_clearing', 'market_e2e'),
    'bootstrap': ('system', 'bootstrap', 'isolated_namespace'),
    'market': ('store', 'market', 'market_lifecycle'),
    'postgres': ('system', 'storage', 'stable_id_read'),
    'root_trust': ('identity', 'root_trust', 'root_network_rejected'),
    'credential_delivery': ('identity', 'credential_delivery', 'credential_delivery_recovery'),
    'custodial_history': ('identity', 'custodial_history', 'custodial_history_recovery'),
    'identity_upgrade': ('identity', 'authority_snapshot', 'identity_upgrade_recovery'),
    'online_ca': ('identity', 'online_ca', 'online_registration'),
    'authorization': ('identity', 'authority_snapshot', 'certgate'),
    'audit': ('system', 'audit', 'online_delegation'),
    'content': ('content', 'content_layout', 'idempotency'),
    'git_content': ('content', 'git', 'git_push_read_cas'),
    'valkey_signal': ('system', 'valkey', None),
    'recovery': ('recovery', 'recovery_checkpoint', 'recovery_checkpoint_replay'),
    'search': ('discovery', 'lexical_search', 'lexical_search_access'),
    'hosting': ('extensions', 'hosting', 'hosting_deploy'),
}


def feature_ids(plugin_name):
    return tuple(key for key, source in FEATURE_SOURCES.items() if source[0] == plugin_name)


def validate_feature_sources(rows):
    require({row['feature_id'] for row in rows} == set(FEATURE_SOURCES),
            'feature_source_mismatch')
    for row in rows:
        require((row['doctor_check'], row['selftest_case']) ==
                FEATURE_SOURCES[row['feature_id']][1:], 'feature_source_mismatch')


def validate_feature_claims(manifest):
    claims = manifest.feature_ids
    require(isinstance(claims, tuple) and all(isinstance(key, str) for key in claims),
            'feature_source_mismatch')
    require(len(set(claims)) == len(claims) and
            all(key in FEATURE_SOURCES and FEATURE_SOURCES[key][0] == manifest.name
                for key in claims), 'feature_source_mismatch')
