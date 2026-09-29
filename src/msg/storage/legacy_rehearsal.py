"""TEST ONLY: isolated local PostgreSQL rehearsal; never supplies production approval."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
import pwd
from pathlib import Path
import subprocess
from urllib.parse import quote

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer, subject_id
from msg.storage.legacy_resource_import import (
    PURPOSE,
    approval_payload,
    identity_requirements,
    import_content,
)
from msg.storage.legacy_sqlite import preserve


def _write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


async def _run(source, sha256, target, dsn):
    from dataclasses import replace
    from msg.admin.root import _provision, _approve_csr
    from msg.application import Application
    from msg.config import write_example

    before = preserve(source, expected_sha256=sha256)
    mapping_report = identity_requirements(source, sha256, {}, summary_only=False)
    settings = write_example(
        target / "etc",
        target / "data",
        "https://test-only-legacy-rehearsal.invalid",
        postgres_dsn=dsn,
    )
    app = Application(settings)
    try:
        csr, root = await _provision(app, "test-only-disposable-rehearsal-passphrase")
        await _approve_csr(app, csr, root, expected_digest=None, operator="test-only-rehearsal")
        signer = Ed25519Signer.generate()
        uid = subject_id(signer.public_key)
        _, recipient = generate_age_key()
        registered = await app.executor.execute(
            request_for(
                "identity.register",
                {
                    "handle": "test-only-importer",
                    "public_key": b64(signer.public_key),
                    "encryption_recipient": recipient,
                },
                settings.service_url,
                signer=signer,
                subject=uid,
                contract_version=2,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        if registered.status != "ok":
            raise RuntimeError("test-only registration failed")
        created = await app.executor.execute(
            request_for(
                "content.topic_create",
                {"parent": "/main", "name": "test-only-legacy-import"},
                settings.service_url,
                signer=signer,
                subject=uid,
                certificates=(registered.data["certificate_id"],),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        if created.status != "ok":
            raise RuntimeError("test-only namespace creation failed")
        parent = created.resources[0].id
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(parent)
            await tx.replace(
                replace(resource, mode=0o700, generation=resource.generation + 1),
                resource.generation,
            )
            generation = resource.generation + 1
            baseline = {
                table: tx.one("SELECT count(*) FROM " + table)[0]
                for table in ("credentials", "certificates", "jobs")
            }
        identities = {old: uid for old in mapping_report["required"]}
        approval = approval_payload(
            sha256=sha256,
            service=settings.service_url,
            parent=parent,
            parent_generation=generation,
            operator=uid,
            identities=identities,
            expires_at=wire(datetime.now(UTC) + timedelta(hours=2)),
        )
        # This local Root is freshly generated for this disposable installation.
        # Its signature is never reusable against the production pinned Root/service.
        report = await import_content(
            app, source, approval, wire(root.sign(canonical(approval), purpose=PURPOSE))
        )
        async with app.metadata.transaction(write=False) as tx:
            final = {table: tx.one("SELECT count(*) FROM " + table)[0] for table in baseline}
        after = preserve(source, expected_sha256=sha256)
        summary = {
            "format": "msg-test-only-legacy-rehearsal-v1",
            "test_only": True,
            "production_approval": False,
            "source_sha256": sha256,
            "source_unchanged": before == after,
            "source_tables": len(before["tables"]),
            "source_rows": sum(item["count"] for item in before["tables"].values()),
            "identity_mapping_required": len(mapping_report["required"]),
            "identity_mapping_missing": 0,
            "test_only_identity_targets": 1,
            "boards": report["boards"],
            "posts": report["posts"],
            "attachments": report["attachments"],
            "url_mapping_count": len(report["url_map"]),
            "authority_and_jobs_unchanged": baseline == final,
            "visibility": "private",
            "result": "passed",
        }
        if not summary["source_unchanged"] or not summary["authority_and_jobs_unchanged"]:
            raise RuntimeError("rehearsal invariant failed")
        return summary
    finally:
        await app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--protected-target", type=Path, required=True)
    args = parser.parse_args()
    source = args.snapshot.resolve()
    target = args.protected_target.absolute()
    if target.is_relative_to("/tmp") or target.is_relative_to("/var/tmp"):
        parser.error("target must be protected persistent storage, not a temporary directory")
    if args.snapshot.is_symlink() or source.stat().st_mode & 0o077:
        parser.error("source must be a non-symlink private snapshot (0600 or stricter)")
    if target.parent.resolve() != target.parent or target.parent.stat().st_mode & 0o077:
        parser.error("target parent must be a private real directory (0700 or stricter)")
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    # Socket contains no backup payload. Put it under the same protected directory.
    socket = target / "sock"
    socket.mkdir(mode=0o700)
    if len(str(socket).encode()) > 85:
        parser.error("protected-target path is too long for a local PostgreSQL socket")
    cluster = target / "pg"
    started = False
    summary = None
    try:
        subprocess.run(
            ["initdb", "-D", str(cluster), "-A", "trust", "--no-instructions"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "pg_ctl",
                "-D",
                str(cluster),
                "-l",
                str(target / "postgres.log"),
                "-o",
                f'-k {socket} -h "" -p 5432',
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
        )
        started = True
        import psycopg

        dsn = f"postgresql://{quote(pwd.getpwuid(os.getuid()).pw_name)}@localhost:5432/postgres?host={quote(str(socket), safe='')}"
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("CREATE DATABASE legacy_rehearsal")
        dsn = dsn.replace("/postgres?", "/legacy_rehearsal?")
        summary = asyncio.run(_run(source, args.sha256, target, dsn))
    except BaseException as exc:
        # No raw exception messages: DB errors can include original row contents.
        from msg.core.errors import Failure

        summary = {
            "format": "msg-test-only-legacy-rehearsal-v1",
            "test_only": True,
            "production_approval": False,
            "result": "failed",
            "error_type": type(exc).__name__,
        }
        if isinstance(exc, Failure):
            summary["error_code"] = exc.code
    finally:
        if started:
            stopped = subprocess.run(
                ["pg_ctl", "-D", str(cluster), "-m", "fast", "-w", "stop"],
                capture_output=True,
                check=False,
            )
            if summary is not None:
                summary["cluster_stopped"] = stopped.returncode == 0
                if stopped.returncode != 0:
                    summary["result"] = "failed"
        if summary is not None:
            _write(target / "summary.json", summary)
            print(json.dumps(summary, sort_keys=True))
    if summary is None or summary["result"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
