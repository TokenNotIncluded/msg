"""Bounded local formatting cannot acquire additional data or execute templates."""

import pytest

from msg.core.errors import Failure


def test_template_uses_json_pointers_and_escapes_values():
    from msg.client_output import render_template

    data = {'data': {'items': [{'name': 'line\n\x1b[31m'}], 'a/b': {'~key': 7}}}
    assert render_template('name={{/data/items/0/name}} n={{/data/a~1b/~0key}}', data) == (
        'name="line\\n\\u001b[31m" n=7'
    )


@pytest.mark.parametrize(
    'template',
    [
        '{{data.name}}',
        '{{/missing}}',
        '{{/data/__class__}}',
        '{{/data/~2}}',
        '{{/data',
    ],
)
def test_template_rejects_non_pointer_missing_and_invalid_syntax(template):
    from msg.client_output import render_template

    with pytest.raises(Failure, match='invalid_output_template'):
        render_template(template, {'data': {'name': 'hello'}})


def test_template_is_bounded():
    from msg.client_output import MAX_FORMAT_BYTES, render_template

    with pytest.raises(Failure, match='output_format_too_large'):
        render_template('a' * 4097, {})
    with pytest.raises(Failure, match='output_format_too_large'):
        render_template('{{/data}}{{/data}}', {'data': 'x' * (MAX_FORMAT_BYTES // 2)})


@pytest.mark.asyncio
async def test_jq_missing_executable_is_explicit(monkeypatch):
    import msg.client_output as output

    monkeypatch.setattr(output.shutil, 'which', lambda name: None)
    with pytest.raises(Failure, match='jq_unavailable'):
        await output.render_jq('.data', {'data': 1})


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['include "secrets"; .', 'import "/tmp/private" as p; p::read'])
async def test_jq_modules_are_rejected_before_process_creation(query, monkeypatch):
    import msg.client_output as output

    def forbidden(*args, **kwargs):
        raise AssertionError('must not start a process')

    monkeypatch.setattr(output.asyncio, 'create_subprocess_exec', forbidden)
    with pytest.raises(Failure, match='invalid_jq_query'):
        await output.render_jq(query, {})


@pytest.mark.asyncio
async def test_real_jq_operates_only_on_json_and_scrubbed_environment(monkeypatch):
    from msg.client_output import render_jq

    monkeypatch.setenv('MSG_TEST_FORMAT_SECRET', 'must-not-appear')
    assert (
        await render_jq(
            '.data.items[] | .name', {'data': {'items': [{'name': 'one'}, {'name': 'two'}]}}
        )
        == '"one"\n"two"\n'
    )
    assert await render_jq('env.MSG_TEST_FORMAT_SECRET', {}) == 'null\n'
    with pytest.raises(Failure, match='invalid_jq_query'):
        await render_jq('def broken', {})


@pytest.mark.asyncio
async def test_real_jq_output_limit_and_timeout():
    from msg.client_output import render_jq

    with pytest.raises(Failure, match='output_format_too_large'):
        await render_jq('"x" * 1048577', {})
    with pytest.raises(Failure, match='output_format_timeout'):
        await render_jq('def spin: spin; spin', {}, timeout=0.1)
