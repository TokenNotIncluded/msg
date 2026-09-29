"""Stable errors shared by business operations and every transport."""

from __future__ import annotations

_PUBLIC_MESSAGES = {
    'authentication_required': 'Authentication is required.',
    'permission_denied': 'You do not have permission to perform this operation.',
    'capability_required': 'The required permission has not been granted.',
    'not_found': 'The requested resource was not found.',
    'invalid_request_envelope': 'The request format is invalid.',
    'invalid_email': 'Enter one valid email address.',
    'generation_conflict': 'The resource changed. Read it again before retrying.',
    'request_expired': 'The request has expired.',
    'secure_channel_required': 'Use an authenticated secure request channel.',
    'effect_mismatch': 'This route does not allow that operation.',
    'recovery_quarantined': 'The service is isolated pending recovery verification.',
    'recovery_runtime_stale': 'The service must restart after recovery.',
    'service_restart_required': 'The service must restart before accepting requests.',
    'internal_error': 'The service could not complete the operation.',
}


def public_error_message(code: str) -> str:
    """Fixed contract text only: never interpolate input or exception details."""
    return (
        _PUBLIC_MESSAGES.get(code, 'The operation could not be completed.')
        if isinstance(code, str)
        else 'The operation could not be completed.'
    )


class Failure(ValueError):
    def __init__(
        self,
        code: str,
        field: str | None = None,
        *,
        retryable: bool = False,
        details: dict | None = None,
    ):
        super().__init__(code)
        self.code, self.field, self.retryable, self.details = code, field, retryable, details

    def as_dict(self) -> dict:
        value = {
            'code': self.code,
            'retryable': self.retryable,
            'message': public_error_message(self.code),
        }
        if self.field:
            value['field_path'] = self.field
        if self.details:
            value['details'] = self.details
        return value


def require(condition: bool, code: str, field: str | None = None, **kwargs) -> None:
    if not condition:
        raise Failure(code, field, **kwargs)
