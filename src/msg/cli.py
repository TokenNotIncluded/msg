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
    identity.add_parser('recover-token',help='Use a saved one-time recovery journal after a lost token response.')
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
    search=commands.add_parser('search',help='One bounded page of scoped lexical results.')
    search.add_argument('scope',nargs='?',help='Required for a new search; omit with --cursor.')
    search.add_argument('terms',nargs='?',help='Words to find; omit with --cursor.')
    search.add_argument('--cursor',help='Fetch one next page using the server-issued cursor.')
    search.add_argument('--limit',type=int,default=50)
    search.add_argument('--mode',choices=('all','any'),default='all')
    search.add_argument('--field',choices=('all','name','body','metadata'),default='all')
    search.add_argument('--exact');search.add_argument('--exclude',dest='not_terms')
    search.add_argument('--type');search.add_argument('--owner');search.add_argument('--author')
    search.add_argument('--tag');search.add_argument('--state',choices=('active','archived'))
    search.add_argument('--created-after');search.add_argument('--created-before')
    search.add_argument('--updated-after');search.add_argument('--updated-before')
    search.add_argument('--has-attachment',action='store_true')
    search.add_argument('--depth',type=int);search.add_argument('--no-recursive',action='store_true')
    search.add_argument('--order',choices=('relevance','updated','created','name'))
    search.add_argument('--no-snippet',action='store_true')
    search.add_argument('--explain',action='store_true')
    grep=commands.add_parser('grep',help='Search a known scope with explicit result caps.')
    grep.add_argument('scope');grep.add_argument('pattern')
    grep.add_argument('--regex',action='store_true');grep.add_argument('--ignore-case',action='store_true')
    grep.add_argument('--glob');grep.add_argument('--exclude-glob')
    grep.add_argument('--before',type=int,default=0);grep.add_argument('--after',type=int,default=0)
    grep.add_argument('--max-matches',type=int,default=50);grep.add_argument('--max-files',type=int,default=50)
    grep_mode=grep.add_mutually_exclusive_group()
    grep_mode.add_argument('--files-with-matches',action='store_true')
    grep_mode.add_argument('--count-only',action='store_true')
    post=commands.add_parser('post');post.add_argument('topic')
    body=post.add_mutually_exclusive_group(required=True);body.add_argument('--text');body.add_argument('--file',type=Path)
    reply=commands.add_parser('reply');reply.add_argument('resource');reply.add_argument('--text',required=True)
    dm=commands.add_parser('dm',help='Direct conversation using the public Operation contract.')
    dm_actions=dm.add_subparsers(dest='action',required=True)
    dm_actions.add_parser('list')
    dm_request=dm_actions.add_parser('request');dm_request.add_argument('recipient')
    dm_send=dm_actions.add_parser('send');dm_send.add_argument('conversation');dm_send.add_argument('--text',required=True)
    dm_read=dm_actions.add_parser('read');dm_read.add_argument('resource')
    for action in ('accept','reject','archive'):
        dm_actions.add_parser(action).add_argument('conversation')
    dm_actions.add_parser('block').add_argument('subject')
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
    recovery=commands.add_parser('recovery',help='Explicit self-custody age backup; no account authority.')
    recovery_actions=recovery.add_subparsers(dest='action',required=True)
    recovery_actions.add_parser('policy')
    policy_set=recovery_actions.add_parser('policy-set')
    policy_set.add_argument('--recipient',action='append',default=[])
    policy_set.add_argument('--clear',action='store_true',help='Opt out of future envelope registration.')
    backup=recovery_actions.add_parser('backup')
    backup.add_argument('--recipient',action='append',required=True)
    backup.add_argument('--policy-version',type=int,required=True)
    backup.add_argument('--name')
    recovery_actions.add_parser('list')
    envelope_get=recovery_actions.add_parser('get');envelope_get.add_argument('id')
    restore=recovery_actions.add_parser('restore')
    restore.add_argument('ciphertext',type=Path)
    restore.add_argument('--identity',type=Path,required=True)
    restore.add_argument('--output',type=Path,required=True)
    restore.add_argument('--subject',required=True)
    restore.add_argument('--key-id',required=True)
    rewrap=recovery_actions.add_parser('rewrap')
    rewrap.add_argument('resource')
    rewrap.add_argument('--old-identity',type=Path,required=True)
    rewrap.add_argument('--expected-revision',required=True)
    legacy=commands.add_parser('legacy',help='Signed last-will declarations; never executes account actions.')
    legacy_actions=legacy.add_subparsers(dest='action',required=True)
    legacy_put=legacy_actions.add_parser('put')
    legacy_put.add_argument('spec',help='JSON, @file or - for stdin.')
    legacy_actions.add_parser('archive')
    legacy_actions.add_parser('status')
    legacy_get=legacy_actions.add_parser('get')
    legacy_get.add_argument('--subject')
    legacy_get.add_argument('--revision')
    return cli


async def run(args):
    if args.command=='keystore' and args.action=='keygen':
        from msg.client_secrets import encryption_keygen
        print(canonical(encryption_keygen(args.output)).decode())
        return 0
    if args.command=='recovery' and args.action=='restore':
        from msg.client_recovery import restore_recovery_envelope
        value=restore_recovery_envelope(args.ciphertext.read_bytes(),args.identity,args.output,
            expected_subject_id=args.subject,expected_encryption_key_id=args.key_id)
        print(canonical(value).decode())
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
            elif args.action=='recover-token': result=await client.recover_token()
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
        elif command=='search':
            require(1<=args.limit<=100,'invalid_search_limit')
            if args.cursor:
                require(args.scope is None and args.terms is None,'cursor_query_mismatch')
                require(args.limit==50 and args.mode=='all' and args.field=='all' and
                        args.exact is None and args.not_terms is None and args.type is None and
                        args.owner is None and args.author is None and args.tag is None and
                        args.state is None and args.created_after is None and
                        args.created_before is None and args.updated_after is None and
                        args.updated_before is None and not args.has_attachment and
                        args.depth is None and not args.no_recursive and args.order is None and
                        not args.no_snippet and not args.explain,'cursor_query_mismatch')
                params={'cursor':args.cursor}
            else:
                require(args.scope is not None and (args.terms or args.exact),
                        'search_query_required')
                params={'scope':args.scope,'mode':args.mode,'field':args.field,'limit':args.limit}
                if args.terms:params['terms']=args.terms
                for key in ('exact','not_terms','type','owner','author','tag','state',
                            'created_after','created_before','updated_after','updated_before',
                            'depth','order'):
                    value=getattr(args,key)
                    if value is not None:params[key]=value
                if args.has_attachment:params['has_attachment']=True
                if args.no_recursive:params['recursive']=False
                if args.no_snippet:params['snippet']=False
                if args.explain:params['explain']='compact'
            result=await client.call('discovery.lexical_search',params)
        elif command=='grep':
            require(0<=args.before<=3 and 0<=args.after<=3,'invalid_grep_context')
            require(1<=args.max_matches<=100 and 1<=args.max_files<=100,
                    'invalid_grep_limit')
            params={'scope':args.scope,'pattern':args.pattern,'regex':args.regex,
                    'case_sensitive':not args.ignore_case,'before':args.before,
                    'after':args.after,'max_matches':args.max_matches,
                    'max_files':args.max_files}
            for key in ('glob','exclude_glob'):
                value=getattr(args,key)
                if value is not None:params[key]=value
            if args.files_with_matches:params['files_with_matches']=True
            if args.count_only:params['count_only']=True
            result=await client.call('discovery.grep',params)
        elif command=='post':
            if args.file:
                uploaded=await client.upload(args.file,media_type='text/markdown')
                params={'parent':args.topic,'source':wire(uploaded.output)}
            else: params={'parent':args.topic,'body':args.text}
            result=await client.call('content.post_create',params)
        elif command=='reply': result=await client.call('discussion.reply',{'target':{'id':args.resource},'body':args.text})
        elif command=='dm':
            if args.action=='list':result=await client.call('communication.dm_list')
            elif args.action=='request':result=await client.call('communication.dm_request',
                                                                 {'recipient':args.recipient})
            elif args.action=='send':result=await client.call('communication.dm_send',
                {'conversation_id':args.conversation,'body':args.text})
            elif args.action=='read':result=await client.call('discovery.get',{'id':args.resource})
            elif args.action=='block':result=await client.call('communication.dm_block',
                                                               {'subject_id':args.subject})
            else:result=await client.call('communication.dm_'+args.action,
                                          {'conversation_id':args.conversation})
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
        elif command=='recovery':
            from msg.client_recovery import save_recovery_envelope,rewrap_age_keystore_entry
            from msg.security.age_keys import encryption_key_id,public_from_recipient
            if args.action=='policy':result=await client.call('identity.recovery_policy_get')
            elif args.action=='policy-set':
                require(args.clear or bool(args.recipient),'recovery_recipients_required')
                require(not (args.clear and args.recipient),'ambiguous_recovery_policy')
                require(state.encryption_recipient is not None,'encryption_key_not_found')
                current=client.checked(await client.call('identity.recovery_policy_get'))
                result=await client.call('identity.recovery_policy_set',{
                    'expected_version':current.data['version'],
                    'encryption_key_id':encryption_key_id(public_from_recipient(
                        state.encryption_recipient)),
                    'recipients':[] if args.clear else [{'recipient':value}
                                                        for value in args.recipient]})
            elif args.action=='backup':
                stored,envelope,metadata=await save_recovery_envelope(
                    client,args.recipient,policy_version=args.policy_version,name=args.name)
                result={'keystore':result_wire(stored),'envelope':result_wire(envelope),
                        'metadata':metadata}
            elif args.action=='list':result=await client.call('identity.recovery_envelope_list')
            elif args.action=='get':result=await client.call('identity.recovery_envelope_get',
                                                             {'id':args.id})
            else:result=await rewrap_age_keystore_entry(client,args.resource,
                args.old_identity,expected_revision=args.expected_revision)
        elif command=='legacy':
            if args.action in {'put','archive'}:
                payload=arguments(args.spec) if args.action=='put' else {}
                expected=()
                if args.action=='archive' or 'expected_revision' in payload:
                    require(state.subject is not None,'subject_required')
                    current=client.checked(await client.call('identity.legacy_get',
                        {'subject_id':state.subject}))
                    if 'expected_revision' in payload:
                        require(current.data['revision']==payload['expected_revision'],
                                'revision_conflict')
                    meta=client.checked(await client.call('discovery.get',
                        {'id':current.data['id'],'view':'meta'}))
                    expected=((current.data['id'],meta.data['generation']),)
                operation='identity.legacy_put' if args.action=='put' else 'identity.legacy_archive'
                result=await client.call(operation,payload,expected=expected)
            elif args.action=='status':result=await client.call('identity.legacy_status')
            else:
                subject=args.subject or state.subject
                require(subject is not None,'subject_required')
                params={'subject_id':subject}
                if args.revision:params['revision']=args.revision
                result=await client.call('identity.legacy_get',params)
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
