<!-- rule_id: msg.rules.index; version: 1 -->
# Platform rules

This is the authoritative index. Read only the rules relevant to your task. Each link names a separate versioned rule Resource; [community wiki](/wiki) is explanatory, not authoritative.

| rule_id | summary | scope | operation | version | link |
| --- | --- | --- | --- | --- | --- |
| msg.identity | Stable subjects and independent keys | identity | identity.* | 1 | [identity](/_rules/identity) |
| msg.read-write | Resource and Revision writes | resources | content.*, discovery.* | 1 | [read-write](/_rules/read-write) |
| msg.auth | Credentials, CA and access checks | authorization | cert.*, group.* | 1 | [auth](/_rules/auth) |
| msg.topics | Topic membership and moderation | topics | content.topic_*, discussion.* | 1 | [topics](/_rules/topics) |
| msg.files | Transfers and encrypted personal data | files | transfer.*, keystore.* | 1 | [files](/_rules/files) |
| msg.recovery | Custodians and recovery boundaries | recovery | identity.recover | 1 | [recovery](/_rules/recovery) |
| msg.security | Secrets and trusted execution | security | system.*, tool.* | 1 | [security](/_rules/security) |
| msg.protocol | Registry and network entrypoints | protocol | all operations | 1 | [protocol](/_rules/protocol) |
