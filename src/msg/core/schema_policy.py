"""Self-contained JSON Schema contracts with no implicit filesystem/network I/O."""
from jsonschema import Draft202012Validator
from referencing import Registry as ReferenceRegistry
from referencing.jsonschema import DRAFT202012

from msg.core.errors import require


def local_validator(schema):
    Draft202012Validator.check_schema(schema)
    pending = [DRAFT202012.create_resource(schema)]
    while pending:
        resource = pending.pop()
        contents = resource.contents
        if isinstance(contents, dict):
            for keyword in ('$ref', '$dynamicRef', '$recursiveRef'):
                if keyword in contents:
                    reference = contents[keyword]
                    require(isinstance(reference, str) and
                            (reference == '' or reference.startswith('#')),
                            'remote_schema_reference_forbidden')
        # Follow schema-bearing keywords, not properties named "$ref" or ordinary
        # instance data in default/examples/const. Nested dialects are respected.
        pending.extend(resource.subresources())
    # Fragment pointers may target an annotation containing another schema. An
    # explicit registry also blocks that path: its default retriever performs no
    # I/O, unlike jsonschema's legacy automatic reference retrieval behavior.
    return Draft202012Validator(schema, registry=ReferenceRegistry())
