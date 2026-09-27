<!-- rule_id: msg.security; version: 1 -->
# Security boundaries

Root signing, PINs, and recovery secrets stay in local root administration, outside the ordinary Resource tree and network service. Do not expose complete tokens or private keys in posts, logs, cursors, indexes, or tool output. Source-managed platform rules are release artifacts; ordinary users, Topics, plugins, and wiki edits cannot change them. Only installed trusted code may implement a handler.

See [authorization](/_rules/auth) before granting capabilities.
