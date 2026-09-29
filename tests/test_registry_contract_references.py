"""Registry freeze rejects dangling references before serving any contract."""
from dataclasses import replace

import pytest

from msg.core.errors import Failure
from msg.core.models import (
    CapabilitySpec, OperationSpec, PluginManifest, ResourceRef, ResourceTypeSpec,
)
from msg.core.registry import Registry


REF = ResourceRef(id='schema_contract_reference')


async def unused(*args):
    raise AssertionError('Registry validation must not invoke application code')


def declarations():
    resource = ResourceTypeSpec(name='document', version=1, container=False,
        content_schema=None, operations=frozenset(), relations=frozenset())
    capability = CapabilitySpec(name='document.read', version=1,
        scope_types=frozenset({'document'}), operations=frozenset({'document.read@1'}),
        replaces_checks=frozenset(), delegatable=False, ca_only=False,
        constraints_schema=None)
    operation = OperationSpec(name='document.read', version=1, input_schema=REF,
        output_schema=REF, effect='read', entries=frozenset({'network'}),
        require_signature=False, requirements=unused, handler=unused)
    return resource, capability, operation


def registry(resource, capability, operation):
    result = Registry()
    result.add_schema(REF, {'type': 'object'})
    result.add(PluginManifest(name='identity', version='1', dependencies=(),
        resource_types=(resource,), capabilities=(capability,),
        operations=(operation,), migrations=()))
    return result


@pytest.mark.parametrize('owner', ['resource', 'capability'])
def test_freeze_rejects_unregistered_optional_schema_and_can_retry(owner):
    resource, capability, operation = declarations()
    missing = ResourceRef(id='schema_missing')
    if owner == 'resource':
        resource = replace(resource, content_schema=missing)
    else:
        capability = replace(capability, constraints_schema=missing)
    installed = registry(resource, capability, operation)
    with pytest.raises(Failure, match='^missing_schema$'):
        installed.freeze()
    assert installed.frozen is False
    # Forward references are permitted while assembling the complete registry.
    installed.add_schema(missing, {'type': 'object'})
    installed.freeze()
    assert installed.frozen is True


@pytest.mark.parametrize('reference', ['document.unknown@1', 'document.read@2', 'document.read'])
def test_freeze_rejects_unknown_resource_operation_including_wrong_version(reference):
    resource, capability, operation = declarations()
    resource = replace(resource, operations=frozenset({reference}))
    installed = registry(resource, capability, operation)
    with pytest.raises(Failure, match='^unknown_resource_operation$'):
        installed.freeze()
    assert installed.frozen is False


def test_freeze_accepts_registered_content_constraints_and_operation_references():
    resource, capability, operation = declarations()
    resource = replace(resource, content_schema=REF,
        operations=frozenset({'document.read@1'}))
    capability = replace(capability, constraints_schema=REF)
    installed = registry(resource, capability, operation)
    installed.freeze()
    assert installed.resource_type('document') == resource
    assert installed.capability('document.read') == capability
    assert installed.operation('document.read') == operation
