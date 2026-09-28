from pathlib import Path
r=Path('src/msg')
# Move the actual common schema values, without cloning/rewriting contracts.
s=(r/'plugins/schemas.py').read_text()
(r/'core/schemas.py').write_text(s)
(r/'plugins/schemas.py').write_text('''"""Compatibility exports; shared wire schemas are owned by msg.core.schemas."""
from msg.core.schemas import (
    STRING, IDENTIFIER, INTEGER, BOOLEAN, BYTES, REF, SCOPE, NETWORK_CONSTRAINTS,
    GRANT, GRANTS, ISSUANCE, SIGNATURE, OUTPUT, obj,
)
''')
# Packet/request/result codecs are protocol contracts. URL/gzip rules stay outside.
p=r/'transports/packet.py';s=p.read_text()
schema=s[s.index('REQUEST_SCHEMA='):s.index('def require_url_safe_packet')].rstrip()
funcs=s[s.index('def decode_packet'):s.index('def gunzip')].rstrip()
executor=(r/'core/executor.py').read_text()
result_wire=executor[executor.index('def result_wire'):]
(r/'core/packet.py').write_text('''"""Transport-neutral request/result contracts and bounded canonical decoding."""
from __future__ import annotations
from collections.abc import Mapping
import re
from msg.core.codec import decode, loads, canonical, wire
from msg.core.errors import require
from msg.core.models import OperationRequest, OperationResult
from msg.core.schemas import obj, IDENTIFIER, STRING, BYTES, REF, SIGNATURE

'''+schema+'\n\n\n'+funcs+'\n\n\n'+result_wire)
url=s[s.index('def require_url_safe_packet'):s.index('def decode_packet')]
gzip=s[s.index('def gunzip'):]
p.write_text('''"""URL and compression boundaries; common wire contracts live in core.packet."""
from __future__ import annotations
import zlib
from msg.core.codec import unb64
from msg.core.errors import require, Failure
from msg.core.requests import SECRET_DELIVERY_MIN_VERSION
from msg.core.models import OperationRequest, TokenProof
from msg.core.packet import (
    REQUEST_SCHEMA, RESULT_SCHEMA, decode_packet, decode_result, safe_error_code,
)
from msg.transports.url_safety import contains_secret_fields


'''+url+gzip)
# Preserve stable event IDs for cached jobs and receipts.
(r/'core/events.py').write_text('''"""Deterministic committed-operation event identity shared by all producers."""
from msg.core.codec import digest


def event_id(request, subject):
    return 'e_' + digest((subject, request.request_id))[7:39]
''')
p=r/'plugins/communication.py';s=p.read_text();s=s.replace("def event_id(request,subject):\n    return 'e_'+digest((subject,request.request_id))[7:39]\n",'from msg.core.events import event_id\n');p.write_text(s)
# One batch validator shared by pre-replay guard and registered atomic handler.
p=r/'plugins/batch.py';s=p.read_text();a=s.index('# Credential delivery');b=s.index('def install(app):')
(r/'core/batching.py').write_text('''"""The one child-envelope validator for atomic and independent batches."""
from msg.core.codec import canonical
from msg.core.errors import require
from msg.core.packet import decode_packet
from msg.core.requests import SECRET_DELIVERY_MIN_VERSION

'''+s[a:b])
s=s[:a]+s[b:];s=s.replace('from msg.core.codec import wire,canonical\n','').replace('from msg.core.requests import SECRET_DELIVERY_MIN_VERSION\n','').replace('from msg.core.executor import result_wire','from msg.core.packet import result_wire, REQUEST_SCHEMA\nfrom msg.core.batching import NO_BATCH, packets').replace('from msg.transports.packet import REQUEST_SCHEMA,decode_packet\n','');p.write_text(s)
(r/'core/execution_ports.py').write_text('''"""Fixed executor dependencies, not plugin hooks or a workflow interface.

ResultProjection is a permission-checked read in the existing transaction.
TransactionalEventNotifications may enqueue durable jobs in that transaction;
it must never perform external I/O or commit it. Execution, auth and receipt
ownership remain in OperationExecutor.
"""
from typing import Protocol
from msg.core.contracts import MetadataSession
from msg.core.models import Event, ExecutionContext, JsonMap, OperationRequest, ResourceRef


class ResultProjection(Protocol):
    async def __call__(self, context: ExecutionContext, request: OperationRequest,
                       session: MetadataSession, resource: ResourceRef,
                       *, fields: tuple[str, ...]) -> JsonMap: ...


class TransactionalEventNotifications(Protocol):
    async def __call__(self, session: MetadataSession, event: Event) -> None: ...
''')
p=r/'core/executor.py';s=executor[:executor.index('\ndef result_wire')].rstrip()+'\n'
s=s.replace('from msg.core.codec import wire,digest','from msg.core.codec import wire,digest\nfrom msg.core.batching import packets\nfrom msg.core.events import event_id\nfrom msg.core.packet import result_wire\nfrom msg.core.execution_ports import ResultProjection, TransactionalEventNotifications')
s=s.replace('def __init__(self,registry,metadata,contents,authenticator,authorizer,clock,receipt_signer):','''def __init__(self,registry,metadata,contents,authenticator,authorizer,clock,receipt_signer,
                 *, max_request_bytes: int | None = None,
                 result_projection: ResultProjection | None = None,
                 event_notifications: TransactionalEventNotifications | None = None):''')
s=s.replace('        self.application=None','''        require(max_request_bytes is None or (type(max_request_bytes) is int and max_request_bytes>0),
                'invalid_request_limit')
        self.max_request_bytes=max_request_bytes
        self.result_projection=result_projection
        self.event_notifications=event_notifications''')
s=s.replace('                    from msg.plugins.batch import packets\n','').replace('                            self.application.settings.server.limits.max_request_bytes)','                            self.max_request_bytes)')
s=s.replace('''                        from msg.plugins.discovery import read_projection
                        require(output.resources,'projection_unavailable')
                        projections=[await read_projection(self.application,context,request,session,ref.id,revision=ref.revision,
                                     fields=request.return_fields) for ref in output.resources]''','''                        require(output.resources and self.result_projection is not None,'projection_unavailable')
                        projections=[await self.result_projection(context,request,session,ref,
                                     fields=request.return_fields) for ref in output.resources]''')
s=s.replace('                        from msg.plugins.communication import event_id\n','')
s=s.replace('''                        from msg.plugins.communication import WEBHOOK_DOMAIN_EVENTS,enqueue_domain_webhooks
                        if event.type in WEBHOOK_DOMAIN_EVENTS and self.application is not None:
                            await enqueue_domain_webhooks(self.application,session,event)''','''                        if self.event_notifications is not None:
                            await self.event_notifications(session,event)''')
s=s.replace('        from msg.plugins.batch import packets\n','').replace('        from msg.plugins.communication import event_id\n','').replace('children=packets(self.registry,request,principal.subject)','children=packets(self.registry,request,principal.subject,self.max_request_bytes)')
assert 'msg.plugins' not in s and 'self.application' not in s
p.write_text(s)
# Composition root may bind application-aware implementations; core sees only ports.
p=r/'application.py';s=p.read_text()
s=s.replace('''        self.executor=OperationExecutor(self.registry,self.metadata,self.contents,self.authenticator,self.authorizer,
                                       self.clock,self.receipt_signer)
        self.executor.application=self''','''        self.executor=self.new_executor(self.authenticator)''')
s=s.replace('        self.executor.response_hook=self._secrets_for_caller\n','')
a=s.index('    async def online_issuer(')
s=s[:a]+'''    def new_executor(self, authenticator=None):
        """Compose identical limits/ports for HTTP and the restricted SSH entry."""
        executor=OperationExecutor(self.registry,self.metadata,self.contents,
            self.authenticator if authenticator is None else authenticator,
            self.authorizer,self.clock,self.receipt_signer,
            max_request_bytes=self.settings.server.limits.max_request_bytes,
            result_projection=self._result_projection,
            event_notifications=self._event_notifications)
        executor.response_hook=self._secrets_for_caller
        executor.recovery_drill_marker=self.settings.config_dir/'recovery-drill.json'
        if self.executor is not None:
            executor.recovery_quarantined=self.executor.recovery_quarantined
        return executor

    async def _result_projection(self, context, request, session, resource, *, fields):
        from msg.plugins.discovery import read_projection
        return await read_projection(self,context,request,session,resource.id,
                                     revision=resource.revision,fields=fields)

    async def _event_notifications(self, session, event):
        from msg.plugins.communication import enqueue_domain_webhooks
        await enqueue_domain_webhooks(self,session,event)

'''+s[a:]
p.write_text(s)
p=r/'extensions/ssh.py';s=p.read_text();s=s.replace('''        executor = OperationExecutor(app.registry, app.metadata, app.contents, auth, app.authorizer, app.clock, app.receipt_signer)
        # The normal secret delivery hook remains gated by signed operations.
        executor.application = app
        executor.response_hook = app._secrets_for_caller''','''        executor = app.new_executor(auth)''');s=s.replace('from msg.core.executor import OperationExecutor, result_wire','from msg.core.packet import result_wire');p.write_text(s)
# Fake storage fixture keeps its explicit narrow composition (no fake auth changes).
p=Path('tests/issue_78_80/conftest.py');s=p.read_text();s=s.replace('''    app.executor=OperationExecutor(registry,metadata,app.contents,app.authenticator,
        app.authorizer,app.clock,Ed25519Signer.generate())
    app.executor.application=app''','''    async def result_projection(context, request, session, resource, *, fields):
        return await discovery.read_projection(app,context,request,session,resource.id,
                                               revision=resource.revision,fields=fields)

    app.executor=OperationExecutor(registry,metadata,app.contents,app.authenticator,
        app.authorizer,app.clock,Ed25519Signer.generate(),
        max_request_bytes=app.settings.server.limits.max_request_bytes,
        result_projection=result_projection)''');p.write_text(s)

p=r/'core/batching.py';p.write_text(p.read_text().rstrip()+'\n')

p=Path('docs/ISSUE_PROGRESS.md')
p.write_text(p.read_text()+'\n## 当前提交进度'+'\n\n失败基线已提交453d6f858e19136261278c7042c13598035aef1a，tree ae0b4f5e31dc1844c1abd0142da898b75b7779d4。云端专项36481310584和完整36481310527已触发；回读时仍排队，未当作通过。\n\n实现采用固定的ResultProjection和TransactionalEventNotifications接口，不引入任意插件钩子。core.batching/core.events/core.packet/core.schemas分别只有一份真实逻辑；旧公开导入路径兼容重导出。URL秘密检查和gzip只在传输层，未移到所有入口。Application.new_executor同时装配HTTP/SSH的请求限制、秘密释放和恢复隔离，不再给executor整棵Application。未运行本地项目测试，尚无修复后通过结论。\n')
