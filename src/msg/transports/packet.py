"""Bounded wire decoding shared by every network adapter."""
from __future__ import annotations
import re
import zlib
from collections.abc import Mapping
from msg.core.codec import decode,loads,unb64,canonical,wire
from msg.core.errors import require,Failure
from msg.core.models import OperationRequest,OperationResult,TokenProof
from msg.plugins.schemas import obj,IDENTIFIER,STRING,BYTES,REF,SIGNATURE

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


_URL_SECRET_FIELDS = frozenset({'token', 'recovery_secret',
    'new_recovery_secret', 'private_key', 'secret', 'bootstrap_claim',
    'password', 'api_key', 'authorization'})
_CLAIM_OPERATIONS = frozenset({'identity.temporary', 'identity.custodial_create',
    'identity.token_create', 'identity.token_rotate', 'identity.token_recover'})


def require_url_safe_packet(packet: OperationRequest) -> None:
    """Reject credentials and recovery claims before a GET URL is dispatched.

    A signature is a bounded request proof; TokenProof and issuance claims are
    reusable secrets. This guards both clients constructing URL packets and the
    server receiving hand-built packets, including gzip envelopes.
    """
    require(not isinstance(packet.proof, TokenProof), 'secure_channel_required')
    require(packet.operation not in _CLAIM_OPERATIONS, 'secure_channel_required')

    def has_secret(value):
        if isinstance(value, Mapping):
            return any(key.casefold() in _URL_SECRET_FIELDS or has_secret(item)
                       for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return any(has_secret(item) for item in value)
        return False

    require(not has_secret(packet.arguments), 'secure_channel_required')


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


def decode_result(value):
    # Compact results omit anonymous actor/subject; these required domain fields
    # are restored explicitly, not inferred from a transport connection.
    return decode(OperationResult,dict({'actor':None,'subject':None},**value))


def gunzip(raw,limit):
    inflater=zlib.decompressobj(wbits=31)
    try:
        result=inflater.decompress(raw,limit+1)
        require(len(result)<=limit and not inflater.unconsumed_tail,'request_too_large')
        result+=inflater.flush(limit+1-len(result))
        require(len(result)<=limit and inflater.eof and not inflater.unused_data,'invalid_gzip')
        return result
    except zlib.error as exc:
        raise Failure('invalid_gzip') from exc


def path_packet(encoded,encoding,limit):
    raw=unb64(encoded,limit=limit)
    require(encoding in {'j','gz'},'unknown_encoding')
    return decode_packet(gunzip(raw,limit) if encoding=='gz' else raw,limit)
