"""Public short-code dictionary and direct read-path contract."""

from copy import deepcopy
from importlib.resources import files
from types import SimpleNamespace

import pytest

from msg.application import Application
from msg.core.codec import b64, loads
from msg.core.errors import Failure
from msg.transports.dictionary import build_dictionary


@pytest.fixture
def registry():
    settings = SimpleNamespace(server=SimpleNamespace(
        plugins=("identity", "content", "discussion", "communication", "discovery")))
    return Application(settings).registry


def test_registry_drives_stable_public_documents(registry):
    first = build_dictionary(registry)
    second = build_dictionary(registry)
    assert first.document == second.document
    assert first.etag == second.etag
    assert first.schema_document == second.schema_document
    assert first.schema_etag == second.schema_etag
    assert first.etag.startswith('"') and first.etag.endswith('"')
    assert first.document["codes"].keys() == {"namespace", "operation", "field", "enum"}
    assert first.document["operations"]
    for operation in first.document["operations"]:
        assert {"required", "shortest_template", "example", "fields"} <= operation.keys()
        for field in operation["fields"]:
            assert {"type", "required", "order", "constraints", "code"} <= field.keys()

    class ReorderedRegistry:
        frozen = True

        def operations(self, entry):
            return tuple(reversed(registry.operations(entry)))

        def schema(self, ref):
            return registry.schema(ref)

        def describe(self, spec):
            return registry.describe(spec)

        def validate(self, ref, value):
            return registry.validate(ref, value)

    reordered = build_dictionary(ReorderedRegistry())
    assert reordered.document == first.document
    assert reordered.etag == first.etag


def test_direct_read_decodes_required_and_optional_short_fields(registry):
    dictionary = build_dictionary(registry)
    operation = "discovery.get@1"
    op_code = dictionary.code_for("operation", operation)
    limit_code = dictionary.code_for("field", operation + ":limit")
    spec, arguments = dictionary.decode_get_path(op_code, ["/main", limit_code, "12"])
    assert spec.name == "discovery.get"
    assert arguments == {"id": "/main", "limit": 12}
    assert dictionary.resolve_operation(op_code) is spec


def test_unknown_or_cross_operation_codes_fail_closed(registry):
    dictionary = build_dictionary(registry)
    read = dictionary.code_for("operation", "discovery.get@1")
    other_field = dictionary.code_for("field", "discovery.list@1:limit")
    mutation = dictionary.code_for("operation", "content.post_create@1")
    with pytest.raises(Failure, match="unknown_short_code"):
        dictionary.resolve_operation("ounknown")
    with pytest.raises(Failure, match="unknown_short_code"):
        dictionary.decode_get_path(read, ["/main", "funknown", "1"])
    with pytest.raises(Failure, match="field_code_operation_mismatch"):
        dictionary.decode_get_path(read, ["/main", other_field, "1"])
    with pytest.raises(Failure, match="direct_path_read_only"):
        dictionary.decode_get_path(mutation, ["/main", "hello"])
    with pytest.raises(Failure, match="missing_path_argument"):
        dictionary.decode_get_path(read, [])
    with pytest.raises(Failure, match="invalid_path_arguments"):
        dictionary.decode_get_path(read, ["/main", "fextra"])


def test_enum_codes_are_scoped_to_field(registry):
    dictionary = build_dictionary(registry)
    operation = "discovery.list@1"
    op_code = dictionary.code_for("operation", operation)
    state_code = dictionary.code_for("field", operation + ":state")
    state_field = next(field for field in dictionary.document["operations"]
                       if field["name"] == "discovery.list")
    state = next(field for field in state_field["fields"] if field["name"] == "state")
    value = state["enum"][0]
    _, arguments = dictionary.decode_get_path(op_code, [state_code, value["code"]])
    assert arguments == {"state": value["value"]}
    with pytest.raises(Failure, match="unknown_short_code"):
        dictionary.decode_get_path(op_code, [state_code, str(value["value"])])


def test_published_dictionary_rejects_changed_meanings_and_keeps_retired_codes(registry):
    original = build_dictionary(registry).document
    changed = deepcopy(original)
    changed["operations"][0]["effect"] = "read"
    with pytest.raises(Failure, match="published_operation_changed"):
        build_dictionary(registry, published=changed)

    class ReducedRegistry:
        frozen = True

        def operations(self, entry):
            return tuple(spec for spec in registry.operations(entry)
                         if spec.name != "discovery.get")

        def schema(self, ref):
            return registry.schema(ref)

        def describe(self, spec):
            return registry.describe(spec)

    reduced = build_dictionary(ReducedRegistry(), published=original)
    old_code = build_dictionary(registry).code_for("operation", "discovery.get@1")
    retired = next(row for row in reduced.document["codes"]["operation"]
                   if row["code"] == old_code)
    assert retired["deprecated"] and retired["replaced_by"] is None
    assert reduced.lookup_document(old_code)["deprecated"]
    assert any(row["code"] == old_code for row in reduced.index_document["retired"])
    with pytest.raises(Failure, match="deprecated_short_code"):
        reduced.resolve_operation(old_code)


def test_scoped_dictionary_keeps_only_requested_namespace_or_operation(registry):
    dictionary = build_dictionary(registry)
    assert dictionary.index_document["namespaces"]
    assert all(set(operation) == {"name", "code", "effect"}
               for namespace in dictionary.index_document["namespaces"]
               for operation in namespace["operations"])
    assert dictionary.index_etag != dictionary.etag
    namespace_code = dictionary.code_for("namespace", "discovery")
    namespace = dictionary.lookup_document(namespace_code)
    assert namespace["operations"]
    assert all(row["namespace"] == "discovery" for row in namespace["operations"])
    assert len(namespace["operations"]) < len(dictionary.document["operations"])
    assert len(dictionary.lookup_document("discovery")["operations"]) == len(namespace["operations"])
    op_code = dictionary.code_for("operation", "discovery.get@1")
    operation = dictionary.lookup_document(op_code)
    assert len(operation["operations"]) == 1
    assert operation["operations"][0]["name"] == "discovery.get"
    assert len(dictionary.lookup_document("discovery.get")["operations"]) == 1
    assert len(operation["codes"]["operation"]) == 1
    assert all(row["operation"] == "discovery.get@1" for row in operation["codes"]["field"])
    assert dictionary.etag_for(op_code) == dictionary.etag_for(op_code)
    assert dictionary.etag_for(op_code) != dictionary.etag
    with pytest.raises(Failure, match="unknown_short_code"):
        dictionary.lookup_document("unknown.operation")


def test_non_direct_template_uses_full_operation_packet_route(registry):
    dictionary = build_dictionary(registry)
    operation = next(row for row in dictionary.document["operations"]
                     if row["name"] == "content.post_create")
    assert not operation["direct"]
    assert operation["shortest_template"] == "/-/g/content.post_create/j/{packet}"


def test_default_build_checks_packaged_publication_snapshot(registry):
    snapshot = loads(files("msg.data").joinpath("shortcodes.json").read_bytes())
    assert len(snapshot["operations"]) >= len(registry.operations("network"))
    automatic = build_dictionary(registry)
    explicit = build_dictionary(registry, published=snapshot)
    assert automatic.document == explicit.document
    assert automatic.document != build_dictionary(registry, published=None).document


def test_token_direct_write_decodes_explicit_claims_and_short_fields(registry):
    dictionary = build_dictionary(registry, published=None)
    operation = "content.post_create@1"
    code = dictionary.code_for("operation", operation)
    parent = dictionary.code_for("field", operation + ":parent")
    body = dictionary.code_for("field", operation + ":body")
    token = b"t" * 32
    decoded = dictionary.decode_direct_write_path(code, [
        "token", "t_test", b64(token), "u_test", "request_1",
        "2026-09-27T00:05:00Z", "args", parent, "/main", body, "hello",
    ])
    assert decoded.spec.name == "content.post_create"
    assert decoded.arguments == {"parent": "/main", "body": "hello"}
    assert decoded.kind == "token" and decoded.subject == "u_test"
    assert decoded.credential_id == "t_test" and decoded.token == token
    assert decoded.request_id == "request_1" and decoded.expires_at.utcoffset().total_seconds() == 0
    assert b64(token) not in repr(decoded)
    operation_row = next(row for row in dictionary.document["operations"]
                         if row["name"] == "content.post_create")
    assert operation_row["direct_write_template"].startswith("/-/g/" + code + "/token/")
    assert "/" + parent + "/{parent}" in operation_row["direct_write_template"]
    assert "/expected/{resource_id}/{generation}/args/" in operation_row["direct_expected_template"]

    expected = dictionary.decode_direct_write_path(code, [
        "token", "t_test", b64(token), "u_test", "request_1",
        "2026-09-27T00:05:00Z", "expected", "p_one", "0", "p_two", "12",
        "args", parent, "/main",
    ])
    assert expected.expected_generations == (("p_one", 0), ("p_two", 12))


def test_bootstrap_direct_write_is_only_identity_temporary(registry):
    dictionary = build_dictionary(registry, published=None)
    operation = "identity.temporary@1"
    code = dictionary.code_for("operation", operation)
    nonce_code = dictionary.code_for("field", operation + ":nonce")
    nonce = b64(b"n" * 32)
    decoded = dictionary.decode_direct_write_path(code, [
        "bootstrap", "bootstrap_1", "2026-09-27T00:05:00Z", "args", nonce_code, nonce,
    ])
    assert decoded.kind == "bootstrap" and decoded.arguments == {"nonce": nonce}
    assert decoded.subject is None and decoded.credential_id is None and decoded.token is None
    operation_row = next(row for row in dictionary.document["operations"]
                         if row["name"] == "identity.temporary")
    assert operation_row["direct_write_template"].startswith("/-/g/" + code + "/bootstrap/")
    with pytest.raises(Failure, match="invalid_bootstrap"):
        dictionary.decode_direct_write_path(
            dictionary.code_for("operation", "content.post_create@1"),
            ["bootstrap", "bootstrap_1", "2026-09-27T00:05:00Z", "args", nonce_code, nonce],
        )


def test_direct_write_rejects_missing_unknown_repeated_and_complex_fields(registry):
    dictionary = build_dictionary(registry, published=None)
    operation = "content.post_create@1"
    code = dictionary.code_for("operation", operation)
    parent = dictionary.code_for("field", operation + ":parent")
    prefix = ["token", "t_test", b64(b"t" * 32), "u_test", "request_1",
              "2026-09-27T00:05:00Z", "args"]
    cases = [
        prefix,
        prefix + ["funknown", "/main"],
        prefix + [parent, "/main", parent, "/other"],
        prefix + [parent],
        prefix + [parent, "/main", dictionary.code_for("field", "discovery.get@1:id"), "x"],
        prefix + [parent, "/main", dictionary.code_for("field", operation + ":body"), "x" * 4097],
        prefix + [parent, "/main", dictionary.code_for("field", operation + ":body"), "x" * 8192],
        ["token", "t_test", b64(b"t" * 32), "u_test", "", *prefix[5:], parent, "/main"],
        ["token", "t_test", b64(b"t" * 32), "u_test", "r" * 129,
         *prefix[5:], parent, "/main"],
        ["token", "t" * 161, b64(b"t" * 32), "u_test", "request_1",
         *prefix[5:], parent, "/main"],
        ["token", "t_test", b64(b"t" * 32), "u" * 161, "request_1",
         *prefix[5:], parent, "/main"],
        ["token", "t_test", b64(b"t" * 65), "u_test", "request_1",
         *prefix[5:], parent, "/main"],
        ["token", "t_test", b64(b"t" * 32), "u_test", "request_1",
         "2" * 41, "args", parent, "/main"],
        [*prefix[:6], "expected", "p_one", "-1", "args", parent, "/main"],
        [*prefix[:6], "expected", "p_one", "1.5", "args", parent, "/main"],
        [*prefix[:6], "expected", "p_one", "01", "args", parent, "/main"],
        [*prefix[:6], "expected", "p_one", "1", "p_one", "2", "args", parent, "/main"],
        [*prefix[:6], "expected", "p_one", "1", parent, "/main"],
    ]
    for segments in cases:
        with pytest.raises(Failure):
            dictionary.decode_direct_write_path(code, segments)
    complex_code = dictionary.code_for("operation", "discussion.reply@1")
    with pytest.raises(Failure, match="direct_path_unavailable"):
        dictionary.decode_direct_write_path(complex_code, prefix)
    read_code = dictionary.code_for("operation", "discovery.get@1")
    with pytest.raises(Failure, match="direct_path_write_required"):
        dictionary.decode_direct_write_path(read_code, prefix + [parent, "/main"])
    bootstrap_code = dictionary.code_for("operation", "identity.temporary@1")
    with pytest.raises(Failure, match="invalid_direct_write_path"):
        dictionary.decode_direct_write_path(bootstrap_code, [
            "bootstrap", "bootstrap_1", "2026-09-27T00:05:00Z", "expected",
            "p_one", "0", "args", dictionary.code_for("field", "identity.temporary@1:nonce"),
            b64(b"n" * 32),
        ])


def test_direct_transfer_accepts_one_kibibyte_data(registry):
    plugins = ("identity", "content", "discussion", "communication", "discovery",
               "transfer", "extensions", "system", "batch")
    full_registry = Application(SimpleNamespace(server=SimpleNamespace(plugins=plugins))).registry
    dictionary = build_dictionary(full_registry)
    operation = "transfer.part_put@1"
    code = dictionary.code_for("operation", operation)

    def field(name):
        return dictionary.code_for("field", operation + ":" + name)

    data = b64(bytes(range(256)) * 4)
    prefix = ["token", "t_test", b64(b"t" * 32), "u_test", "transfer_1",
              "2026-09-27T00:05:00Z", "args",
              field("transfer_id"), "tr_test", field("offset"), "0",
              field("data"), data, field("digest"), "sha256:" + "0" * 64]
    decoded = dictionary.decode_direct_write_path(code, prefix)
    assert decoded.arguments["data"] == data
    assert len(decoded.arguments["data"]) > 1024
    prefix[-3] = "A" * 4097
    with pytest.raises(Failure, match="direct_path_value_too_long"):
        dictionary.decode_direct_write_path(code, prefix)
