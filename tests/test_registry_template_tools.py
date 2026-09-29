"""Installed contract metadata stays separate from resource revisions and values."""

from dataclasses import replace
from hashlib import sha256

import pytest

from msg.bootstrap import manifest
from msg.core.codec import decode, wire
from msg.core.errors import Failure
from msg.core.models import FieldSpec, PluginManifest, ResourceRef, TemplateSpec
from msg.core.registry import Registry
from msg.core.template_dsl import normalize_values, parse_template
from msg.extensions.tools import read_tool


def template():
    return TemplateSpec(
        resource=ResourceRef(id='tpl_example'),
        digest='sha256:' + '0' * 64,
        fields=(FieldSpec(name='title', type='str', required=False, default_json=b'"hello"'),),
        renderer_version=1,
    )


def test_templates_are_versioned_owned_specs_with_defaults_and_strict_lookup():
    registry = Registry()
    first = template()
    registry.add_template(first)
    second = replace(first, fields=(replace(first.fields[0], default_json=b'"new"'),))
    registry.add_template(second, version=2)
    assert registry.template('tpl_example') == first
    assert registry.template('tpl_example', 2) == second
    assert registry.templates() == (first, second)
    assert normalize_values(registry.template('tpl_example'), {}) == {'title': 'hello'}
    with pytest.raises(Failure, match='^unknown_template$'):
        registry.template('unregistered')
    with pytest.raises(Failure, match='^duplicate_template$'):
        registry.add_template(second)
    with pytest.raises(Failure, match='^invalid_registry_name$'):
        registry.add_template(first, version=0)


def test_template_registration_snapshots_nested_fields_and_validates_defaults():
    registry = Registry()
    constraints = {'label': ['one']}
    spec = replace(template(), fields=(replace(template().fields[0], constraints=constraints),))
    registry.add_template(spec)
    constraints['label'].append('two')
    assert registry.template('tpl_example').fields[0].constraints['label'] == ('one',)
    with pytest.raises(Failure, match='^template_type_error'):
        registry.add_template(
            replace(spec, fields=(replace(spec.fields[0], default_json=b'42'),)), 2
        )
    with pytest.raises(Failure, match='^unknown_template_renderer$'):
        registry.add_template(replace(spec, renderer_version=2), 2)


async def test_installed_template_and_tool_contracts_match_published_resources(installed):
    app, _ = installed
    for name, source in manifest()['templates'].items():
        parsed = parse_template(source)
        registered = app.registry.template('tpl_' + name, parsed.version)
        assert registered.fields == parsed.fields
        assert registered.digest == 'sha256:' + sha256(source.encode()).hexdigest()
        assert registered.resource.revision is None
    assert {spec.resource.id for spec in app.registry.tools()} == {'tool_dns', 'tool_curl'}
    async with app.metadata.transaction(write=False) as tx:
        for registered in app.registry.tools():
            actual = await read_tool(app, tx, registered.resource.id)
            assert actual.resource.revision is not None
            assert replace(actual, resource=registered.resource) == registered
    with pytest.raises(Failure, match='^registry_frozen$'):
        app.registry.add_template(template())
    with pytest.raises(Failure, match='^registry_frozen$'):
        app.registry.add_tool(app.registry.tool('tool_dns'), 2)
    with pytest.raises(Failure, match='^unknown_tool$'):
        app.registry.tool('tool_dns', 2)


async def test_tool_registry_rejects_conflicts_missing_schemas_and_operations(installed):
    app, _ = installed
    original = app.registry.tool('tool_dns')
    # Build a fresh registration graph from the installed public contracts.
    registry = Registry()
    registry.add(
        PluginManifest(
            name='identity',
            version='1',
            dependencies=(),
            resource_types=(),
            capabilities=(),
            operations=(),
            migrations=(),
        )
    )
    registry.add_tool(original)
    with pytest.raises(Failure, match='^duplicate_tool$'):
        registry.add_tool(original)
    with pytest.raises(Failure, match='^unknown_tool_operation$'):
        registry.freeze()
    operation = app.registry.operation('tool.run')
    registry.add_operation(operation)
    for ref in (operation.input_schema, operation.output_schema):
        registry.add_schema(ref, app.registry.schema(ref))
    with pytest.raises(Failure, match='^missing_schema$'):
        registry.freeze()
    for ref in (original.input_schema, original.output_schema):
        registry.add_schema(ref, app.registry.schema(ref))
    registry.freeze()
    assert registry.tool('tool_dns') == decode(type(original), wire(original))


async def test_shipped_template_post_keeps_signed_content_contract(installed):
    from test_service import call, register

    app, _ = installed
    key, uid, _ = await register(app, 'registry-template-user')
    result = await call(
        app,
        'content.post_create',
        {'parent': '/tmp', 'template': 'message', 'values': {'body': 'hello'}},
        key=key,
        subject=uid,
    )
    assert result.status == 'ok', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        revision = await tx.revision(result.resources[0])
        from msg.core.codec import loads

        content = loads(await app.contents.read_bytes(revision.content))
        assert content == {
            'template_id': 'tpl_message',
            'template_version': 1,
            'template_digest': app.registry.template('tpl_message').digest,
            'values': {'body': 'hello'},
        }


@pytest.mark.parametrize(
    'mutation,error',
    [
        ({'executor_key': 'uninstalled'}, 'untrusted_tool_executor'),
        ({'input_schema': {'id': 'schema:tool.curl:input'}}, 'tool_schema_mismatch'),
        ({'version': 99}, 'unknown_tool'),
        ({}, None),
    ],
)
async def test_tool_resource_policy_is_preserved_but_cannot_replace_registered_code(
    installed, mutation, error
):
    from types import SimpleNamespace

    from msg.constants import TOOLS_SPACE
    from msg.core.codec import canonical
    from msg.extensions.tools import descriptor

    app, _ = installed
    value = descriptor('dns')
    value['network']['timeout_ms'] = 1234
    value.update(mutation)

    async def resource(rid):
        return SimpleNamespace(type='tool', parent=TOOLS_SPACE)

    async def revision(ref):
        assert ref.revision == 'pinned'
        return SimpleNamespace(id='pinned', content=None)

    async def read_bytes(content, limit):
        return canonical(value)

    context = SimpleNamespace(
        registry=app.registry, contents=SimpleNamespace(read_bytes=read_bytes)
    )
    tx = SimpleNamespace(resource=resource, revision=revision)
    if error:
        with pytest.raises(Failure, match='^' + error + '$'):
            await read_tool(context, tx, 'tool_dns', 'pinned')
    else:
        actual = await read_tool(context, tx, 'tool_dns', 'pinned')
        assert actual.resource.revision == 'pinned'
        assert actual.network.timeout_ms == 1234
        assert app.registry.tool('tool_dns').network.timeout_ms == 30000


async def test_user_template_versions_remain_resources_outside_frozen_registry(installed):
    from test_service import call, register

    app, _ = installed
    key, uid, _ = await register(app, 'registry-user-template')
    before = app.registry.templates()
    created = await call(
        app,
        'content.template_put',
        {'parent': '/templates', 'source': 'user-notice@7\nbody:text!\npriority:int=2\n'},
        key=key,
        subject=uid,
    )
    assert created.status == 'ok', wire(created)
    rid = created.resources[0].id
    result = await call(
        app,
        'content.post_create',
        {'parent': '/tmp', 'template': {'id': rid, 'version': 7}, 'values': {'body': 'user data'}},
        key=key,
        subject=uid,
    )
    assert result.status == 'ok', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        revision = await tx.revision(result.resources[0])
        from msg.core.codec import loads

        content = loads(await app.contents.read_bytes(revision.content))
        assert content['template_id'] == rid and content['template_version'] == 7
        assert content['values'] == {'body': 'user data', 'priority': 2}
    assert app.registry.templates() == before
    with pytest.raises(Failure, match='^unknown_template$'):
        app.registry.template(rid, 7)
