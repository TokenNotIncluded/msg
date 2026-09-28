<!-- rule_id: msg.security; version: 3 -->
# Security boundaries

Root keys, PINs and recovery secrets stay in local administration, outside network resources. Never expose full tokens or private keys in posts, logs, cursors, indexes or tool output. Only installed trusted code supplies handlers; users, Topics, plugins and wiki cannot change release-managed rules.

Order mail only notifies. Credential-free pickup links require buyer authentication. Mail state comes from EffectJob; reads never revive disabled, failed or uncertain jobs. Acceptance requires a separate signed operation.

An endpoint is one exact mailbox, not a list, group, display name, comment or repaired address. Validate at order binding and before SMTP, including legacy jobs. Explicitly pin that sole envelope recipient; never infer recipients from headers. Mail failure cannot change site delivery, payment or acceptance.

See [authorization](/_rules/auth).
