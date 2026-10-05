"""Private external-login facts; never a content or discovery collection."""

LOGIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS login_bindings (
 id TEXT PRIMARY KEY,
 provider TEXT NOT NULL,
 external_subject_hash TEXT NOT NULL,
 subject TEXT NOT NULL REFERENCES identities(id),
 source_credential_id TEXT NOT NULL REFERENCES credentials(id),
 auth_version INTEGER NOT NULL,
 custodial INTEGER NOT NULL CHECK (custodial IN (0,1)),
 ceiling TEXT NOT NULL,
 source_data TEXT NOT NULL,
 provider_data TEXT NOT NULL DEFAULT '{}',
 generation INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 revoked_at TEXT,
 UNIQUE(provider,external_subject_hash));
CREATE INDEX IF NOT EXISTS login_bindings_subject ON login_bindings(subject,id);
"""
