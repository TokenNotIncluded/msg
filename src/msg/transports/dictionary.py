"""Registry-derived, order-independent short codes for public GET paths."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from urllib.parse import quote

from msg.core.codec import canonical, digest, loads, parse_time, unb64
from msg.core.errors import Failure, require
from msg.core.models import OperationSpec


_PREFIX = {"namespace": "n", "operation": "o", "field": "f", "enum": "e"}
_SCALAR = {"string", "integer", "number", "boolean"}
_DEFAULT_PUBLISHED = object()

# Versioned, published path grammar for bounded GET-only read queries. The
# meanings of these short segments must not change within version 1.
READ_QUERY_V1_SEGMENTS = {'root':'r','type':'t','sort':'s','fields':'f',
                          'first':'n','after':'a'}
READ_QUERY_V2_SEGMENTS = {**READ_QUERY_V1_SEGMENTS,'expand':'x',
                          'nested_first':'nf','collection':'co','parent':'pa','limit':'l'}
READ_QUERY_V1_SORT = {'id':'i','time':'t','name':'n'}
READ_QUERY_V1_FIELDS = {'id':'i','type':'t','name':'n','revision':'v',
                        'generation':'g','path':'p','created_at':'c',
                        'modified_at':'m','owner':'o','group':'u','mode':'d'}
SEARCH_QUERY_V1_SEGMENTS = {'query':'q','tag':'t','limit':'n','cursor':'a'}


def read_query_path_document(registry):
    require(registry.operation('discovery.read_query').effect=='read','effect_mismatch')
    require(registry.operation('discovery.read_query',2).effect=='read','effect_mismatch')
    require(registry.operation('discovery.search').effect=='read','effect_mismatch')
    return {'version':1,'available_versions':[1,2],
            'segments':READ_QUERY_V1_SEGMENTS,'segments_v2':READ_QUERY_V2_SEGMENTS,
            'sort':READ_QUERY_V1_SORT,'fields':READ_QUERY_V1_FIELDS,
            'search_segments':SEARCH_QUERY_V1_SEGMENTS,
            'read_template':'/_read/q/1/r/{percent-encoded-root}/t/{type}/s/{sort-code}/f/{field-codes}/n/{first}',
            'read_template_v2':'/_read/q/2/r/{percent-encoded-root}/x/{collections}/n/{first}/nf/{nested-first}',
            'search_template':'/_search/q/1/q/{percent-encoded-query}/n/{limit}',
            'proof_suffix':'/p/{short-lived-signed-OperationRequest}',
            'continuation':'/_r/c/{opaque-cursor}',
            'query_ref':{'description':'Sealed read-only descriptor; never an authorization credential',
                         'upload':'/-/g/transfer.open -> transfer.part_put -> transfer.seal',
                         'seal':'/-/g/transfer.query_seal/j/{signed-packet}',
                         'descriptor':{'version':1,'kind':'read','arguments':'{ReadQuery arguments}'},
                         'read':'/_r/q/{query_ref}/p/{short-lived-signed-query_get-packet}'}}


@dataclass(frozen=True, slots=True, kw_only=True)
class DirectWritePath:
    """Decoded URL claims; the HTTP adapter must still authenticate the request."""

    spec: OperationSpec
    arguments: dict
    kind: str
    request_id: str
    expires_at: datetime
    subject: str | None = None
    credential_id: str | None = None
    expected_generations: tuple[tuple[str, int], ...] = ()
    token: bytes | None = field(default=None, repr=False)


def _code(kind: str, identity: str) -> str:
    # A code depends only on its semantic identity, never registration order.
    raw = hashlib.sha256(f"msg-dictionary-v1:{kind}:{identity}".encode()).digest()
    return _PREFIX[kind] + base64.b32encode(raw).decode("ascii").lower()[:7]


def _field_type(schema: dict) -> str | list[str] | None:
    return schema.get("type")


def _is_scalar(schema: dict) -> bool:
    kind = _field_type(schema)
    if isinstance(kind, str) and kind in _SCALAR:
        return True
    choices = schema.get("enum", [schema["const"]] if "const" in schema else [])
    return bool(choices) and all(type(value) in {str, int, float, bool} for value in choices)


def _example(schema: dict) -> str:
    if "enum" in schema:
        return str(schema["enum"][0])
    if "const" in schema:
        return str(schema["const"])
    match _field_type(schema):
        case "boolean":
            return "true"
        case "integer" | "number":
            return str(schema.get("minimum", 0))
        case _:
            return "example"


def _choices(schema: dict) -> list:
    if "enum" in schema:
        return schema["enum"]
    if "const" in schema:
        return [schema["const"]]
    return []


class ShortCodeDictionary:
    def __init__(self, registry, entry: str = "network", *, published: dict | None = None) -> None:
        require(registry.frozen, "registry_not_frozen")
        self.registry = registry
        self.entry = entry
        self._by_code: dict[str, dict[str, dict]] = {kind: {} for kind in _PREFIX}
        self._by_identity: dict[str, dict[str, str]] = {kind: {} for kind in _PREFIX}
        self._operations = {}
        operations = []
        namespaces = set()
        specs = sorted(registry.operations(entry), key=lambda spec: (spec.name, spec.version))
        for spec in specs:
            identity = f"{spec.name}@{spec.version}"
            namespace = spec.name.split(".", 1)[0]
            namespaces.add(namespace)
            op_code = self._add("operation", identity, value=identity)
            self._operations[op_code] = spec
            schema = registry.schema(spec.input_schema)
            properties = schema.get("properties", {})
            required = list(schema.get("required", []))
            fields = []
            for order, (name, field_schema) in enumerate(properties.items()):
                field_identity = f"{identity}:{name}"
                field_code = self._add("field", field_identity, value=name,
                                       operation=identity, path=name)
                enums = []
                for value in _choices(field_schema):
                    enum_identity = f"{field_identity}:{canonical(value).decode()}"
                    enum_code = self._add("enum", enum_identity, value=value,
                                          operation=identity, path=name)
                    enums.append({"code": enum_code, "value": value})
                fields.append({"name": name, "code": field_code,
                               "type": _field_type(field_schema), "required": name in required,
                               "order": required.index(name) if name in required else None,
                               "constraints": {key: value for key, value in field_schema.items()
                                               if key not in {"type", "enum"}},
                               "enum": enums})
            direct = (spec.name != "sharing.link_read" and spec.effect == "read" and
                      schema.get("type") == "object" and
                      all(name in properties and _is_scalar(properties[name]) for name in required))
            if spec.name == "sharing.link_read":
                template = "/-/p/sharing.link_read"
                example = None
            elif direct:
                template = "/-/g/" + op_code + "".join("/{" + name + "}" for name in required)
                example = "/-/g/" + op_code + "".join(
                    "/" + quote(
                        self.code_for("enum", f"{identity}:{name}:"
                                      f"{canonical(_choices(properties[name])[0]).decode()}")
                        if "enum" in properties[name] or "const" in properties[name]
                        else _example(properties[name]),
                        safe="") for name in required)
            else:
                template = "/-/g/" + spec.name + "/j/{packet}"
                example = None
            direct_write_template = None
            direct_expected_template = None
            if spec.effect != "read" and schema.get("type") == "object" and all(
                name in properties and _is_scalar(properties[name]) for name in required
            ):
                if spec.name == "identity.temporary":
                    direct_write_template = ("/-/g/" + op_code +
                        "/bootstrap/{request_id}/{UTC-expiry}/args")
                else:
                    direct_write_template = ("/-/g/" + op_code +
                        "/token/{credential_id}/{token}/{subject}/{request_id}/{UTC-expiry}/args")
                    direct_expected_template = direct_write_template.replace(
                        "/args", "/expected/{resource_id}/{generation}/args")
                direct_write_template += "".join(
                    "/" + self.code_for("field", f"{identity}:{name}") + "/{" + name + "}"
                    for name in required)
                if direct_expected_template is not None:
                    direct_expected_template += "".join(
                        "/" + self.code_for("field", f"{identity}:{name}") + "/{" + name + "}"
                        for name in required)
            operations.append({"name": spec.name, "version": spec.version,
                               "code": op_code, "namespace": namespace,
                               "effect": spec.effect, "fields": fields,
                               "required": required, "shortest_template": template,
                               "example": example, "direct": direct,
                               "direct_write_template": direct_write_template,
                               "direct_expected_template": direct_expected_template})
        for namespace in sorted(namespaces):
            self._add("namespace", namespace, value=namespace)
        self.document = {"version": 1, "entry": entry,
                         "codes": {kind: sorted(values.values(), key=lambda row: row["code"])
                                   for kind, values in self._by_code.items()},
                         "operations": operations}
        if published is not None:
            self._preserve_published(published)
        self.etag = '"' + digest(self.document)[7:] + '"'
        self.index_document = {"version": 1, "entry": entry, "namespaces": [
            {"name": namespace, "code": self.code_for("namespace", namespace),
             "operations": [{"name": row["name"], "code": row["code"],
                             "effect": row["effect"]}
                            for row in operations if row["namespace"] == namespace]}
            for namespace in sorted(namespaces)],
            "retired": [{"kind": kind, "code": row["code"],
                         "identity": row["identity"],
                         "replaced_by": row.get("replaced_by")}
                        for kind in _PREFIX for row in self.document["codes"][kind]
                        if row["deprecated"]]}
        self.index_etag = '"' + digest(self.index_document)[7:] + '"'
        schema_operations = []
        for spec in specs:
            schema_operations.append({**registry.describe(spec),
                                      "input": registry.schema(spec.input_schema),
                                      "output": registry.schema(spec.output_schema)})
        self.schema_document = {"version": 1, "entry": entry, "operations": schema_operations}
        self.schema_etag = '"' + digest(self.schema_document)[7:] + '"'

    def _preserve_published(self, published: dict) -> None:
        """Reject changed meanings and retain retired codes as visible tombstones."""
        require(published.get("version") == 1 and published.get("entry") == self.entry,
                "incompatible_published_dictionary")
        old_operations = {row["code"]: row for row in published.get("operations", [])}
        new_operations = {row["code"]: row for row in self.document["operations"]}
        for code, old in old_operations.items():
            if code in new_operations:
                require(canonical(old) == canonical(new_operations[code]),
                        "published_operation_changed")
        for kind in _PREFIX:
            current = self._by_code[kind]
            for old in published.get("codes", {}).get(kind, []):
                code = old["code"]
                if code in current:
                    require(current[code]["identity"] == old["identity"],
                            "published_short_code_reused")
                else:
                    retired = {**old, "deprecated": True}
                    current[code] = retired
                    self.document["codes"][kind].append(retired)
            self.document["codes"][kind].sort(key=lambda row: row["code"])

    def _add(self, kind: str, identity: str, **data) -> str:
        code = _code(kind, identity)
        existing = self._by_code[kind].get(code)
        require(existing is None or existing["identity"] == identity, "short_code_collision")
        row = {"code": code, "identity": identity, "deprecated": False,
               "replaced_by": None, **data}
        self._by_code[kind][code] = row
        self._by_identity[kind][identity] = code
        return code

    def resolve(self, kind: str, code: str) -> dict:
        require(kind in _PREFIX, "unknown_short_code_kind")
        try:
            row = self._by_code[kind][code]
        except KeyError as exc:
            raise Failure("unknown_short_code") from exc
        require(not row["deprecated"], "deprecated_short_code",
                details={"replaced_by": row.get("replaced_by")})
        return row

    def code_for(self, kind: str, identity: str) -> str:
        require(kind in _PREFIX, "unknown_short_code_kind")
        try:
            return self._by_identity[kind][identity]
        except KeyError as exc:
            raise Failure("unknown_short_code") from exc

    def resolve_operation(self, code: str):
        self.resolve("operation", code)
        return self._operations[code]

    def lookup_document(self, key: str) -> dict:
        """Return a small namespace or operation dictionary by name or code."""
        require(type(key) is str and bool(key), "unknown_short_code")
        for kind in ("namespace", "operation"):
            row = self._by_code[kind].get(key)
            if row is not None and row["deprecated"]:
                return {"version": 1, "entry": self.entry, "scope": key,
                        "kind": kind, "identity": row["identity"],
                        "deprecated": True, "replaced_by": row.get("replaced_by")}
        if key.startswith("n") and key in self._by_code["namespace"]:
            namespace = self.resolve("namespace", key)["identity"]
            operations = [row for row in self.document["operations"]
                          if row["namespace"] == namespace]
        elif key in self._by_identity["namespace"]:
            namespace = key
            operations = [row for row in self.document["operations"]
                          if row["namespace"] == namespace]
        else:
            if key in self._by_code["operation"]:
                code = key
            else:
                identity = key if "@" in key else key + "@1"
                code = self.code_for("operation", identity)
            self.resolve_operation(code)
            operations = [row for row in self.document["operations"] if row["code"] == code]
            namespace = operations[0]["namespace"]
        identities = {f"{row['name']}@{row['version']}" for row in operations}
        codes = {
            "namespace": [row for row in self.document["codes"]["namespace"]
                          if row["identity"] == namespace],
            "operation": [row for row in self.document["codes"]["operation"]
                          if row["identity"] in identities],
            "field": [row for row in self.document["codes"]["field"]
                      if row.get("operation") in identities],
            "enum": [row for row in self.document["codes"]["enum"]
                     if row.get("operation") in identities],
        }
        return {"version": 1, "entry": self.entry, "scope": key,
                "codes": codes, "operations": operations}

    def etag_for(self, key: str) -> str:
        return '"' + digest(self.lookup_document(key))[7:] + '"'

    def decode_get_path(self, operation_code: str, segments: list[str] | tuple[str, ...]):
        """Decode a read-only path into (OperationSpec, arguments), without authentication."""
        spec = self.resolve_operation(operation_code)
        require(spec.effect == "read", "direct_path_read_only")
        schema = self.registry.schema(spec.input_schema)
        properties = schema.get("properties", {})
        required = list(schema.get("required", []))
        require(all(name in properties and _is_scalar(properties[name]) for name in required),
                "direct_path_unavailable")
        require(len(segments) >= len(required), "missing_path_argument")
        values = {}
        for name, raw in zip(required, segments[:len(required)]):
            values[name] = self._decode_value(spec, name, raw)
        rest = segments[len(required):]
        require(len(rest) % 2 == 0, "invalid_path_arguments")
        for field_code, raw in zip(rest[::2], rest[1::2]):
            field = self.resolve("field", field_code)
            require(field.get("operation") == f"{spec.name}@{spec.version}",
                    "field_code_operation_mismatch")
            name = field["path"]
            require(name not in values and name in properties and _is_scalar(properties[name]),
                    "invalid_path_argument")
            values[name] = self._decode_value(spec, name, raw)
        self.registry.validate(spec.input_schema, values)
        return spec, values

    def decode_direct_write_path(
        self, operation_code: str, segments: list[str] | tuple[str, ...],
    ) -> DirectWritePath:
        """Decode token/bootstrap URL syntax without granting any authority."""
        spec = self.resolve_operation(operation_code)
        require(spec.effect != "read", "direct_path_write_required")
        require(isinstance(segments, (list, tuple)) and
                all(type(segment) is str for segment in segments), "invalid_path_arguments")
        require(bool(segments), "invalid_path_arguments")
        require(sum(len(segment.encode("utf-8")) + 1 for segment in segments) <= 8192,
                "path_too_large")
        kind = segments[0]
        expected_generations = ()
        if kind == "token":
            require(spec.name != "identity.temporary", "invalid_bootstrap")
            require(len(segments) >= 7, "invalid_direct_write_path")
            credential_id, encoded_token, subject, request_id, expiry = segments[1:6]
            require(all(re.fullmatch(r"[A-Za-z0-9_-]{1,160}", item)
                        for item in (credential_id, subject)) and
                    re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id) is not None,
                    "invalid_direct_write_metadata")
            token = unb64(encoded_token, limit=64)
            require(len(token) >= 24, "invalid_direct_write_metadata")
            if segments[6] == "args":
                rest = segments[7:]
            else:
                require(segments[6] == "expected", "invalid_direct_write_path")
                try:
                    args_index = segments.index("args", 7)
                except ValueError as exc:
                    raise Failure("invalid_direct_write_path") from exc
                expected_parts = segments[7:args_index]
                require(bool(expected_parts) and len(expected_parts) % 2 == 0 and
                        len(expected_parts) <= 64, "invalid_expected_generations")
                expected = []
                seen = set()
                for resource_id, generation in zip(expected_parts[::2], expected_parts[1::2]):
                    require(re.fullmatch(r"[A-Za-z0-9_-]{1,160}", resource_id) is not None and
                            resource_id not in {"args", "expected"} and resource_id not in seen and
                            re.fullmatch(r"(?:0|[1-9][0-9]{0,18})", generation) is not None and
                            int(generation) <= 2**63 - 1,
                            "invalid_expected_generations")
                    seen.add(resource_id)
                    expected.append((resource_id, int(generation)))
                expected_generations = tuple(expected)
                rest = segments[args_index + 1:]
        elif kind == "bootstrap":
            require(spec.name == "identity.temporary", "invalid_bootstrap")
            require(len(segments) >= 4 and segments[3] == "args", "invalid_direct_write_path")
            request_id, expiry = segments[1:3]
            require(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id) is not None,
                    "invalid_direct_write_metadata")
            credential_id = subject = token = None
            rest = segments[4:]
        else:
            raise Failure("invalid_direct_write_path")
        require(1 <= len(expiry) <= 40, "invalid_direct_write_metadata")
        expires_at = parse_time(expiry)
        require(len(rest) % 2 == 0, "invalid_path_arguments")
        schema = self.registry.schema(spec.input_schema)
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        require(all(name in properties and _is_scalar(properties[name]) for name in required),
                "direct_path_unavailable")
        arguments = {}
        for field_code, raw in zip(rest[::2], rest[1::2]):
            require(len(raw.encode("utf-8")) <= 4096, "direct_path_value_too_long")
            field_row = self.resolve("field", field_code)
            require(field_row.get("operation") == f"{spec.name}@{spec.version}",
                    "field_code_operation_mismatch")
            name = field_row["path"]
            require(name not in arguments and name in properties and _is_scalar(properties[name]),
                    "invalid_path_argument")
            arguments[name] = self._decode_value(spec, name, raw)
        self.registry.validate(spec.input_schema, arguments)
        if kind == "bootstrap":
            require(set(arguments) == {"nonce"}, "invalid_bootstrap")
            claim = unb64(arguments["nonce"], limit=64)
            require(len(claim) >= 24, "invalid_bootstrap_nonce")
        return DirectWritePath(spec=spec, arguments=arguments, kind=kind,
                               request_id=request_id, expires_at=expires_at,
                               subject=subject, credential_id=credential_id,
                               expected_generations=expected_generations, token=token)

    def _decode_value(self, spec, name: str, raw: str):
        require(type(raw) is str, "invalid_path_argument")
        schema = self.registry.schema(spec.input_schema)["properties"][name]
        if "enum" in schema or "const" in schema:
            enum = self.resolve("enum", raw)
            require(enum.get("operation") == f"{spec.name}@{spec.version}" and
                    enum.get("path") == name, "enum_code_field_mismatch")
            return enum["value"]
        kind = schema.get("type")
        if kind == "string":
            return raw
        if kind == "boolean":
            require(raw in {"true", "false"}, "invalid_path_boolean")
            return raw == "true"
        if kind == "integer":
            require(raw.isdecimal() and (raw == "0" or not raw.startswith("0")),
                    "invalid_path_integer")
            return int(raw)
        if kind == "number":
            try:
                value = float(raw)
            except ValueError as exc:
                raise Failure("invalid_path_number") from exc
            require(value == value and abs(value) != float("inf"), "invalid_path_number")
            return value
        raise Failure("direct_path_unavailable")


def build_dictionary(registry, entry: str = "network", *,
                     published: dict | None | object = _DEFAULT_PUBLISHED) -> ShortCodeDictionary:
    if published is _DEFAULT_PUBLISHED:
        try:
            published = loads(files("msg.data").joinpath("shortcodes.json").read_bytes())
        except FileNotFoundError:
            # A source checkout can generate its first publication snapshot.
            published = None
    return ShortCodeDictionary(registry, entry, published=published)
