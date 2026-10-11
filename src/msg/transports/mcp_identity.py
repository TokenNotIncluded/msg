"""Trusted adapter-local identity context; never populated from tool arguments."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MCPIdentity:
    # A transport-only value. Never pass this dataclass to wire(), JSON or logs.
    subject: str
    credential_id: str = field(repr=False)
    token: bytes = field(repr=False)
    operations: frozenset[str]
    expires_at: datetime
    session_binding: str = field(repr=False)
    credential_type: str = 'key-grant'
