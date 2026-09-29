"""Deployment-wide durability caps for tables that otherwise grow with fresh identities.

Personal entitlements stay elsewhere. These checks only stop one shared database
or content volume from being filled by unbounded metadata, staged bytes, or
ciphertext. They do not grant quota, delete user content, or clear quarantine.
"""

from __future__ import annotations

from msg.core.codec import decode, loads
from msg.core.errors import Failure, require
from msg.core.models import Revision

MAX_TRANSFERS = 4096
MAX_STAGED_CHUNK_BYTES = 4 * 1024 * 1024 * 1024
MAX_PURGE_RECORDS = 4096
MAX_PENDING_CSRS = 1024
MAX_CSR_ROWS = 8192
MAX_QUEUED_WEBHOOKS = 1024
MAX_WEBHOOK_ROWS = 8192
MAX_RESULTS = 100_000
MAX_REACTIONS = 100_000
MAX_KEYSTORE_REVISIONS = 4096
MAX_KEYSTORE_BYTES = 64 * 1024 * 1024
_CAPACITY = 'storage_capacity_exceeded'


def _int(value):
    if type(value) is bool:
        raise Failure(_CAPACITY)
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise Failure(_CAPACITY) from exc
    require(number >= 0 and number == value, _CAPACITY)
    return number


def _count(tx, sql, args=()):
    return _int(tx.one(sql, args)[0])


def require_transfer_capacity(tx, extra_bytes=0, *, new_transfer=False):
    require(type(extra_bytes) is int and extra_bytes >= 0, _CAPACITY)
    if new_transfer:
        require(_count(tx, 'SELECT COUNT(*) FROM transfers') < MAX_TRANSFERS, _CAPACITY)
    staged = _count(tx, 'SELECT COALESCE(SUM(length), 0) FROM chunks')
    require(staged + extra_bytes <= MAX_STAGED_CHUNK_BYTES, _CAPACITY)


def trim_purge_records(tx):
    rows = tx.rows("SELECT key, value FROM settings WHERE key LIKE 'purge_record:%'")
    if len(rows) <= MAX_PURGE_RECORDS:
        return

    def stamp(raw):
        value = loads(raw)
        require(isinstance(value, dict), _CAPACITY)
        return str(value.get('time') or '')

    ranked = sorted(rows, key=lambda row: (stamp(row[1]), row[0]))
    for key, _value in ranked[: len(ranked) - MAX_PURGE_RECORDS]:
        tx.execute('DELETE FROM settings WHERE key=?', (key,), write=True)


def require_csr_capacity(tx):
    require(
        _count(tx, "SELECT COUNT(*) FROM csrs WHERE state='pending'") < MAX_PENDING_CSRS, _CAPACITY
    )
    require(_count(tx, 'SELECT COUNT(*) FROM csrs') < MAX_CSR_ROWS, _CAPACITY)


def require_webhook_capacity(tx):
    active = _count(
        tx, "SELECT COUNT(*) FROM jobs WHERE kind='webhook' AND state IN ('pending','running')"
    )
    require(active < MAX_QUEUED_WEBHOOKS, _CAPACITY)
    total = _count(tx, "SELECT COUNT(*) FROM jobs WHERE kind='webhook'")
    if total < MAX_WEBHOOK_ROWS:
        return
    overflow = total - MAX_WEBHOOK_ROWS + 1
    finished = tx.rows(
        "SELECT id FROM jobs WHERE kind='webhook' AND state IN ('done','failed') ORDER BY next_at, id LIMIT ?",
        (overflow,),
    )
    for (job_id,) in finished:
        tx.execute(
            "DELETE FROM jobs WHERE id=? AND kind='webhook' AND state IN ('done','failed')",
            (job_id,),
            write=True,
        )
    require(
        _count(tx, "SELECT COUNT(*) FROM jobs WHERE kind='webhook'") < MAX_WEBHOOK_ROWS, _CAPACITY
    )


def require_result_capacity(tx):
    require(_count(tx, 'SELECT COUNT(*) FROM results') < MAX_RESULTS, _CAPACITY)


def require_reaction_capacity(tx):
    require(_count(tx, 'SELECT COUNT(*) FROM reactions') < MAX_REACTIONS, _CAPACITY)


def require_keystore_capacity(tx, extra_bytes):
    require(type(extra_bytes) is int and extra_bytes >= 0, _CAPACITY)
    require(
        _count(
            tx,
            """SELECT COUNT(*) FROM revisions r JOIN resources s
        ON s.id=r.resource_id WHERE s.type='keystore'""",
        )
        < MAX_KEYSTORE_REVISIONS,
        _CAPACITY,
    )
    total = 0
    for (raw,) in tx.execute("""SELECT r.body FROM revisions r JOIN resources s
            ON s.id=r.resource_id WHERE s.type='keystore'"""):
        total += _int(decode(Revision, loads(raw)).content.size)
        require(total + extra_bytes <= MAX_KEYSTORE_BYTES, _CAPACITY)
