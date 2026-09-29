"""Local import of a root-signed custodial backup retirement record.

Never imported by a network adapter, plugin or worker. RootAdmin calls these use
cases only after require_local_console(); tests call them against disposable
databases. The record is verified again on every read, so this import is not a
trusted cache of a success flag.
"""
from __future__ import annotations

from msg.constants import ROOT_SUBJECT
import os
from pathlib import Path
import tempfile

from msg.core.codec import canonical, digest, loads
from msg.core.errors import Failure, require
from msg.core.models import AuditEvent, Event, ResourceRef
from msg.plugins.common import new_id
from msg.security.backup_retirement import FIELDS, DOMAIN, check, root_verifier, setting_key


def read_statement(path):
    from msg.security.root_files import read_private
    return loads(read_private(path, limit=1024 * 1024))


def write_record(path, record):
    """Publish a complete 0600 record without replacing an existing destination."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.retirement-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(record))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise Failure('backup_retirement_destination_exists') from exc
        os.unlink(temporary)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def unsigned_statement(value):
    require(isinstance(value, dict) and set(value) == FIELDS and value.get('domain') == DOMAIN,
            'backup_retirement_invalid')
    return value


def completed_migration(tx, subject_id, old_identity_key_id):
    """The one completed migration of this subject that retired exactly this old key."""
    matches = [loads(body) for (body,) in tx.rows(
        "SELECT body FROM custodial_upgrades WHERE subject=? AND status='completed' ORDER BY id",
        (subject_id,)) if loads(body).get('old_identity_key_id') == old_identity_key_id]
    require(len(matches) == 1, 'backup_retirement_migration_not_found')
    return matches[0]


async def import_record(tx, record, *, now, operator):
    body = record.get('statement') if isinstance(record, dict) else None
    require(isinstance(body, dict) and isinstance(body.get('subject_id'), str) and
            isinstance(body.get('old_identity_key_id'), str), 'backup_retirement_invalid')
    subject = body['subject_id']
    details = completed_migration(tx, subject, body['old_identity_key_id'])
    attestation = check(record, await root_verifier(tx, now=now), subject, details, now=now)
    key = setting_key(subject, attestation.old_identity_key_id)
    previous = tx.setting(key)
    tx.set_setting(key, record)
    data = {'subject_id': subject, 'old_identity_key_id': attestation.old_identity_key_id,
            'old_encryption_key_id': attestation.old_encryption_key_id,
            'backup_sets': list(attestation.backup_sets), 'record_digest': attestation.record_digest,
            'signer_key_id': attestation.signer_key_id, 'operator': operator,
            'scope': 'listed_backup_sets_only'}
    event = Event(id=new_id('audit'), type='root.custodial.backup_retirement', time=now,
        request_id=new_id('local'), actor=ROOT_SUBJECT, subject=subject,
        resources=(ResourceRef(id=subject),), data=data)
    await tx.append_audit(AuditEvent(event=event,
        authority=(ResourceRef(id=tx.setting('active_root_certificate')),),
        before_digest=digest(previous) if previous is not None else None,
        after_digest=attestation.record_digest, previous_digest=None, entry_digest='',
        result='imported'))
    return attestation
