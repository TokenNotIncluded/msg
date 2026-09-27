"""A restored snapshot is not current authority until a local recovery is proven.

The gate is a database fact, not a deletable config marker. No network operation
may clear it. Absence alone means an ordinary installation; malformed, false or
unknown values fail closed. This is containment, not a revocation replay proof.
"""
from msg.core.errors import require

SETTING = 'recovery_quarantine'


def active(session):
    return session.one('SELECT value FROM settings WHERE key=?', (SETTING,)) is not None


def require_live_authority(session):
    # Even anonymous access to formerly public content may revive an ACL that
    # was revoked after the snapshot. Only the transport health probe remains.
    require(not active(session), 'recovery_quarantined')
