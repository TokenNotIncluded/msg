"""The msg client. Only JSON results go to stdout; help is explicitly requested."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from msg.client import ClientState,MsgClient
from msg.core.codec import canonical,loads,wire
from msg.core.errors import Failure,require
from msg.core.executor import result_wire
from msg.core.models import ResourceRef,OperationResult
from msg.transports.client import TRANSPORTS


def arguments(value):
    if value=='-':
        raw=sys.stdin.buffer.read(1048577)
    elif value.startswith('@'):
        raw=Path(value[1:]).read_bytes()
    else:
        raw=value.encode()
    require(len(raw)<=1048576,'arguments_too_large')
    parsed=loads(raw)
    require(isinstance(parsed,dict),'arguments_must_be_object')
    return parsed


def parser():
    cli=argparse.ArgumentParser(prog='msg',description='Signed atomic communication for sandboxed agents.')
    cli.add_argument('--config-dir',type=Path,default=Path.home()/'.config'/'msg')
    cli.add_argument('--server')
    cli.add_argument('--transport',choices=TRANSPORTS,default='http')
    cli.add_argument('--certificate',action='append',default=[],help='Attach a registered certificate ID for this invocation.')
    cli.add_argument('--key',type=Path,help='Use a local delegated/CA signing key for this invocation.')
    cli.add_argument('--as-subject',help='Represent a principal authorized by an explicit delegation certificate.')
    commands=cli.add_subparsers(dest='command',required=True)
    identity=commands.add_parser('identity').add_subparsers(dest='action',required=True)
    identity.add_parser('new').add_argument('handle')
    identity.add_parser('temporary')
    identity.add_parser('rotate-token')
    identity.add_parser('upgrade').add_argument('handle')
    identity.add_parser('show')
    call=commands.add_parser('call',help='Call any declared operation with JSON, @file, or - for stdin.')
    call.add_argument('operation');call.add_argument('arguments',nargs='?',default='{}')
    call.add_argument('--request-id');call.add_argument('--expect',action='append',default=[],metavar='ID=GENERATION')
    call.add_argument('--return-field',action='append',default=[])
    schema=commands.add_parser('schema');schema.add_argument('operation')
    commands.add_parser('operations')
    read=commands.add_parser('read');read.add_argument('resource');read.add_argument('--revision')
    read.add_argument('--field',action='append',default=[]);read.add_argument('--meta',action='store_true')
    read.add_argument('--ack',action='store_true',help='Explicitly submit ACK after a successful read.')
    post=commands.add_parser('post');post.add_argument('topic')
    body=post.add_mutually_exclusive_group(required=True);body.add_argument('--text');body.add_argument('--file',type=Path)
    reply=commands.add_parser('reply');reply.add_argument('resource');reply.add_argument('--text',required=True)
    ack=commands.add_parser('ack');ack.add_argument('resource');ack.add_argument('revision')
    upload=commands.add_parser('upload');upload.add_argument('file',type=Path);upload.add_argument('--resume')
    upload.add_argument('--part-bytes',type=int,default=65536);upload.add_argument('--media-type',default='application/octet-stream')
    upload.add_argument('--target')
    download=commands.add_parser('download');download.add_argument('resource');download.add_argument('file',type=Path)
    download.add_argument('--revision');download.add_argument('--part-bytes',type=int,default=65536)
    commands.add_parser('mcp',help='Expose the same operations over auto-signing local MCP stdio.')
    cert=commands.add_parser('cert').add_subparsers(dest='action',required=True)
    cert.add_parser('renew',help='Renew the base certificate without attaching an expired chain.')
    certget=cert.add_parser('get');certget.add_argument('id')
    certrequest=cert.add_parser('request');certrequest.add_argument('spec',help='JSON or @file with issuer, grants, TTL and optional CA issuance.')
    certrequest.add_argument('--ca-key',type=Path,help='Dedicated local key, generated if absent.')
    certissue=cert.add_parser('issue');certissue.add_argument('csr_id');certissue.add_argument('--issuer',required=True)
    certissue.add_argument('--ca-key',type=Path,required=True)
    certissue.add_argument('--approve-digest',help='Bind noninteractive CA approval to the reviewed CSR digest.')
    vault=commands.add_parser('keystore').add_subparsers(dest='action',required=True)
    keygen=vault.add_parser('keygen');keygen.add_argument('--output',type=Path,required=True)
    put=vault.add_parser('put');put.add_argument('name');put.add_argument('file',type=Path);put.add_argument('--recipient',required=True)
    get=vault.add_parser('get');get.add_argument('resource');get.add_argument('--output',type=Path,required=True);get.add_argument('--private-key',type=Path,required=True)
    vault.add_parser('list')
    return cli


async def run(args):
    if args.command=='keystore' and args.action=='keygen':
        from msg.client_secrets import encryption_keygen
        print(canonical(encryption_keygen(args.output)).decode())
        return 0
    state=ClientState(args.config_dir,server=args.server)
    transport=TRANSPORTS[args.transport](state.server)
    client=MsgClient(state,transport)
    if args.key:
        from msg.security.crypto import Ed25519Signer
        require(args.key.stat().st_mode&0o077==0,'unsafe_client_key_permissions')
        state.signer=Ed25519Signer.from_bytes(args.key.read_bytes())
        # Key override is invocation-only, never written back to the primary identity.
        state.data.pop('token',None)
    if args.certificate:
        state.data['certificates']=list(dict.fromkeys([*state.certificates,*args.certificate]))
    if args.as_subject:
        state.data['subject_id']=args.as_subject
    try:
        command=args.command
        if command=='identity':
            if args.action=='new': result=await client.register(args.handle)
            elif args.action=='temporary': result=await client.temporary()
            elif args.action=='rotate-token': result=await client.rotate_token()
            elif args.action=='upgrade': result=await client.upgrade(args.handle)
            else: result={'subject_id':state.subject,'key_id':state.signer.key_id if state.signer else None,
                          'server':state.server,'certificates':state.certificates,'auth':'token' if state.token else 'signature'}
        elif command=='call':
            expected=[]
            for item in args.expect:
                rid,sep,generation=item.rpartition('=')
                require(sep and generation.isdecimal(),'invalid_expected_generation')
                expected.append((rid,int(generation)))
            result=await client.call(args.operation,arguments(args.arguments),request_id=args.request_id,
                                     expected=expected,return_fields=args.return_field)
        elif command=='operations': result=await client.call('discovery.operations')
        elif command=='schema': result=await client.call('discovery.schema',{'operation':args.operation})
        elif command=='read':
            params={'id':args.resource}
            if args.revision:params['revision']=args.revision
            if args.field:params['fields']=args.field
            if args.meta:params['view']='meta'
            result=await client.call('discovery.get',params)
            if args.ack and result.status=='ok':
                require(result.data.get('revision'),'ack_revision_required')
                ack=await client.ack(ResourceRef(id=result.data['id'],revision=result.data['revision']))
                result={'read':result_wire(result),'ack':result_wire(ack)}
        elif command=='post':
            if args.file:
                uploaded=await client.upload(args.file,media_type='text/markdown')
                params={'parent':args.topic,'source':wire(uploaded.output)}
            else: params={'parent':args.topic,'body':args.text}
            result=await client.call('content.post_create',params)
        elif command=='reply': result=await client.call('discussion.reply',{'target':{'id':args.resource},'body':args.text})
        elif command=='ack': result=await client.ack(ResourceRef(id=args.resource,revision=args.revision))
        elif command=='upload': result=await client.upload(args.file,transfer_id=args.resume,
            part_bytes=args.part_bytes,media_type=args.media_type,target=args.target)
        elif command=='download':result=await client.download(ResourceRef(id=args.resource,revision=args.revision),args.file,part_bytes=args.part_bytes)
        elif command=='cert':
            from msg.client_certificates import request_certificate,issue_certificate
            if args.action=='renew': result=await client.renew_certificate()
            elif args.action=='get': result=await client.call('cert.get',{'id':args.id})
            elif args.action=='request': result=await request_certificate(client,arguments(args.spec),args.ca_key)
            else: result=await issue_certificate(client,args.csr_id,args.issuer,args.ca_key,expected_digest=args.approve_digest)
        elif command=='keystore':
            from msg.client_secrets import put_secret,get_secret
            if args.action=='put':result=await put_secret(client,args.name,args.file,args.recipient)
            elif args.action=='get':result=await get_secret(client,args.resource,args.output,args.private_key)
            else:result=await client.call('keystore.list')
        elif command=='mcp':
            from msg.transports.stdio import serve_stdio
            await serve_stdio(client)
            return 0
        else: raise Failure('unknown_command')
        print(canonical(result_wire(result) if isinstance(result,OperationResult) else result).decode())
        return 1 if isinstance(result,OperationResult) and result.status=='error' else 0
    finally:
        await transport.close()


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except Failure as exc:
        print(canonical({'status':'error','error':exc.as_dict()}).decode(),file=sys.stderr)
        return 1
    except (OSError,ValueError) as exc:
        # File contents and credential-bearing URLs never appear in errors.
        print(canonical({'status':'error','error':{'code':'local_io_error','type':type(exc).__name__}}).decode(),file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__=='__main__':
    raise SystemExit(main())
