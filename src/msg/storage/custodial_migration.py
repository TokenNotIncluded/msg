"""One idempotent PostgreSQL upgrade for the decrypt-only vault lifecycle."""
from msg.core.errors import require

LEGACY_STATUS = "CHECK ((status = ANY (ARRAY['active'::text, 'destroyed'::text])))"
LIFECYCLE_CHECK = """CHECK (
    status IN ('active','decrypt_only','destroyed') AND
    (status <> 'decrypt_only' OR (signing_nonce IS NULL AND signing_ciphertext IS NULL
        AND age_nonce IS NOT NULL AND age_ciphertext IS NOT NULL)))"""


def migrate_custodial_vault(connection):
    """Caller holds the schema advisory lock; neither keys nor rows are rewritten."""
    old = connection.execute("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid='custodial_vault'::regclass AND conname='custodial_vault_status_check'""").fetchone()
    if old is not None:
        require(old[0] == LEGACY_STATUS, 'custodial_vault_unknown_constraint')
        connection.execute('ALTER TABLE custodial_vault DROP CONSTRAINT custodial_vault_status_check')
    present = connection.execute("""SELECT 1 FROM pg_constraint WHERE conrelid='custodial_vault'::regclass
        AND conname='custodial_vault_lifecycle_check'""").fetchone()
    if present is None:
        connection.execute('ALTER TABLE custodial_vault ADD CONSTRAINT custodial_vault_lifecycle_check '
                           + LIFECYCLE_CHECK)
