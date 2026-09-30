"""Executable configuration acceptance vectors, never production credentials.

Doctor reports parsing and existing read-only deployment observations separately.
Selftest writes temporary TOML files and calls the real loader for each option.
It does not connect to SMTP, bind listeners, read a credential, or provision keys.
"""

import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from msg.config_contracts import CONFIGURATION_FIELDS
from msg.core.errors import Failure, require

_REQUIRED = object()
_DISABLED = object()


def _vectors():
    from msg.plugins import BUILTINS
    from msg.security.age_keys import recipient_from_public

    recipient = recipient_from_public(bytes.fromhex('09' + '00' * 31))
    custodian = {'id': 'test-custodian', 'name': 'Isolated custodian', 'recipient': recipient}
    # These are independent acceptance expectations, not configuration defaults
    # used by the server. Each case asserts the loader's effective Settings value.
    rows = {
        'oauth.enabled': (False, True, 'true', 'invalid_oauth_config'),
        'oauth.access_ttl': (900, 3600, 3601, 'invalid_oauth_config'),
        'oauth.session_ttl': (2592000, 7776000, 7776001, 'invalid_oauth_config'),
        'oauth.clients': (
            [],
            [
                {
                    'client_id': 'selftest',
                    'name': 'Config test',
                    'redirect_uris': ['https://example.invalid/callback'],
                    'scopes': ['msg.read', 'openid'],
                }
            ],
            [
                {
                    'client_id': 'selftest',
                    'name': 'Config test',
                    'redirect_uris': ['http://example.invalid/callback'],
                }
            ],
            'invalid_redirect_uri',
        ),
        'server.service_url': (
            _REQUIRED,
            'https://config.invalid',
            'file:///tmp/x',
            'invalid_service_url',
        ),
        'server.listen': ('127.0.0.1', '::1', '', 'invalid_listen_address'),
        'server.service_aliases': (
            [],
            ['https://alias.example.org'],
            ['http://alias.example.org'],
            'invalid_service_aliases',
        ),
        'server.port': (8042, 65535, 0, 'invalid_listen_port'),
        'server.public_web_origin': (
            None,
            'https://service.example.org',
            'https://elsewhere.invalid',
            'hosting_origin_must_match_service',
        ),
        'server.temporary_ttl': (3600, 1, 0, 'invalid_ttl'),
        'server.transfer_ttl': (86400, 1, 0, 'invalid_ttl'),
        'storage.postgres_dsn': (
            _REQUIRED,
            'service=isolated',
            'sqlite:///tmp/x',
            'invalid_postgres_dsn',
        ),
        'storage.valkey_url': (
            None,
            'redis://localhost:6379/0',
            'http://localhost:6379',
            'invalid_valkey_url',
        ),
        'storage.content': (
            '/var/lib/msgd/git/content',
            '/tmp/config-content',
            'relative',
            'storage_paths_must_be_absolute',
        ),
        'storage.repositories': (
            '/var/lib/msgd/git/repos',
            '/tmp/config-repos',
            'relative',
            'storage_paths_must_be_absolute',
        ),
        'storage.blobs': (
            '/var/lib/msgd/blobs/sha256',
            '/tmp/config-blobs',
            'relative',
            'storage_paths_must_be_absolute',
        ),
        'storage.staging': (
            '/var/lib/msgd/transfers/staging',
            '/tmp/config-staging',
            'relative',
            'storage_paths_must_be_absolute',
        ),
        'storage.service_keys': (
            '/var/lib/msgd/service',
            '/tmp/config-service',
            'relative',
            'storage_paths_must_be_absolute',
        ),
        'limits.request_bytes': (1048576, 256, 255, 'invalid_limit'),
        'limits.response_bytes': (1048576, 256, 255, 'invalid_limit'),
        'limits.path_bytes': (8192, 256, 255, 'invalid_limit'),
        'limits.part_bytes': (65536, 256, 255, 'invalid_limit'),
        'plugins.enabled': (list(BUILTINS), ['identity'], ['arbitrary'], 'unknown_plugin'),
        'tools.isolation': ('bubblewrap', 'bubblewrap', 'none', 'unsafe_tool_worker'),
        'tools.timeout_ms': (10000, 1, 0, 'invalid_tool_limit'),
        'tools.max_response_bytes': (4194304, 1, 0, 'invalid_tool_limit'),
        'tools.methods': (['GET', 'HEAD'], ['GET'], ['EXEC'], 'invalid_tool_methods'),
        'tools.ports': ([80, 443], [65535], [0], 'invalid_tool_ports'),
        'identity.handle_rename_enabled': (True, False, 'false', 'invalid_handle_rename_enabled'),
        'identity.credential_delivery_recovery_window': (
            900,
            '60m',
            '61m',
            'invalid_credential_delivery_recovery_window',
        ),
        'money.enabled': (True, True, False, 'invalid_money_configuration'),
        'money.currency_id': ('primary', 'primary', 'other', 'invalid_money_configuration'),
        'money.display_name': ('MSG', 'Community credits', '', 'invalid_money_display_name'),
        'money.code': ('MSG', 'CREDIT', 'msg', 'invalid_money_code'),
        'money.scale': (6, 6, 7, 'invalid_money_configuration'),
        'money.transfer_fee': (0, 0, 1, 'invalid_money_configuration'),
        'money.allow_overdraft': (False, False, True, 'invalid_money_configuration'),
        'hosting.base_capacity_bytes': (10485760, 1024**4, 0, 'invalid_hosting_capacity'),
        'hosting.recovery_marker': (
            None,
            '/tmp/config-marker',
            'relative',
            'invalid_hosting_recovery_marker',
        ),
        'hosting.content_group_read': (False, True, 'true', 'invalid_content_group_read'),
        'recovery.custodians': ([], [custodian], [custodian] * 17, 'invalid_recovery_custodians'),
        'mail.enabled': (_DISABLED, False, 'false', 'invalid_mail_enabled'),
        'mail.host': (_DISABLED, 'smtp.example.test', 'bad host', 'invalid_mail_host'),
        'mail.port': (_DISABLED, 465, 65536, 'invalid_mail_port'),
        'mail.tls': (_DISABLED, 'tls', 'plaintext', 'mail_tls_required'),
        'mail.sender': (
            _DISABLED,
            'Message service <sender@example.test>',
            'sender\r\ninjected',
            'invalid_mail_sender',
        ),
        'mail.credential_file': (
            _DISABLED,
            '/tmp/unread-smtp-credentials',
            'relative',
            'invalid_mail_credential_file',
        ),
        'recovery.custodians.id': (
            _DISABLED,
            'other-custodian',
            'INVALID',
            'invalid_recovery_custodian',
        ),
        'recovery.custodians.name': (
            _DISABLED,
            'Other custodian',
            '',
            'invalid_recovery_custodian',
        ),
        'recovery.custodians.recipient': (_DISABLED, recipient, 'invalid', 'invalid_age_recipient'),
        'recovery.custodians.description': (
            _DISABLED,
            'Description',
            'x' * 501,
            'invalid_recovery_custodian',
        ),
        'recovery.custodians.policy_ref': (
            _DISABLED,
            '/wiki/recovery-policy',
            'x' * 501,
            'invalid_recovery_custodian',
        ),
    }
    return rows, custodian


def _toml(value):
    if isinstance(value, dict):
        return '{' + ', '.join(key + ' = ' + _toml(item) for key, item in value.items()) + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_toml(item) for item in value) + ']'
    return json.dumps(value, ensure_ascii=False)


def _write(directory, data, mail):
    (directory / 'msgd.toml').write_text(
        '\n'.join(
            '['
            + section
            + ']\n'
            + '\n'.join(key + ' = ' + _toml(value) for key, value in values.items())
            for section, values in data.items()
        )
    )
    path = directory / 'mail.toml'
    if mail is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text('\n'.join(key + ' = ' + _toml(value) for key, value in mail.items()))


def _effective(settings, field):
    value = settings
    for part in CONFIGURATION_FIELDS[field]['setting'].split('.'):
        if value is None:
            return None
        if part.isdigit():
            value = value[int(part)] if value else None
        else:
            value = getattr(value, part)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        if field == 'oauth.clients':
            return [
                {
                    'client_id': item.client_id,
                    'name': item.name,
                    'redirect_uris': list(item.redirect_uris),
                    'scopes': sorted(item.scopes),
                }
                for item in value
                if item.client_id != 'msg-cli'
            ]
        if field == 'recovery.custodians':
            return [
                {key: getattr(item, key) for key in ('id', 'name', 'recipient')} for item in value
            ]
        return list(value)
    return value


def configuration_selftest():
    """Return only status/code per field; never return loaded configuration values."""
    from msg.config import load_settings

    vectors, custodian = _vectors()
    require(set(vectors) == set(CONFIGURATION_FIELDS), 'configuration_diagnostic_coverage_missing')
    results = {}
    with TemporaryDirectory(prefix='msg-config-selftest-') as temporary:
        directory = Path(temporary)
        baseline = {
            'server': {'service_url': 'https://service.example.org'},
            'storage': {'postgres_dsn': 'service=msgd'},
        }
        for field, (default, good, bad, error_code) in vectors.items():
            try:
                _write(directory, baseline, None)
                defaults = load_settings(directory)
                if default is _REQUIRED:
                    missing = deepcopy(baseline)
                    section, key = field.split('.')
                    missing[section].pop(key)
                    _write(directory, missing, None)
                    try:
                        load_settings(directory)
                    except Failure as exc:
                        require(exc.code == error_code, 'configuration_default_mismatch')
                    else:
                        raise Failure('configuration_default_mismatch')
                    default_status = 'required'
                elif default is _DISABLED:
                    require(_effective(defaults, field) is None, 'configuration_default_mismatch')
                    default_status = 'disabled'
                else:
                    require(
                        _effective(defaults, field) == default, 'configuration_default_mismatch'
                    )
                    default_status = 'pass'
                for accepted, value in ((True, good), (False, bad)):
                    data = deepcopy(baseline)
                    mail = None
                    if field.startswith('mail.'):
                        mail = {
                            'enabled': True,
                            'host': 'smtp.example.test',
                            'tls': 'starttls',
                            'sender': 'sender@example.test',
                        }
                        mail[field.split('.')[1]] = value
                    elif field.startswith('recovery.custodians.'):
                        item = dict(custodian)
                        item[field.rsplit('.', 1)[1]] = value
                        data['recovery'] = {'custodians': [item]}
                    else:
                        section, key = field.split('.')
                        data.setdefault(section, {})[key] = value
                    _write(directory, data, mail)
                    try:
                        loaded = load_settings(directory)
                    except Failure as exc:
                        require(
                            not accepted and exc.code == error_code,
                            'configuration_rejection_mismatch',
                        )
                    else:
                        require(accepted, 'configuration_rejection_missing')
                        expected = (
                            int(good[:-1]) * 60
                            if field == 'identity.credential_delivery_recovery_window'
                            else None
                            if field == 'mail.enabled'
                            else good
                        )
                        require(
                            _effective(loaded, field) == expected,
                            'configuration_effective_value_mismatch',
                        )
                results[field] = {
                    'status': 'pass',
                    'default': default_status,
                    'accepted': 'pass',
                    'rejected': 'pass',
                }
            except (Failure, ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
                results[field] = {
                    'status': 'fail',
                    'code': getattr(exc, 'code', 'configuration_check_failed'),
                }
    return results


def configuration_doctor(observations):
    """Parsing success does not prove listener, SMTP, OS permissions or live limits."""
    result = {}
    for field, spec in CONFIGURATION_FIELDS.items():
        row = {
            'status': 'pass' if observations.get('configuration', {}).get('ok') is True else 'fail',
            'scope': 'configuration_loading',
            'selftest_case': field,
        }
        check = spec['doctor_check']
        value = observations.get(check, {}) if check else {}
        row['related_check'] = {
            'check': check,
            'status': 'disabled'
            if value.get('status') == 'disabled'
            else 'pass'
            if value.get('ok') is True
            else 'fail'
            if value.get('ok') is False
            else 'not_checked',
        }
        result[field] = row
    return result
