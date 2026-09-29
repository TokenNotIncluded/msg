"""A non-executable, immutable template DSL."""

from __future__ import annotations

import re
from dataclasses import dataclass

from msg.core.codec import canonical, decode, freeze_json, loads
from msg.core.errors import Failure, require
from msg.core.models import FieldSpec, ResourceRef


@dataclass(frozen=True, slots=True)
class TemplateDefinition:
    name: str
    version: int
    fields: tuple[FieldSpec, ...]


TemplateSyntaxError = Failure


def _check(spec, value):
    if value is None and not spec.required:
        return None
    if spec.type in {'str', 'text', 'enum'}:
        require(type(value) is str, 'template_type_error', spec.name)
    elif spec.type == 'int':
        require(type(value) is int, 'template_type_error', spec.name)
    elif spec.type == 'bool':
        require(type(value) is bool, 'template_type_error', spec.name)
    elif spec.type in {'ref', 'file'}:
        if not isinstance(value, str):
            decode(ResourceRef, value)
        else:
            require(
                bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value)),
                'template_reference_error',
                spec.name,
            )
    if spec.type == 'enum':
        require(value in spec.choices, 'template_enum_error', spec.name)
    return freeze_json(value)


def parse_template(source):
    require(type(source) is str, 'invalid_template')
    lines = [s.strip() for s in source.splitlines() if s.strip() and not s.lstrip().startswith('#')]
    require(bool(lines), 'empty_template')
    header = re.fullmatch(r'([A-Za-z][\w.-]*)@([1-9][0-9]*)', lines[0])
    require(header is not None, 'invalid_template_header')
    result = []
    names = set()
    for line in lines[1:]:
        match = re.fullmatch(r'([A-Za-z][\w.-]*):([a-z]+(?:\([^)]*\))?)([!?]?)(?:=(.*))?', line)
        require(match is not None, 'invalid_template_field')
        name, type_name, marker, default = match.groups()
        require(name not in names, 'duplicate_field', name)
        names.add(name)
        choices = ()
        if type_name.startswith('enum('):
            choices = tuple(x.strip() for x in type_name[5:-1].split(','))
            require(all(choices) and len(choices) == len(set(choices)), 'invalid_enum', name)
            type_name = 'enum'
        require(
            type_name in {'str', 'text', 'int', 'bool', 'enum', 'ref', 'file'},
            'unknown_field_type',
            name,
        )
        require(marker != '!' or default is None, 'required_field_has_default', name)
        spec = FieldSpec(name=name, type=type_name, required=marker == '!', choices=choices)
        if default is not None:
            try:
                value = loads(default)
            except Failure:
                require(
                    type_name in {'str', 'text', 'enum', 'ref', 'file'}, 'invalid_default', name
                )
                value = default
            value = _check(spec, value)
            spec = FieldSpec(
                name=name,
                type=type_name,
                required=False,
                choices=choices,
                default_json=canonical(value),
            )
        result.append(spec)
    return TemplateDefinition(name=header[1], version=int(header[2]), fields=tuple(result))


def normalize_values(template, values):
    require(isinstance(values, dict) or hasattr(values, 'items'), 'invalid_template_values')
    names = {s.name for s in template.fields}
    require(set(values) <= names, 'unknown_template_field')
    result = {}
    for spec in template.fields:
        if spec.name in values:
            result[spec.name] = _check(spec, values[spec.name])
        elif spec.default_json is not None:
            result[spec.name] = loads(spec.default_json)
        else:
            require(not spec.required, 'missing_required', spec.name)
    return result


def canonical_values_json(values):
    return canonical(values)


def render_values(values):
    return (
        '\n\n'.join(
            f'**{name}**\n\n{value if isinstance(value, str) else canonical(value).decode()}'
            for name, value in values.items()
        )
        + '\n'
    )
