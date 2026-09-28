<!-- rule_id: msg.security; version: 3 -->
# Security boundaries

Root signing, PINs, and recovery secrets stay in local root administration, outside the ordinary Resource tree and network service. Do not expose complete tokens or private keys in posts, logs, cursors, indexes, or tool output. Source-managed platform rules are release artifacts; ordinary users, Topics, plugins, and wiki edits cannot change them. Only installed trusted code may implement a handler.

Order emails are notifications, not ownership or acceptance. Pickup links must contain no credentials and still require buyer authentication. SMTP status comes from the delivery job, not a second order state machine. Disabled endpoints, failed jobs and uncertain attempts are not revived by reads; buyer acceptance is a separate signed operation.

A delivery endpoint is one exact mailbox, not a display name, address group or recipient list. Reject ambiguous parser recovery before binding it to an order, and revalidate queued targets before SMTP. The SMTP envelope must explicitly contain that single verified mailbox; never infer recipients from message headers. Mail failures must not change site delivery, payment or acceptance.

See [authorization](/_rules/auth) before granting capabilities.
