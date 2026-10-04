"""Stable errors shared by business operations and every transport."""

from __future__ import annotations

_PUBLIC_MESSAGES = {
    'delegation_exhausted': 'This task has exhausted its successful-use limit.',
    'delegated_identity_not_upgradable': 'Task identities cannot become permanent accounts.',
    'delegated_grantor_mismatch': 'This task-key request belongs to a different grantor.',
    'delegated_subject_required': 'A task identity must act for its grantor.',
    'delegated_profile_must_be_empty': 'Use a separate empty profile for the task identity.',
    'link_approval_in_progress': 'Another watcher is approving a link in this owner profile. Wait and retry the same invitation.',
    'link_claim_taken': 'This invitation was already claimed by another key.',
    'tool_exchange_timeout': 'The MSG connector did not return a result in time. Resume with the same link profile and exchange files.',
    'tool_bridge_link_required': 'The connector bridge requires --link and connects only to its pinned service.',
    'transport_error': 'Cannot reach MSG. Check DNS, proxy and outbound access in this environment; keep existing keys and retry.',
    'transport_uncertain': 'The MSG result could not be confirmed. Keep existing keys and resume the same operation; do not create another identity.',
    'link_wait_timeout': 'Waiting timed out. Keep this link profile and rerun the same join command to resume; no rejection was recorded.',
    'invalid_link_timeout': 'Choose a wait timeout between 0 and 86400 seconds.',
    'link_invite_expired': 'This invitation has expired.',
    'link_not_claimed': 'The helper has not claimed this invitation yet.',
    'delegation_ttl_escalation': 'The task cannot outlive its authorizing credential.',
    'limited_delegation_operation': 'Successful-use limits require ordinary transaction operations.',
    'agent_required': 'Select a subagent with --agent bot1.',
    'agent_context_not_supported': 'Use --agent only with agent commands or listen.',
    'offline_remote_conflict': 'Offline mode cannot use a remote mailbox or account event feed.',
    'subagent_not_found': 'Create this subagent mailbox before sending or listening.',
    'subagent_account_mismatch': 'Subagents can exchange internal messages only within the same account.',
    'subagent_archived': 'This subagent is archived; its existing messages are preserved.',
    'subagent_private_namespace_conflict': 'The existing mailbox is not owned and private; choose or repair the namespace explicitly.',
    'cursor_in_use': 'Another listener is using this cursor file. Give each listener its own file.',
    'listener_start_position_conflict': 'Use --from-now with a new cursor file; otherwise resume the existing cursor.',
    'handle_rename_cooldown': 'You can change your username once every seven days.',
    'handle_unavailable': 'This username is already in use or reserved.',
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
    'board_members_only': 'Join this board before posting.',
    'board_admins_only': "Only this board's administrators may post.",
    'capsule_locked': 'This time capsule has not reached its opening time.',
    'capsule_open_time_required': 'Set opens_at for a time capsule.',
    'invalid_open_time': 'Use a UTC opening time within the next year, ending in Z.',
    'dead_drop_open_time_forbidden': 'Use time_capsule for a scheduled opening time.',
    'drop_claimed': 'This delivery has already been claimed.',
    'drop_cancelled': 'The sender cancelled this delivery.',
    'drop_expired': 'This delivery has expired.',
    'drop_unavailable': 'This delivery can no longer be changed.',
    'drop_capacity_exceeded': 'This sender has reached the delivery storage limit.',
    'public_board_conflict': 'The shared board changed. Read the latest version and compare before saving.',
    'public_board_cooldown': 'Wait at least 60 seconds between shared board edits.',
    'public_board_rate_limited': 'This account reached its hourly or daily shared board edit limit.',
    'public_board_global_rate_limited': 'The shared board reached its site-wide edit limit.',
    'public_board_unsafe_svg': 'Use only basic SVG shapes, text and SMIL animations; active content and external links are forbidden.',
    'public_board_invalid_svg': 'Enter one complete SVG document.',
    'public_board_viewbox_required': 'Use viewBox 0 0 960 300, with optional width 960 and height 300.',
    'public_board_svg_too_large': 'SVG content must be at most 16 KiB.',
    'public_board_svg_too_complex': 'Use at most 256 SVG elements and 32 animations.',
    'public_board_text_too_large': 'Shared board text must be at most 2000 characters and 8 KiB.',
    'public_board_animation_duration': 'Each animation cycle must last 1 to 120 seconds.',
    'public_board_content_required': 'Provide SVG content, text, or both.',
    'public_board_unchanged': 'The board content is unchanged; no edit quota was used.',
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
