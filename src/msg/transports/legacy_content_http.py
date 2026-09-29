"""Locate explicitly namespaced legacy imports; caller must authorize the target."""

from __future__ import annotations

import re
from dataclasses import dataclass

from msg.core.errors import require
from msg.storage.legacy_resource_import import PURPOSE


@dataclass(frozen=True)
class LegacyReadTarget:
    resource_id: str
    canonical_path: str
    view: str
    source_sha256: str
    provenance_path: str | None


async def resolve_legacy_read(tx, path: str, raw_path: bytes):
    if not path.startswith("/_legacy/"):
        return None
    # No alternate escaped spellings or normalization-dependent routing.
    require(raw_path == path.encode("utf-8") and b"%" not in raw_path, "not_found")
    match = re.fullmatch(r"/_legacy/([0-9a-f]{64})(/[^?#]+)", path)
    require(match is not None, "not_found")
    sha256, old_path = match.groups()
    require(all(part and part not in {".", ".."} for part in old_path.split("/")[1:]), "not_found")
    report = tx.setting("legacy-import:" + sha256)
    require(
        isinstance(report, dict)
        and report.get("format") == PURPOSE
        and report.get("source_sha256") == sha256
        and report.get("visibility") == "private"
        and report.get("authority_enabled") is False
        and report.get("external_jobs_enabled") is False,
        "not_found",
    )
    mapping = report.get("url_map", {})
    require(isinstance(mapping, dict), "not_found")
    rid = mapping.get(old_path)
    provenance = False
    base_path = old_path
    if rid is None and old_path.endswith("/provenance"):
        base_path = old_path.removesuffix("/provenance")
        rid = mapping.get(base_path)
        require(isinstance(rid, str), "not_found")
        record = tx.setting("legacy-provenance:" + rid)
        require(isinstance(record, dict) and isinstance(record.get("resource"), str), "not_found")
        rid = record["resource"]
        provenance = True
    require(isinstance(rid, str), "not_found")
    resource = await tx.resource(rid)
    ancestors = await tx.ancestors(rid)
    require(any(parent.id == report.get("parent") for parent in ancestors), "not_found")
    view = "json" if provenance else "markdown"
    if not provenance:
        suffix = old_path.rsplit("/", 1)[-1]
        prefix = old_path.rsplit("/", 1)[0]
        if suffix in {"raw", "meta"} and mapping.get(prefix) == rid:
            view = suffix
            base_path = prefix
        elif re.fullmatch(r"/file/[0-9]+", old_path):
            view = "raw"
    canonical_path = await tx.path(resource.id)
    if view != "markdown":
        canonical_path += "/" + view
    record = tx.setting("legacy-provenance:" + rid) if not provenance else None
    provenance_path = (
        "/_legacy/" + sha256 + base_path + "/provenance" if isinstance(record, dict) else None
    )
    return LegacyReadTarget(rid, canonical_path, view, sha256, provenance_path)
