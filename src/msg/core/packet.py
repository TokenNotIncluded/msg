"""Transport-neutral request/result contracts and bounded canonical decoding."""
from __future__ import annotations
from collections.abc import Mapping
import re
from msg.core.codec import decode, loads, canonical, wire
from msg.core.errors import require
from msg.core.models import OperationRequest, OperationResult
from msg.core.schemas import obj, IDENTIFIER, STRING, BYTES, REF, SIGNATURE

REQUEST_SCHEMA=obj({
    'request_id':{'type':'string','minLength':1,'maxLength':128},'protocol_version':{'const':1},
    'operation':{'type':'string','pattern':'^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$','maxLength':128},
    'contract_version':{'type':'integer','minimum':1},'target_service':{'type':'string','maxLength':2048},
    'subject':{'type':['string','null'],'maxLength':160},'arguments':{'type':'object'},
    'expected_generations':{'type':'array','maxItems':64,'items':{'type':'array','prefixItems':[IDENTIFIER,{'type':'integer','minimum':0}],
                                                                 'minItems':2,'maxItems':2}},
    'expires_at':{'type':['string','null'],'maxLength':40},
    'payload_digest':{'type':'string','pattern':'^sha256:[0-9a-f]{64}$'},
    'proof':{'anyOf':[{'type':'null'},obj({'signature':SIGNATURE,'certificates':{'type':'array','items':IDENTIFIER,'maxItems':16}},('signature','certificates')),
                    obj({'credential_id':IDENTIFIER,'token':BYTES},('credential_id','token'))]},
    'return_fields':{'type':'array','maxItems':30,'items':STRING},'source':{'enum':['msg','manual','mcp','unknown']}
},('request_id','protocol_version','operation','contract_version','target_service','subject','arguments',
    'expected_generations','expires_at','payload_digest','proof'))
RESULT_SCHEMA={'type':'object','properties':{
    'request_id':STRING,'operation':STRING,'status':{'enum':['ok','accepted','error','uncertain']},
    'actor':STRING,'subject':STRING,'resources':{'type':'array','items':REF},'committed_at':STRING,
    'replayed':{'type':'boolean'},'receipt':SIGNATURE,'error':{'type':'object'},'data':{'type':'object'},
    'output':REF,'prefer_cli':{'type':'boolean'},'cli_url':STRING},
    'required':['request_id','operation','status'],'additionalProperties':False}


def decode_packet(value,max_bytes=1048576):
    from jsonschema import Draft202012Validator
    if isinstance(value,(bytes,str)):
        require(len(value.encode() if isinstance(value,str) else value)<=max_bytes,'request_too_large')
        value=loads(value)
    value=wire(value)
    require(len(canonical(value))<=max_bytes,'request_too_large')
    error=next(Draft202012Validator(REQUEST_SCHEMA).iter_errors(value),None)
    require(error is None,'invalid_request_envelope', '.'.join(map(str,error.path)) if error else None)
    packet=decode(OperationRequest,value)
    require(len(set(rid for rid,generation in packet.expected_generations))==len(packet.expected_generations),
            'duplicate_expected_generation')
    return packet


def safe_error_code(value, fallback):
    """Only bounded contract identifiers, never exception text or credential URLs."""
    return (value if isinstance(value, str) and
            re.fullmatch(r'[a-z][a-z0-9_]{0,95}', value) else fallback)


def decode_result(value):
    # Reject malformed signed/unsigned results rather than rewriting their error
    # fields. Valid receipts and persisted result bytes are left unchanged.
    require(isinstance(value, Mapping), 'invalid_result_envelope')
    error = value.get('error')
    if error is not None:
        require(isinstance(error, Mapping) and safe_error_code(error.get('code'), None) is not None,
                'invalid_result_envelope')
    # Compact results omit anonymous actor/subject; these required domain fields
    # are restored explicitly, not inferred from a transport connection.
    return decode(OperationResult,dict({'actor':None,'subject':None},**value))


def result_wire(result):
    value=wire(result,compact=True)
    for name in ('replayed','prefer_cli'):
        if not value.get(name):
            value.pop(name,None)
    return value
