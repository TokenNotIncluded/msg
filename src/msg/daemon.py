"""msgd: local installation, serving, workers, diagnostics and restricted SSH."""
from __future__ import annotations
import argparse
import asyncio
from datetime import UTC,datetime
import json
import os
from pathlib import Path
import signal
import sys

from msg import __version__
from msg.core.codec import canonical,wire
from msg.core.errors import Failure,require


def emit(value):
    print(canonical(wire(value)).decode())


def network_runtime(settings):
    require(sys.version_info[:2]>=(3,15),'python_315_required')
    require(not hasattr(os,'geteuid') or os.geteuid()!=0,'network_service_must_not_run_as_root')
    # This is a permission probe, not a root material read or signer injection.
    require(not os.access(settings.config_dir/'root',os.R_OK|os.X_OK),'root_material_accessible_to_service')


def load_application(directory):
    from msg.application import Application
    from msg.config import load_settings
    return Application(load_settings(directory))


async def worker_loop(app, *, once=False):
    from msg.workers.effects import EffectWorker
    from msg.workers.maintenance import run_maintenance
    await app.load()
    worker=EffectWorker(app)
    stop=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        try:loop.add_signal_handler(sig,stop.set)
        except (NotImplementedError,RuntimeError):pass
    next_cleanup=0.0
    try:
        while not stop.is_set():
            if loop.time()>=next_cleanup:
                await run_maintenance(app,'cleanup_expired',scheduled=True)
                next_cleanup=loop.time()+60
            processed=await worker.run_once()
            if once:return {'processed':processed}
            if not processed:
                try:await asyncio.wait_for(stop.wait(),1)
                except TimeoutError:pass
    finally:
        await app.close()


def parser():
    root=argparse.ArgumentParser(prog='msgd',description='Agent communication service and local administration')
    root.add_argument('--version',action='version',version=__version__)
    root.add_argument('--config-dir',type=Path,default=Path('/etc/msgd'))
    sub=root.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init',help='Initialize from the physical local console; no PIN arguments')
    init.add_argument('--data-dir',type=Path,default=Path('/var/lib/msgd'))
    init.add_argument('--service-url',default='https://msg.lmm.best')
    sub.add_parser('serve',help='Serve JSON/Markdown, operations, GraphQL and MCP')
    hosted=sub.add_parser('hosting',help='Serve user-published files on the separate configured origin')
    hosted.add_argument('--listen',default='127.0.0.1');hosted.add_argument('--port',type=int,default=8043)
    worker=sub.add_parser('worker',help='Run the global durable effects/retention queue')
    worker.add_argument('--once',action='store_true')
    sub.add_parser('doctor',help='Read-only inspection; never repair or reinitialize')
    sub.add_parser('selftest',help='Run real end-to-end checks in a disposable installation')
    cert=sub.add_parser('cert');cs=cert.add_subparsers(dest='cert_command',required=True)
    issue=cs.add_parser('issue');issue.add_argument('csr_id')
    revoke=cs.add_parser('revoke');revoke.add_argument('certificate_id');revoke.add_argument('--reason',required=True)
    rootkey=sub.add_parser('root');rs=rootkey.add_subparsers(dest='root_command',required=True)
    rs.add_parser('change-pin')
    rotate=rs.add_parser('rotate');rotate.add_argument('--lost-key',action='store_true');rotate.add_argument('--resume',action='store_true')
    rb=rs.add_parser('backup');rb.add_argument('destination',type=Path)
    rr=rs.add_parser('recover');rr.add_argument('source',type=Path)
    backup=sub.add_parser('backup',help='Local service-data backup, excluding root private material')
    backup.add_argument('destination',type=Path)
    restore=sub.add_parser('restore',help='Restore service data into new directories only')
    restore.add_argument('source',type=Path);restore.add_argument('--data-dir',type=Path,required=True)
    auth=sub.add_parser('ssh-authorized-key',help=argparse.SUPPRESS)
    auth.add_argument('key_type');auth.add_argument('key_body')
    ssh=sub.add_parser('ssh-session',help=argparse.SUPPRESS)
    ssh.add_argument('--credential',required=True)
    return root


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command in {'doctor','selftest'}:
            from msg.admin.diagnostics import doctor,selftest
            result=doctor(args.config_dir) if args.command=='doctor' else asyncio.run(selftest())
            emit(result)
            return 0 if result['ok'] else 1
        if args.command=='init':
            from msg.config import write_example
            # An unattended invocation stops before creating any trust material.
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                emit({'status':'needs_initialization','command':'msgd init','code':'interactive_pin_required'})
                return 78
            require(hasattr(os,'geteuid') and os.geteuid()==0,'local_os_administrator_required')
            args.config_dir.mkdir(parents=True,exist_ok=True)
            from msg.admin.root import RootAdmin,require_local_console
            require_local_console(args.config_dir)
            write_example(args.config_dir,args.data_dir,args.service_url)
            result=RootAdmin(args.config_dir).initialize()
            emit({'root_id':result})
            return 0
        if args.command in {'cert','root'}:
            from msg.admin.root import RootAdmin
            admin=RootAdmin(args.config_dir)
            if args.command=='cert':
                result=admin.issue(args.csr_id) if args.cert_command=='issue' else admin.revoke(args.certificate_id,args.reason)
            elif args.root_command=='change-pin':
                admin.change_pin();result={'status':'pin_changed'}
            elif args.root_command=='rotate':result=admin.rotate(lost_key=args.lost_key,resume=args.resume)
            elif args.root_command=='backup':result=admin.backup(args.destination)
            else:result=admin.recover(args.source)
            emit(result);return 0
        if args.command=='restore':
            from msg.admin.backups import restore
            emit(restore(args.source,args.config_dir,args.data_dir));return 0
        if args.command=='ssh-authorized-key':
            from msg.extensions.ssh import authorized_key
            print(asyncio.run(authorized_key(args.config_dir,args.key_type,args.key_body)))
            return 0
        if args.command=='ssh-session':
            from msg.extensions.ssh import forced_session
            return asyncio.run(forced_session(args.config_dir,args.credential))
        app=load_application(args.config_dir)
        if args.command=='backup':
            from msg.admin.backups import backup
            async def save():
                await app.load()
                try:return await backup(app,args.destination)
                finally:await app.close()
            emit(asyncio.run(save()));return 0
        network_runtime(app.settings)
        if args.command=='worker':
            result=asyncio.run(worker_loop(app,once=args.once))
            if result is not None:emit(result)
            return 0
        import uvicorn
        if args.command=='hosting':
            require(app.settings.public_web_origin is not None,'hosting_origin_not_configured')
            from msg.extensions.hosting import hosting_app
            # The hosted origin exposes no operations, cookies, MCP or root routes.
            from contextlib import asynccontextmanager
            @asynccontextmanager
            async def lifespan(asgi):
                await app.load()
                yield
                await app.close()
            asgi=hosting_app(app);asgi.router.lifespan_context=lifespan
            uvicorn.run(asgi,host=args.listen,port=args.port,access_log=False,ws='none')
        else:
            from msg.transports.http import create_app
            uvicorn.run(create_app(app),host=app.settings.listen,port=app.settings.port,access_log=False,ws='none')
        return 0
    except (Failure,OSError,ValueError) as exc:
        code=exc.code if isinstance(exc,Failure) else 'local_operation_failed'
        print(canonical({'status':'error','error':{'code':code}}).decode(),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
