import base64
import hashlib
import json
import sqlite3
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_legacy_resource_import import setup, source
from test_service import NOW, call

from msg.core.codec import canonical, wire
from msg.core.errors import Failure
from msg.storage.legacy_identity_plan import (
    PURPOSE,
    _json,
    approval_payload,
    identity_plan,
    import_identity_records,
    summary,
)


def keypair():
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    return key, base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()


def signed_source(tmp_path):
    path = source(tmp_path)
    root, root_public, root_id = keypair()
    key, public, uid = keypair()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("DELETE FROM attachments")
        db.execute(
            "UPDATE posts SET author_id=?,author_key=?,actor_id=?,actor_key=?,signature=NULL,sig_version=1,sig_nonce=?,sig_issued=?",
            (uid, public, uid, public, "a" * 32, int(NOW.timestamp())),
        )
        # Independent fixed wire vector from installed crypto.py request/v1.
        payload = (
            "msg.lmm.best/request/v1\n"
            "action:11:post.create\n"
            f"signer_id:64:{uid}\nversion:1:1\n"
            f"nonce:32:{'a' * 32}\nissued:10:{int(NOW.timestamp())}\n"
            f"owner_id:64:{uid}\nboard:4:main\nname:9:anonymous\n"
            "title:0:\nbody:8:old body\nreply_to:0:\nfiles:2:[]\n"
        ).encode()
        signature = base64.b64encode(key.sign(payload)).decode()
        db.execute("UPDATE posts SET signature=?", (signature,))
        body = {
            "v": 1,
            "serial": "b" * 32,
            "issuer_serial": "root",
            "issuer_id": root_id,
            "subject_key": public,
            "subject_id": uid,
            "not_before": int(NOW.timestamp()) - 10,
            "not_after": int(NOW.timestamp()) + 100,
            "delegate": False,
            "grants": [{"topic": "*", "actions": ["post.create"]}],
        }
        signature = base64.b64encode(
            root.sign(b"msg.lmm.best/cert/v1\n" + _json(body).encode())
        ).decode()
        db.execute(
            "INSERT INTO certificates VALUES (?,?,?,?,?,?,?,?)",
            (body["serial"], "root", root_id, uid, public, _json(body), signature, 1),
        )
    return path, root_public, uid


def plan(path, root=None):
    return identity_plan(
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        at=int(NOW.timestamp()),
        root_public_key=root,
    )


def test_verifies_public_signatures_and_never_implies_authority(tmp_path):
    path, root, uid = signed_source(tmp_path)
    result = plan(path, root)
    assert result["identities"][0]["post_signatures"] == {"verified": 1}
    assert result["certificates"][0]["status"] == "signature_chain_verified"
    assert result["authority_enabled"] is False
    assert result["private_ciphertext_imported"] is False
    assert uid not in json.dumps(summary(result))
    assert plan(path)["certificates"][0]["status"] == "root_anchor_unknown"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE posts SET body=?", ("tampered",))
        db.execute("INSERT INTO revocations VALUES (?,?,?,?)", ("b" * 32, 1, "root", "test"))
    result = plan(path, root)
    assert result["identities"][0]["post_signatures"] == {"invalid": 1}
    assert result["certificates"][0]["status"] == "revoked"


def test_snapshot_change_and_expiry(tmp_path):
    path, root, _ = signed_source(tmp_path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    result = identity_plan(path, sha, at=int(NOW.timestamp()) + 101, root_public_key=root)
    assert result["certificates"][0]["status"] == "expired"
    with pytest.raises(Failure, match="legacy_snapshot_changed"):
        identity_plan(path, "0" * 64, at=0)


@pytest.mark.asyncio
async def test_root_archive_creates_only_private_nonlogin_records(installed, tmp_path):
    app, root, snapshot, old_approval, _key, _cert = await setup(installed, tmp_path)
    result = plan(snapshot)
    signed = approval_payload(
        plan_digest=summary(result)["plan_digest"],
        service=app.settings.service_url,
        parent=old_approval["parent"],
        parent_generation=old_approval["parent_generation"],
        operator=old_approval["operator"],
        mappings={r["old_id"]: None for r in result["identities"]},
        expires_at=wire(NOW + timedelta(hours=1)),
    )
    async with app.metadata.transaction(write=False) as tx:
        before = {
            t: tx.one("SELECT count(*) FROM " + t)[0]
            for t in ("identities", "credentials", "certificates", "identity_keys", "jobs")
        }
    envelope = wire(root.sign(canonical(signed), purpose=PURPOSE))
    changed = {**signed, "mappings": {}}
    with pytest.raises(Failure):
        await import_identity_records(app, result, changed, envelope)
    report = await import_identity_records(app, result, signed, envelope)
    assert report["authority_enabled"] is False
    async with app.metadata.transaction(write=False) as tx:
        assert before == {t: tx.one("SELECT count(*) FROM " + t)[0] for t in before}
        rows = tx.execute("SELECT id FROM resources WHERE parent=?", (signed["parent"],))
        ids = [row[0] for row in rows]
        for resource_id in ids:
            assert (await tx.resource(resource_id)).mode == 0o600
    for resource_id in ids:
        response = await call(app, "discovery.get", {"id": resource_id})
        assert response.status == "error"
    with pytest.raises(Failure):
        await import_identity_records(app, result, signed, envelope)


def test_bad_certificate_signature_and_key_mismatch_fail_closed(tmp_path):
    path, root, _ = signed_source(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE certificates SET signature=?", (base64.b64encode(b"x" * 64).decode(),))
        db.execute("UPDATE posts SET author_id=?", ("0" * 64,))
    result = plan(path, root)
    assert result["certificates"][0]["status"] == "invalid_signature"
    assert any(r["classification"] == "key_conflict" for r in result["identities"])
    assert result["authority_enabled"] is False


def test_cli_exclusive_private_plan_and_summary_only(tmp_path, monkeypatch, capsys):
    from msg.storage.legacy_identity_plan import main

    path, _, uid = signed_source(tmp_path)
    target = tmp_path / "private-plan.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "plan",
            "--snapshot",
            str(path),
            "--sha256",
            hashlib.sha256(path.read_bytes()).hexdigest(),
            "--output",
            str(target),
            "--at",
            str(int(NOW.timestamp())),
        ],
    )
    main()
    assert target.stat().st_mode & 0o777 == 0o600
    assert uid not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        main()
