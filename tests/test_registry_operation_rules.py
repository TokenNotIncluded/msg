"""Operations own rule dependencies; publication does not infer them anew."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from msg.bootstrap import RULE_PATHS
from msg.core.errors import Failure
from msg.core.registry import Registry
from msg.plugins.common import registration


async def unused(*args):
    raise AssertionError('contract assembly must not execute handlers')


def assemble(name, **options):
    registry = Registry()
    op, finish = registration(SimpleNamespace(registry=registry), 'identity')
    op(name, {'type': 'object'}, **options)(unused)
    finish()
    return registry


@pytest.mark.parametrize(
    ('name', 'ids'),
    [
        ('identity.register', ('msg.identity', 'msg.auth')),
        ('content.topic_create', ('msg.topics', 'msg.read-write')),
        ('discussion.reply', ('msg.topics', 'msg.read-write')),
        ('content.post_create', ('msg.read-write',)),
        ('transfer.begin', ('msg.files', 'msg.protocol')),
        ('keystore.write', ('msg.files', 'msg.protocol')),
        ('git.push', ('msg.files', 'msg.protocol')),
        ('cert.issue', ('msg.auth',)),
        ('group.join', ('msg.auth',)),
        ('system.get', ('msg.security', 'msg.protocol')),
        ('tool.invoke', ('msg.security', 'msg.protocol')),
        ('discovery.get', ('msg.read-write', 'msg.protocol')),
        ('orders.buy', ('msg.protocol',)),
    ],
)
def test_existing_default_rule_descriptions_remain_identical(name, ids):
    registry = assemble(name)
    registry.freeze()
    assert registry.describe(registry.operation(name))['requires_rules'] == [
        {'rule_id': rid, 'path': RULE_PATHS[rid]} for rid in ids
    ]


def test_explicit_rule_dependencies_override_name_and_are_owned_by_spec():
    registry = assemble('identity.custom', requires_rules=('msg.recovery', 'msg.security'))
    registry.freeze()
    spec = registry.operation('identity.custom')
    assert spec.requires_rules == ('msg.recovery', 'msg.security')
    # Renaming an already assembled declaration must not change its rules.
    assert registry.describe(replace(spec, name='content.custom'))['requires_rules'] == [
        {'rule_id': rid, 'path': RULE_PATHS[rid]} for rid in spec.requires_rules
    ]


@pytest.mark.parametrize('ids', [('msg.unknown',), ('msg.auth', 'msg.auth'), ()])
def test_freeze_rejects_missing_duplicate_or_empty_operation_rules(ids):
    registry = assemble('identity.custom', requires_rules=ids)
    with pytest.raises(Failure, match='^dangling_requires_rules$'):
        registry.freeze()
    assert not registry.frozen


@pytest.mark.parametrize(
    'options',
    [
        {'enabled': 'false'},
        {'anonymous_only': 'true'},
        {'anonymous_only': True},
        {'anonymous_only': True, 'effect': 'read', 'signature': True},
    ],
)
def test_freeze_rejects_unsafe_access_modes(options):
    registry = assemble('identity.custom', **options)
    with pytest.raises(Failure, match='^invalid_operation_access_mode$'):
        registry.freeze()
    assert not registry.frozen
