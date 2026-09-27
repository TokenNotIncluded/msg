"""Versioned deterministic JSON, strict parsing and immutable contract records.

Canonical v1 uses UTF-8, sorted Unicode keys, compact separators and JSON finite
numbers as emitted by Python. It is not advertised as RFC 8785/JCS. Signed
request test vectors define the wire representation for non-Python clients.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import math
import re
import types
import typing
from functools import lru_cache
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from .errors import Failure, require


def freeze_json(value):
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        require(math.isfinite(value), "non_finite_float")
        return value
    if isinstance(value, (tuple, list)):
        return tuple(freeze_json(x) for x in value)
    if isinstance(value, Mapping):
        require(all(type(k) is str for k in value), "non_string_json_key")
        return MappingProxyType({k: freeze_json(v) for k, v in value.items()})
    raise Failure("unsupported_json_type")


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(value: str, *, limit: int = 16 * 1024 * 1024) -> bytes:
    require(type(value) is str and len(value) <= (limit * 4 // 3 + 4), "invalid_base64")
    require(bool(re.fullmatch(r"[A-Za-z0-9_-]*", value)), "invalid_base64")
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise Failure("invalid_base64") from exc
    require(b64(result) == value and len(result) <= limit, "invalid_base64")
    return result


def timestamp(value: datetime) -> str:
    require(isinstance(value, datetime) and value.tzinfo is not None and
            value.utcoffset() is not None, "naive_datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    require(type(value) is str and value.endswith("Z"), "invalid_datetime")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise Failure("invalid_datetime") from exc
    require(result.tzinfo is not None, "naive_datetime")
    return result.astimezone(UTC)


def wire(value, *, compact: bool = False):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result = {}
        for field in dataclasses.fields(value):
            v = getattr(value, field.name)
            if v is None and field.metadata.get('omit_if_none'):
                continue
            if compact and (v is None or v == () or v == frozenset()):
                continue
            result[field.name] = f"{v:04o}" if field.name == "mode" else wire(v, compact=compact)
        return result
    if isinstance(value, Mapping):
        return {k: wire(v, compact=compact) for k, v in value.items()
                if not compact or v is not None}
    if isinstance(value, (tuple, list)):
        return [wire(v, compact=compact) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((wire(v, compact=compact) for v in value), key=canonical)
    if isinstance(value, datetime):
        return timestamp(value)
    if isinstance(value, bytes):
        return b64(value)
    if isinstance(value, Path):
        return str(value)
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise Failure("invalid_json_value")


def canonical(value) -> bytes:
    try:
        return json.dumps(wire(value), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, Failure):
            raise
        raise Failure("invalid_json_value") from exc


def digest(value: bytes | object) -> str:
    return "sha256:" + hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def loads(raw: str | bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key", key)
            result[key] = value
        return result
    def constant(_):
        raise Failure("non_finite_float")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        freeze_json(value)
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, Failure):
            raise
        raise Failure("invalid_json") from exc


@lru_cache(maxsize=256)
def _record_shape(cls):
    # Registered Python classes are immutable schema definitions. This cache
    # never contains user identities, authorization decisions, or mutable rows.
    return ({f.name: f for f in dataclasses.fields(cls)}, typing.get_type_hints(cls))


def decode(cls, value, field: str = ""):
    """Decode only declared fields; booleans never masquerade as integers."""
    if isinstance(cls, typing.TypeAliasType):
        if cls.__name__ in {"Json", "JsonMap"}:
            if cls.__name__ == "JsonMap":
                require(isinstance(value, Mapping), "invalid_type", field)
            return freeze_json(value)
        return decode(cls.__value__, value, field)
    if hasattr(cls, "__supertype__"):
        return decode(cls.__supertype__, value, field)
    origin, args = typing.get_origin(cls), typing.get_args(cls)
    if origin in (typing.Union, types.UnionType):
        for option in args:
            try:
                return decode(option, value, field)
            except (Failure, TypeError, ValueError):
                continue
        raise Failure("invalid_union", field)
    if origin is typing.Literal:
        require(any(type(value) is type(v) and value == v for v in args), "invalid_enum", field)
        return value
    if origin in (tuple, list, frozenset, set):
        require(isinstance(value, (tuple, list, set, frozenset)), "invalid_type", field)
        if origin is tuple and args and args[-1] is not Ellipsis:
            require(len(args) == len(value), "invalid_length", field)
            return tuple(decode(t, x, field) for t, x in zip(args, value, strict=True))
        child = args[0] if args else typing.Any
        return origin(decode(child, x, field) for x in value)
    if origin in (dict, Mapping):
        require(isinstance(value, Mapping), "invalid_type", field)
        return MappingProxyType({decode(args[0], k, field): decode(args[1], v, field)
                                 for k, v in value.items()})
    if cls is datetime:
        return parse_time(value)
    if cls is bytes:
        return unb64(value)
    if cls is Path:
        require(type(value) is str, "invalid_type", field)
        return Path(value)
    if cls is typing.Any:
        return freeze_json(value)
    if dataclasses.is_dataclass(cls):
        require(isinstance(value, Mapping), "invalid_type", field)
        fields, hints = _record_shape(cls)
        unknown = set(value) - fields.keys()
        require(not unknown, "unknown_field", (field + "." if field else "") +
                (sorted(unknown)[0] if unknown else ""))
        result = {}
        for name, f in fields.items():
            where = f"{field}.{name}" if field else name
            if name not in value:
                require(f.default is not dataclasses.MISSING or
                        f.default_factory is not dataclasses.MISSING, "missing_field", where)
                continue
            if name == "mode":
                mode = value[name]
                require(type(mode) is str and re.fullmatch(r"[0-7]{4}", mode) is not None,
                        "invalid_mode", where)
                result[name] = int(mode, 8)
            else:
                result[name] = decode(hints[name], value[name], where)
        return cls(**result)
    if cls in (str, bool, int, float, type(None)):
        require(type(value) is cls, "invalid_type", field)
        if cls is float:
            require(math.isfinite(value), "non_finite_float", field)
        return value
    raise Failure("unsupported_contract_type", field)


def validate_record(obj):
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        if isinstance(value, datetime):
            require(value.tzinfo is not None and value.utcoffset() is not None, "naive_datetime", f.name)
            object.__setattr__(obj, f.name, value.astimezone(UTC))
        elif isinstance(value, Mapping):
            object.__setattr__(obj, f.name, freeze_json(value))
        elif isinstance(value, list):
            object.__setattr__(obj, f.name, tuple(value))
        elif isinstance(value, set):
            object.__setattr__(obj, f.name, frozenset(value))
        if f.name in {"generation", "size", "expected_size", "offset", "attempts", "delegation_depth",
                      "max_child_ca_depth", "max_delegation_depth", "max_redirects"} and value is not None:
            require(type(value) is int and value >= 0, "invalid_nonnegative_integer", f.name)
        if f.name in {"version", "type_version", "format_version", "protocol_version", "contract_version",
                      "max_cert_ttl_seconds", "requested_ttl_seconds"}:
            if f.name == "version" and type(obj).__name__ == "PluginManifest":
                require(type(value) is str and bool(value), "invalid_version", f.name)
            elif f.name == "version" and type(obj).__name__ == "Membership":
                require(type(value) is int and value >= 0, "invalid_version", f.name)
            else:
                require(type(value) is int and value >= 1, "invalid_version", f.name)
    if type(obj).__name__ == "Resource":
        from msg.core.tags import normalize_tags

        require(type(obj.mode) is int and 0 <= obj.mode <= 0o7777, "invalid_mode")
        require(obj.state in {"active", "archived", "purged"}, "invalid_state")
        require(obj.parent != obj.id, "parent_cycle")
        require(obj.tags == normalize_tags(obj.tags), "invalid_tags")
    if type(obj).__name__ == "Relation" and obj.excerpt is not None:
        require(len(obj.excerpt) == 2 and all(type(n) is int for n in obj.excerpt)
                and 0 <= obj.excerpt[0] <= obj.excerpt[1], "invalid_byte_range")
    if hasattr(obj, "not_before") and getattr(obj, "expires_at", None) is not None:
        require(obj.not_before < obj.expires_at, "invalid_validity")


def record(**kwargs):
    """Dataclass decoration plus local validation, not a domain base class."""
    def decorate(cls):
        cls.__post_init__ = validate_record
        return dataclasses.dataclass(**kwargs)(cls)
    return decorate
