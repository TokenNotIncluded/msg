"""msgd: local installation, serving, workers, diagnostics and restricted SSH."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import signal
import sys
from pathlib import Path

from msg import __version__
from msg.core.codec import canonical, wire
from msg.core.errors import Failure, require
from msg.paths import SERVER_CONFIG_DIR, SERVER_DATA_DIR


def emit(value):
    print(canonical(wire(value)).decode())


def network_runtime(settings):
    require(sys.version_info[:2] >= (3, 15), 'python_315_required')
    require(not hasattr(os, 'geteuid') or os.geteuid() != 0, 'network_service_must_not_run_as_root')
    # This is a permission probe, not a root material read or signer injection.
    require(
        not os.access(settings.config_dir / 'root', os.R_OK | os.X_OK),
        'root_material_accessible_to_service',
    )


def load_application(directory):
    from msg.application import Application
    from msg.config import load_settings

    return Application(load_settings(directory))


async def worker_loop(app, *, once=False):
    from msg.storage.valkey_bus import ValkeyOutboxSignal
    from msg.workers.effects import EffectWorker
    from msg.workers.maintenance import run_maintenance

    await app.load()
    worker = EffectWorker(app)
    wakeup = (
        ValkeyOutboxSignal(app.settings.server.valkey_url)
        if app.settings.server.valkey_url
        else None
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError, RuntimeError:
            pass
    next_cleanup = 0.0
    try:
        while not stop.is_set():
            executor = getattr(app, 'executor', None)
            if executor is not None and executor.recovery_drill_active():
                if once:
                    return {'processed': False, 'recovery_drill': True}
                try:
                    await asyncio.wait_for(stop.wait(), 1)
                except TimeoutError:
                    pass
                continue
            if loop.time() >= next_cleanup:
                await run_maintenance(app, 'cleanup_expired', scheduled=True)
                await run_maintenance(app, 'collect_garbage', scheduled=True)
                await run_maintenance(app, 'deliver_due_todos', scheduled=True)
                next_cleanup = loop.time() + 60
            processed = await worker.run_once()
            if once:
                return {'processed': processed}
            if not processed:
                if wakeup is not None:
                    try:
                        await wakeup.wait_for_pending(1)
                    except Exception:
                        # Pub/Sub is only a hint; durable jobs remain in PostgreSQL.
                        try:
                            await asyncio.wait_for(stop.wait(), 1)
                        except TimeoutError:
                            pass
                else:
                    try:
                        await asyncio.wait_for(stop.wait(), 1)
                    except TimeoutError:
                        pass
    finally:
        await app.close()


def parser():
    root = argparse.ArgumentParser(
        prog='msgd', description='Agent communication service and local administration'
    )
    root.add_argument('--version', action='version', version=__version__)
    root.add_argument('--config-dir', type=Path, default=SERVER_CONFIG_DIR)
    sub = root.add_subparsers(dest='command', required=True)
    init = sub.add_parser(
        'init', help='Initialize from the physical local console; no PIN arguments'
    )
    init.add_argument('--data-dir', type=Path, default=SERVER_DATA_DIR)
    init.add_argument('--service-url', default='https://msg.lmm.best')
    init.add_argument(
        '--allow-ssh',
        action='store_true',
        help='Allow an OS root SSH terminal; still requires interactive PIN input',
    )
    sub.add_parser('serve', help='Serve JSON/Markdown, operations, GraphQL and MCP')
    hosted = sub.add_parser(
        'hosting', help='Serve user-published files with the read-only hosting runtime'
    )
    hosted.add_argument('--listen', default='127.0.0.1')
    hosted.add_argument('--port', type=int, default=8043)
    worker = sub.add_parser('worker', help='Run the global durable effects/retention queue')
    worker.add_argument('--once', action='store_true')
    sub.add_parser('doctor', help='Read-only inspection; never repair or reinitialize')
    sub.add_parser('selftest', help='Run real end-to-end checks in a disposable installation')
    cert = sub.add_parser('cert')
    cs = cert.add_subparsers(dest='cert_command', required=True)
    issue = cs.add_parser('issue')
    issue.add_argument('csr_id')
    issue.add_argument(
        '--allow-ssh',
        action='store_true',
        help='Allow an OS root SSH terminal for this issuance only',
    )
    revoke = cs.add_parser('revoke')
    revoke.add_argument('certificate_id')
    revoke.add_argument('--reason', required=True)
    account = sub.add_parser('account', help='Root-approved account lifecycle administration')
    account_sub = account.add_subparsers(dest='account_command', required=True)
    archive = account_sub.add_parser(
        'archive', help='Revoke account access and archive its profile; preserve history'
    )
    archive.add_argument('subject_id')
    archive.add_argument(
        '--allow-ssh',
        action='store_true',
        help='Allow an OS root SSH terminal for this archival only; requires PIN and exact preview confirmation',
    )
    rootkey = sub.add_parser('root')
    rs = rootkey.add_subparsers(dest='root_command', required=True)
    rs.add_parser('change-pin')
    rotate = rs.add_parser('rotate')
    rotate.add_argument('--lost-key', action='store_true')
    rotate.add_argument('--resume', action='store_true')
    rb = rs.add_parser('backup')
    rb.add_argument('destination', type=Path)
    rr = rs.add_parser('recover')
    rr.add_argument('source', type=Path)
    proof = rs.add_parser(
        'recovery-proof', help='Physical-console complete-state recovery proof and promotion'
    )
    proof_sub = proof.add_subparsers(dest='proof_command', required=True)
    proof_sign = proof_sub.add_parser('sign')
    proof_sign.add_argument('source_backup_sha256')
    proof_sign.add_argument('sequence', type=int)
    proof_sign.add_argument('destination', type=Path)
    proof_promote = proof_sub.add_parser('promote')
    proof_promote.add_argument('source', type=Path)
    proof_promote.add_argument('independent_trust', type=Path)
    retirement = rs.add_parser(
        'backup-retirement', help='Physical-console attestation for listed backup sets'
    )
    retirement_sub = retirement.add_subparsers(dest='retirement_command', required=True)
    retirement_sign = retirement_sub.add_parser('sign')
    retirement_sign.add_argument('source', type=Path)
    retirement_sign.add_argument('destination', type=Path)
    retirement_sub.add_parser('import').add_argument('source', type=Path)
    money = sub.add_parser('money', help='Interactive Root central bank administration')
    ms = money.add_subparsers(dest='money_command', required=True)
    mint = ms.add_parser('mint')
    mint.add_argument('amount')
    burn = ms.add_parser('burn')
    burn.add_argument('amount')
    bank = ms.add_parser('bank')
    bs = bank.add_subparsers(dest='bank_command', required=True)
    bank_add = bs.add_parser('add')
    bank_add.add_argument('subject_id')
    bank_remove = bs.add_parser('remove')
    bank_remove.add_argument('subject_id')
    bank_fund = bs.add_parser(
        'fund', help='Confirm BankRole and Root funding separately, commit together'
    )
    bank_fund.add_argument('subject_id')
    bank_fund.add_argument('amount')
    transfer = ms.add_parser('transfer')
    transfer.add_argument('--from', dest='from_subject', required=True, choices=['@root'])
    transfer.add_argument('--to', dest='to_subject', required=True)
    transfer.add_argument('amount')
    for command in (mint, burn, bank_add, bank_remove, bank_fund, transfer):
        command.add_argument(
            '--allow-ssh',
            action='store_true',
            help='Allow an OS root SSH terminal for this action only; requires PIN and exact confirmation',
        )
    offer = ms.add_parser('offer')
    ops = offer.add_subparsers(dest='offer_command', required=True)
    offer_set = ops.add_parser('set')
    offer_set.add_argument('offer_id')
    offer_set.add_argument('--resource-kind', required=True)
    offer_set.add_argument('--entitlement-kind', required=True)
    offer_set.add_argument('--unit', required=True)
    offer_set.add_argument('--price', required=True)
    offer_set.add_argument('--min-quantity', type=int, required=True)
    offer_set.add_argument('--max-quantity', type=int, required=True)
    offer_set.add_argument('--duration-seconds', type=int)
    ops.add_parser('disable').add_argument('offer_id')
    offer_import = ops.add_parser(
        'import', help='Explicitly adopt one legacy offer as a signed Listing'
    )
    offer_import.add_argument('offer_id')
    offer_import.add_argument('--dry-run', action='store_true')
    market = sub.add_parser('market', help='Physical-console arbitration configuration')
    market_sub = market.add_subparsers(dest='market_command', required=True)
    market_sub.add_parser('grant').add_argument('subject_id')
    market_sub.add_parser('revoke').add_argument('subject_id')
    market_sub.add_parser('publish').add_argument('policy_file', type=Path)
    backup = sub.add_parser(
        'backup', help='Local service-data backup, excluding root private material'
    )
    backup.add_argument('destination', type=Path)
    restore = sub.add_parser('restore', help='Restore service data into new directories only')
    restore.add_argument('source', type=Path)
    restore.add_argument('--data-dir', type=Path, required=True)
    auth = sub.add_parser('ssh-authorized-key', help=argparse.SUPPRESS)
    auth.add_argument('key_type')
    auth.add_argument('key_body')
    ssh = sub.add_parser('ssh-session', help=argparse.SUPPRESS)
    ssh.add_argument('--credential', required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    server_modules = ('starlette', 'uvicorn', 'psycopg', 'valkey', 'aiohttp', 'dns', 'graphql')
    if any(importlib.util.find_spec(name) is None for name in server_modules):
        print(
            canonical({
                'status': 'error',
                'error': {
                    'code': 'server_dependencies_required',
                    'hint': 'Install msgctl[server] before running server commands.',
                },
            }).decode(),
            file=sys.stderr,
        )
        return 2
    try:
        if args.command in {'doctor', 'selftest'}:
            from msg.admin.diagnostics import doctor, selftest

            result = (
                doctor(args.config_dir) if args.command == 'doctor' else asyncio.run(selftest())
            )
            emit(result)
            return 0 if result['ok'] else 1
        if args.command == 'init':
            from msg.config import write_example

            # An unattended invocation stops before creating any trust material.
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                emit({
                    'status': 'needs_initialization',
                    'command': 'msgd init',
                    'code': 'interactive_pin_required',
                })
                return 78
            require(hasattr(os, 'geteuid') and os.geteuid() == 0, 'local_os_administrator_required')
            args.config_dir.mkdir(parents=True, exist_ok=True)
            from msg.admin.root import RootAdmin, require_local_console, require_ssh_administrator

            check = require_ssh_administrator if args.allow_ssh else require_local_console
            check(args.config_dir)
            write_example(args.config_dir, args.data_dir, args.service_url)
            result = RootAdmin(args.config_dir, allow_ssh=args.allow_ssh).initialize()
            emit({'root_id': result})
            return 0
        if args.command in {'cert', 'root', 'account'}:
            from msg.admin.root import RootAdmin

            admin = RootAdmin(args.config_dir, allow_ssh=getattr(args, 'allow_ssh', False))
            if args.command == 'account':
                result = admin.archive_account(args.subject_id)
            elif args.command == 'cert':
                result = (
                    admin.issue(args.csr_id)
                    if args.cert_command == 'issue'
                    else admin.revoke(args.certificate_id, args.reason)
                )
            elif args.root_command == 'change-pin':
                admin.change_pin()
                result = {'status': 'pin_changed'}
            elif args.root_command == 'rotate':
                result = admin.rotate(lost_key=args.lost_key, resume=args.resume)
            elif args.root_command == 'backup':
                result = admin.backup(args.destination)
            elif args.root_command == 'recovery-proof':
                result = (
                    admin.sign_recovery_proof(
                        args.source_backup_sha256, args.sequence, args.destination
                    )
                    if args.proof_command == 'sign'
                    else admin.promote_recovery(args.source, args.independent_trust)
                )
            elif args.root_command == 'backup-retirement':
                result = (
                    admin.sign_backup_retirement(args.source, args.destination)
                    if args.retirement_command == 'sign'
                    else admin.import_backup_retirement(args.source)
                )
            else:
                result = admin.recover(args.source)
            emit(result)
            return 0
        if args.command == 'market':
            from msg.admin.market import MarketAdmin

            emit(
                MarketAdmin(args.config_dir).execute(
                    args.market_command,
                    subject=getattr(args, 'subject_id', None),
                    policy_file=getattr(args, 'policy_file', None),
                )
            )
            return 0
        if args.command == 'money':
            from msg.admin.money import MoneyAdmin

            if args.money_command == 'offer':
                fields = (
                    {
                        'resource_kind': args.resource_kind,
                        'entitlement_kind': args.entitlement_kind,
                        'unit': args.unit,
                        'price': args.price,
                        'min_quantity': args.min_quantity,
                        'max_quantity': args.max_quantity,
                        'duration_seconds': args.duration_seconds,
                    }
                    if args.offer_command == 'set'
                    else None
                )
                emit(
                    MoneyAdmin(args.config_dir).execute_offer(
                        args.offer_command,
                        offer_id=args.offer_id,
                        fields=fields,
                        **({'dry_run': args.dry_run} if args.offer_command == 'import' else {}),
                    )
                )
                return 0
            if args.money_command == 'bank':
                action = 'bank_' + args.bank_command
                amount = args.amount if args.bank_command == 'fund' else None
                subject_id = args.subject_id
            elif args.money_command == 'transfer':
                action = 'transfer'
                amount = args.amount
                subject_id = args.to_subject
            else:
                action = args.money_command
                amount = args.amount
                subject_id = None
            emit(
                MoneyAdmin(args.config_dir).execute(
                    action,
                    amount=amount,
                    subject_id=subject_id,
                    allow_ssh=getattr(args, 'allow_ssh', False),
                )
            )
            return 0
        if args.command == 'restore':
            from msg.admin.backups import restore

            emit(restore(args.source, args.config_dir, args.data_dir))
            return 0
        if args.command == 'ssh-authorized-key':
            from msg.extensions.ssh import authorized_key

            print(asyncio.run(authorized_key(args.config_dir, args.key_type, args.key_body)))
            return 0
        if args.command == 'ssh-session':
            from msg.extensions.ssh import forced_session

            return asyncio.run(forced_session(args.config_dir, args.credential))
        if args.command == 'hosting':
            from contextlib import asynccontextmanager

            import uvicorn

            from msg.config import load_settings
            from msg.extensions.hosting import hosting_app
            from msg.hosting_runtime import HostingRuntime

            settings = load_settings(args.config_dir)
            network_runtime(settings)
            app = HostingRuntime(settings)

            @asynccontextmanager
            async def lifespan(asgi):
                try:
                    await app.load()
                    yield
                finally:
                    await app.close()

            asgi = hosting_app(app)
            asgi.router.lifespan_context = lifespan
            uvicorn.run(asgi, host=args.listen, port=args.port, access_log=False, ws='none')
            return 0
        app = load_application(args.config_dir)
        if args.command == 'backup':
            from msg.admin.backups import backup

            async def save():
                await app.load()
                try:
                    return await backup(app, args.destination)
                finally:
                    await app.close()

            emit(asyncio.run(save()))
            return 0
        network_runtime(app.settings)
        if args.command == 'worker':
            result = asyncio.run(worker_loop(app, once=args.once))
            if result is not None:
                emit(result)
            return 0
        import uvicorn

        from msg.transports.http import create_app

        uvicorn.run(
            create_app(app),
            host=app.settings.listen,
            port=app.settings.port,
            access_log=False,
            ws='none',
        )
        return 0
    except (Failure, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, Failure) else 'local_operation_failed'
        print(canonical({'status': 'error', 'error': {'code': code}}).decode(), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
