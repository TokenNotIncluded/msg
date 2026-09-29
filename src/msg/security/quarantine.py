"""A restored snapshot is not current authority until a local recovery is proven.

The gate is a database fact, not a deletable config marker. No network operation
may clear it. Absence alone means an ordinary installation; malformed, false or
unknown values fail closed. This is containment, not a revocation replay proof.
"""
from msg.core.errors import Failure, require

SETTING = 'recovery_quarantine'


def active(session):
    return session.one('SELECT value FROM settings WHERE key=?', (SETTING,)) is not None


def require_live_authority(session):
    # Even anonymous access to formerly public content may revive an ACL that
    # was revoked after the snapshot. Only the transport health probe remains.
    require(not active(session), 'recovery_quarantined')


RUNTIME_GENERATION = 'recovery_runtime_generation'


def _generation(session):
    missing = object()
    value = session.setting(RUNTIME_GENERATION, missing)
    if value is missing:
        return None
    require(isinstance(value, str) and bool(value), 'recovery_runtime_stale')
    return value


class RuntimeGeneration:
    """Pin authority generation at startup; mismatch stays latched until restart."""
    def __init__(self, generation=None):
        self.generation = generation
        self.stale = False

    @classmethod
    def capture(cls, session):
        return cls(_generation(session))

    def require_current(self, session):
        try:
            current = _generation(session)
        except Failure:
            self.stale = True
            raise
        if current != self.generation:
            self.stale = True
        require(not self.stale, 'recovery_runtime_stale')
