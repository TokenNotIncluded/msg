"""The same signed envelope is used by HTTP, path GET, GraphQL, MCP and msg."""
from __future__ import annotations
from dataclasses import replace
from datetime import UTC,datetime,timedelta
from uuid import uuid4
from msg.core.codec import canonical,digest,wire
from msg.core.models import OperationRequest,SignatureProof,TokenProof


def payload_fields(request):
    data=wire(request)
    for field in ('proof','payload_digest','source','expires_at'):
        data.pop(field)
    return data


def signing_bytes(request):
    data=wire(request)
    data.pop('proof')
    return canonical(data)


def request_for(operation,arguments,service, *, subject=None,signer=None,token=None,
                certificates=(),request_id=None,expires_at=None,source='msg',expected=(),return_fields=(),
                contract_version=1):
    request=OperationRequest(request_id=request_id or uuid4().hex,protocol_version=1,
        operation=operation,contract_version=contract_version,target_service=service,subject=subject,
        arguments=arguments,expected_generations=tuple(expected),
        expires_at=expires_at or datetime.now(UTC)+timedelta(minutes=3),payload_digest='',proof=None,
        return_fields=tuple(return_fields),source=source)
    request=replace(request,payload_digest=digest(payload_fields(request)))
    if signer is not None:
        request=replace(request,proof=SignatureProof(signature=signer.sign(signing_bytes(request),purpose='request'),
                                                   certificates=tuple(certificates)))
    elif token is not None:
        request=replace(request,proof=TokenProof(credential_id=token[0],token=token[1]))
    return request


def receipt_bytes(result):
    value=wire(result)
    for field in ('receipt','replayed','prefer_cli','cli_url'):
        value.pop(field,None)
    if result.operation in {'identity.temporary','identity.custodial_create',
                            'identity.token_rotate','identity.token_create',
                            'identity.token_recover'} and value.get('data'):
        value['data'].pop('token',None)
    return canonical(value)
