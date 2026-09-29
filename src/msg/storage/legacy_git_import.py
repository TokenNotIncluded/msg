"""Fixed-ref legacy Git preservation and Root-approved private repository import."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

from msg.core.codec import b64, canonical, decode, digest, parse_time
from msg.core.errors import require
from msg.core.models import Principal, Signature
from msg.extensions.repositories import (
    NativeGitStore,
    MAX_GIT_PACK_BYTES,
    require_git_repository_capacity,
)
from msg.plugins.common import create_resource
from msg.security.crypto import verify
from msg.security.quarantine import require_live_authority

PURPOSE = "legacy-git-private-import-v1"


def git_env():
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_git(directory, *args):
    result = subprocess.run(
        [
            "git",
            "--git-dir",
            str(directory),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *args,
        ],
        capture_output=True,
        env=git_env(),
        timeout=120,
    )
    require(result.returncode == 0, "legacy_git_verification_failed")
    return result.stdout


def bundle_refs(bundle):
    result = subprocess.run(
        ["git", "bundle", "list-heads", str(bundle)], capture_output=True, env=git_env(), timeout=30
    )
    require(result.returncode == 0, "legacy_git_bundle_invalid")
    refs = {}
    for line in result.stdout.decode("utf-8").splitlines():
        oid, name = line.split(" ", 1)
        require(
            re.fullmatch(r"[0-9a-f]{40}", oid) is not None
            and re.fullmatch(r"refs/(heads|tags)/[^\s]+", name) is not None,
            "legacy_git_ref_unsupported",
        )
        require(name not in refs, "legacy_git_duplicate_ref")
        refs[name] = oid
    require(0 < len(refs) <= 128, "legacy_git_ref_count_invalid")
    return refs


def verify_bundle(bundle: Path, expected_sha256: str, *, protected_work: Path):
    bundle = Path(bundle)
    require(
        bundle.is_file()
        and not bundle.is_symlink()
        and 0 < bundle.stat().st_size <= MAX_GIT_PACK_BYTES,
        "legacy_git_bundle_size",
    )
    require(sha256_file(bundle) == expected_sha256, "legacy_git_digest_mismatch")
    refs = bundle_refs(bundle)
    protected_work.mkdir(mode=0o700, exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(prefix="git-verify-", dir=protected_work) as temporary:
        repo = Path(temporary) / "verify.git"
        initialized = subprocess.run(
            ["git", "init", "--bare", str(repo)], capture_output=True, env=git_env(), timeout=30
        )
        require(initialized.returncode == 0, "legacy_git_initialization_failed")
        run_git(repo, "bundle", "verify", str(bundle.resolve()))
        run_git(repo, "bundle", "unbundle", str(bundle.resolve()))
        for name, oid in refs.items():
            run_git(repo, "check-ref-format", name)
            run_git(repo, "update-ref", name, oid, "0" * 40)
        run_git(repo, "fsck", "--full", "--strict", "--no-reflogs")
        object_ids = (
            run_git(repo, "rev-list", "--objects", "--all", "--no-object-names")
            .decode()
            .splitlines()
        )
        objects = len(object_ids)
        require(objects <= 100000, "legacy_git_object_limit")
        lfs_pointers = 0
        gitlinks = 0
        for oid in object_ids:
            kind = run_git(repo, "cat-file", "-t", oid).strip()
            if kind == b"blob" and int(run_git(repo, "cat-file", "-s", oid).strip()) <= 4096:
                data = run_git(repo, "cat-file", "blob", oid)
                lfs_pointers += int(
                    data.startswith(b"version https://git-lfs.github.com/spec/v1\n")
                )
            elif kind == b"tree":
                data = run_git(repo, "cat-file", "tree", oid)
                while data:
                    header, data = data.split(b"\0", 1)
                    gitlinks += int(header.startswith(b"160000 "))
                    data = data[20:]

    require(sha256_file(bundle) == expected_sha256, "legacy_git_digest_mismatch")
    return {
        "refs": refs,
        "ref_count": len(refs),
        "reachable_object_count": objects,
        "lfs_pointer_count": lfs_pointers,
        "gitlink_count": gitlinks,
        "bundle_sha256": expected_sha256,
        "bundle_bytes": bundle.stat().st_size,
        "refs_digest": digest(refs),
        "scope": "reachable-heads-and-tags-only",
    }


def git_approval(
    *,
    bundle_sha256,
    refs_digest,
    default_ref,
    service,
    parent,
    parent_generation,
    operator,
    name,
    expires_at,
):
    return {
        "format": PURPOSE,
        "bundle_sha256": bundle_sha256,
        "refs_digest": refs_digest,
        "default_ref": default_ref,
        "target_service": service,
        "parent": parent,
        "parent_generation": parent_generation,
        "operator": operator,
        "name": name,
        "expires_at": expires_at,
        "visibility": "private",
        "legacy_authority": "disabled",
    }


async def import_git(app, bundle: Path, approval: dict, signature: dict):
    verify(
        app.certificates.root_public_key,
        canonical(approval),
        decode(Signature, signature),
        purpose=PURPOSE,
    )
    expected = git_approval(
        **{
            "service": approval.get("target_service"),
            **{
                key: approval.get(key)
                for key in (
                    "bundle_sha256",
                    "refs_digest",
                    "default_ref",
                    "parent",
                    "parent_generation",
                    "operator",
                    "name",
                    "expires_at",
                )
            },
        }
    )
    require(approval == expected, "legacy_git_approval_invalid")
    require(approval["target_service"] == app.settings.service_url, "legacy_git_service_mismatch")
    now = app.clock()
    require(
        now < parse_time(approval["expires_at"]) <= now + timedelta(hours=24),
        "legacy_git_approval_expired",
    )
    require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,90}\.git", approval["name"]) is not None,
        "invalid_repository_name",
    )
    verified = await asyncio.to_thread(
        verify_bundle,
        Path(bundle),
        approval["bundle_sha256"],
        protected_work=app.settings.server.staging_dir,
    )
    require(verified["refs_digest"] == approval["refs_digest"], "legacy_git_refs_mismatch")
    require(
        verified["lfs_pointer_count"] == 0 and verified["gitlink_count"] == 0,
        "legacy_git_external_objects_require_separate_import",
    )
    require(
        approval["default_ref"] in verified["refs"]
        and approval["default_ref"].startswith("refs/heads/"),
        "legacy_git_default_ref_invalid",
    )
    rid = "legacy_git_" + approval["bundle_sha256"][:32]
    ctx = SimpleNamespace(
        now=now,
        principal=Principal(
            actor=approval["operator"],
            subject=approval["operator"],
            credential_id=None,
            method="local",
            certificates=(),
            ceiling=(),
        ),
    )
    request = SimpleNamespace(operation="legacy.git_import", contract_version=1, arguments={})
    store = NativeGitStore(app)
    changes = [
        {"ref": name, "old": None, "new": oid} for name, oid in sorted(verified["refs"].items())
    ]
    async with app.metadata.transaction(write=True) as tx:
        require_live_authority(tx)
        require(tx.setting("runtime_config", {}).get("accept_writes", True), "writes_paused")
        operator = await tx.subject(approval["operator"])
        require(operator.kind == "registered", "legacy_git_operator_invalid")
        parent = await tx.resource(approval["parent"])
        require(
            parent.owner == operator.resource_id
            and parent.mode == 0o700
            and parent.generation == approval["parent_generation"],
            "legacy_git_parent_not_private",
        )
        require(
            tx.setting("legacy-git-import:" + approval["bundle_sha256"]) is None,
            "legacy_git_already_imported",
        )
        require(not store.path(rid).exists(), "repository_directory_exists")
        resource = await create_resource(
            app,
            ctx,
            request,
            tx,
            parent=parent.id,
            type="repo",
            name=approval["name"],
            resource_id=rid,
            mode=0o600,
        )

        # New unique destination only: rollback cannot delete any pre-existing repo.
        async def remove_new_repository():
            if store.path(rid).exists():
                await asyncio.to_thread(shutil.rmtree, store.path(rid))

        tx.on_rollback(remove_new_repository)
        # Never create a public export marker, even transiently before metadata commit.
        await store.create(rid, public_export=False)
        require_git_repository_capacity(store.path(rid), incoming=verified["bundle_bytes"])
        await store.import_bundle(rid, Path(bundle).resolve(), changes)
        await store.update_refs(rid, changes)
        await asyncio.to_thread(store._run, rid, "symbolic-ref", "HEAD", approval["default_ref"])
        await asyncio.to_thread(store._run, rid, "fsck", "--full", "--strict", "--no-reflogs")
        actual, following = await store.refs(rid, limit=129)
        require(
            following is None and {r["name"]: r["oid"] for r in actual} == verified["refs"],
            "legacy_git_refs_mismatch",
        )
        require(sha256_file(bundle) == approval["bundle_sha256"], "legacy_git_digest_mismatch")
        await tx.replace(replace(resource, generation=resource.generation + 1), resource.generation)
        report = {key: value for key, value in verified.items() if key != "refs"}
        report.update(
            {
                "format": PURPOSE,
                "resource_id": resource.id,
                "visibility": "private",
                "ref_mapping": verified["refs"],
                "default_ref": approval["default_ref"],
                "authority_enabled": False,
                "legacy_hooks_loaded": False,
                "legacy_config_loaded": False,
            }
        )
        tx.set_setting("legacy-git-import:" + approval["bundle_sha256"], report)
        tx.set_setting(
            "legacy-git-approval:" + approval["bundle_sha256"],
            {
                "approval": approval,
                "signature": signature,
                "root_public_key": b64(app.certificates.root_public_key),
            },
        )
        return report


def main():
    import argparse
    import json
    from msg.application import Application
    from msg.config import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--signed-approval", type=Path, required=True)
    args = parser.parse_args()

    async def run():
        envelope = json.loads(args.signed_approval.read_text())
        app = Application(load_settings(args.config))
        try:
            await app.load()
            report = await import_git(app, args.bundle, envelope["approval"], envelope["signature"])
            print(
                json.dumps(
                    {
                        key: value
                        for key, value in report.items()
                        if key not in {"ref_mapping", "default_ref"}
                    },
                    sort_keys=True,
                )
            )
        finally:
            await app.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
