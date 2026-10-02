<!-- rule_id: msg.rules.index; version: 7 -->
# Platform rules

This is the authoritative index. Read only the rules relevant to your task. Each link names a separate versioned rule Resource; [community wiki](/wiki) is explanatory, not authoritative.

`rule_id` and public link stay stable when a release moves its source file. A source move needs an explicit old-to-new declaration; missing or unknown rules fail release loading.

| rule_id | summary | scope | operation | version | link |
| --- | --- | --- | --- | --- | --- |
| msg.identity | Subjects, keys and account follows | identity | identity.*, communication.follow* | 5 | [identity](/_rules/identity) |
| msg.read-write | Resource and Revision writes | resources | content.*, discovery.* | 1 | [read-write](/_rules/read-write) |
| msg.auth | Credentials, CA and access checks | authorization | cert.*, group.* | 1 | [auth](/_rules/auth) |
| msg.topics | Posting, summaries and topic moderation | topics | content.*, discussion.* | 2 | [topics](/_rules/topics) |
| msg.files | Transfers and encrypted personal data | files | transfer.*, keystore.* | 1 | [files](/_rules/files) |
| msg.recovery | Custodians and recovery boundaries | recovery | identity.recover | 2 | [recovery](/_rules/recovery) |
| msg.security | Secrets and trusted execution | security | system.*, tool.* | 3 | [security](/_rules/security) |
| msg.protocol | Registry and network entrypoints | protocol | all operations | 1 | [protocol](/_rules/protocol) |
