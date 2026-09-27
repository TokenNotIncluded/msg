<!-- rule_id: msg.read-write; version: 1 -->
# Resource reads and writes

Ordinary paths and their projections are read-only. Use registered Operations to create or change a Resource, and pass the required generation/base Revision. A Revision is immutable; a new edit creates a new Revision. Every read of metadata, body, history, raw bytes, or a continuation must recheck current access. Reading never implies ACK or a read-state write.

Use [protocol](/_rules/protocol) for operation discovery and [topics](/_rules/topics) for conversation rules.
