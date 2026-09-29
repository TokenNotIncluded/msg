from datetime import timedelta
import subprocess

import pytest

from msg.core.codec import canonical, wire
from msg.core.errors import Failure
from msg.extensions.repositories import NativeGitStore
from msg.storage.legacy_git_import import (
    PURPOSE,
    git_approval,
    git_env,
    import_git,
    sha256_file,
    verify_bundle,
)
from test_legacy_resource_import import setup
from test_service import NOW, call


def make_bundle(tmp_path, *, lfs=False):
    source = tmp_path / "source-repo"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(source)],
        check=True,
        capture_output=True,
        env=git_env(),
    )
    (source / "file.txt").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 1\n"
        if lfs
        else "synthetic content\n"
    )

    def git(*args):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=Synthetic",
                "-c",
                "user.email=synthetic@example.invalid",
                *args,
            ],
            capture_output=True,
            env=git_env(),
        )
        assert result.returncode == 0
        return result.stdout

    git("add", "file.txt")
    git("commit", "-m", "Synthetic fixture")
    git("config", "legacy.shouldNeverCopy", "unsafe-source-config")
    (source / ".git/hooks/post-checkout").write_text(
        "#!/bin/sh\ntouch " + str(tmp_path / "hook-executed") + "\n"
    )
    (source / ".git/hooks/post-checkout").chmod(0o700)
    bundle = tmp_path / "source.bundle"
    git("bundle", "create", str(bundle), "refs/heads/main")
    return bundle


async def prepared(installed, tmp_path, *, lfs=False):
    app, root, _, base, key, cert = await setup(installed, tmp_path)
    bundle = make_bundle(tmp_path, lfs=lfs)
    verified = verify_bundle(bundle, sha256_file(bundle), protected_work=tmp_path / "verify")
    approval = git_approval(
        bundle_sha256=verified["bundle_sha256"],
        refs_digest=verified["refs_digest"],
        default_ref="refs/heads/main",
        service=app.settings.service_url,
        parent=base["parent"],
        parent_generation=base["parent_generation"],
        operator=base["operator"],
        name="retained.git",
        expires_at=wire(NOW + timedelta(hours=1)),
    )
    return app, root, key, bundle, approval, verified


@pytest.mark.asyncio
async def test_git_bundle_import_preserves_oids_and_private_authority(installed, tmp_path):
    app, root, key, bundle, approval, verified = await prepared(installed, tmp_path)
    before = sha256_file(bundle)
    report = await import_git(
        app, bundle, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
    )
    assert report["reachable_object_count"] == 3
    assert report["ref_mapping"] == verified["refs"]
    store = NativeGitStore(app)
    assert not (store.path(report["resource_id"]) / "git-daemon-export-ok").exists()
    assert store._run(report["resource_id"], "symbolic-ref", "HEAD").strip() == b"refs/heads/main"
    assert (
        b"legacy.shouldnevercopy"
        not in store._run(report["resource_id"], "config", "--list").lower()
    )
    assert store._run(report["resource_id"], "config", "core.hooksPath").strip() == b"/dev/null"
    assert not (tmp_path / "hook-executed").exists()
    own = await call(
        app, "git.refs", {"id": report["resource_id"]}, key=key, subject=approval["operator"]
    )
    assert own.status == "ok", own
    assert {row["name"]: row["oid"] for row in own.data["refs"]} == verified["refs"]
    denied = await call(app, "git.refs", {"id": report["resource_id"]})
    assert denied.status == "error"
    assert sha256_file(bundle) == before
    with pytest.raises(Failure):
        await import_git(
            app, bundle, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
        )


@pytest.mark.asyncio
async def test_git_publication_failure_removes_only_new_repository(
    installed, tmp_path, monkeypatch
):
    app, root, key, bundle, approval, verified = await prepared(installed, tmp_path)
    original = NativeGitStore.update_refs

    async def fail_after_refs(self, id, changes):
        await original(self, id, changes)
        raise RuntimeError("injected ref publication failure")

    monkeypatch.setattr(NativeGitStore, "update_refs", fail_after_refs)
    with pytest.raises(RuntimeError, match="injected"):
        await import_git(
            app, bundle, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
        )
    rid = "legacy_git_" + approval["bundle_sha256"][:32]
    assert not NativeGitStore(app).path(rid).exists()
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT id FROM resources WHERE id=?", (rid,)) is None
        assert tx.setting("legacy-git-import:" + approval["bundle_sha256"]) is None


@pytest.mark.asyncio
async def test_git_ref_approval_and_external_lfs_fail_closed(installed, tmp_path):
    app, root, key, bundle, approval, verified = await prepared(installed, tmp_path, lfs=True)
    assert verified["lfs_pointer_count"] == 1
    with pytest.raises(Failure, match="legacy_git_external_objects_require_separate_import"):
        await import_git(
            app, bundle, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
        )
    signature = wire(root.sign(canonical(approval), purpose=PURPOSE))
    approval["name"] = "another.git"
    with pytest.raises(Failure):
        await import_git(app, bundle, approval, signature)
