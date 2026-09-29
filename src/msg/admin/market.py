"""Root-signed, physical-console arbitration configuration; never a network API."""

from __future__ import annotations

import asyncio
import getpass

from msg.admin.money import _root_signer
from msg.admin.root import RootAdmin, require_local_console, root_envelope
from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, digest, loads, wire
from msg.core.errors import require
from msg.core.models import AuditEvent, Event, ResourceRef
from msg.market.policy import DEFAULT_POLICY, validate
from msg.plugins.common import new_id
from msg.security.crypto import Ed25519Signer, open_private_key


async def apply_market(app, signer, *, action, operator, subject=None, policy=None, expected=None):
    _root_signer(app, signer)
    require(action in {'grant', 'revoke', 'publish'}, 'market_admin_action_invalid')
    if action == 'publish':
        policy = validate(policy)
        require(policy['id'] != DEFAULT_POLICY['id'], 'builtin_policy_immutable')
    async with app.metadata.transaction(write=True) as tx:
        if action == 'publish':
            require(
                tx.one('SELECT 1 FROM arbitration_policies WHERE id=?', (policy['id'],)) is None,
                'arbitration_policy_exists',
            )
            for candidate in policy['candidates']:
                identity = await tx.subject(candidate)
                require(
                    identity.kind == 'registered' and not identity.local_only, 'invalid_arbitrator'
                )
            tx.execute(
                'INSERT INTO arbitration_policies(id,body,digest) VALUES (?,?,?)',
                (policy['id'], canonical(policy).decode(), digest(policy)),
                write=True,
            )
            before, after = None, policy
        else:
            identity = await tx.subject(subject)
            require(
                identity.kind == 'registered'
                and not identity.local_only
                and subject != ROOT_SUBJECT,
                'invalid_arbitrator',
            )
            row = tx.one('SELECT epoch,active FROM arbitrator_roles WHERE subject=?', (subject,))
            before = {'epoch': row[0], 'active': row[1]} if row else None
            if expected is not None:
                require(before == expected, 'market_preview_stale')
            require(
                (action == 'grant' and (not row or not row[1]))
                or (action == 'revoke' and row and row[1]),
                'arbitrator_role_unchanged',
            )
            after = {
                'epoch': new_id('role') if action == 'grant' else row[0],
                'active': action == 'grant',
            }
            tx.execute(
                """INSERT INTO arbitrator_roles(subject,epoch,active) VALUES (?,?,?)
                ON CONFLICT(subject) DO UPDATE SET epoch=excluded.epoch,active=excluded.active""",
                (subject, after['epoch'], after['active']),
                write=True,
            )
        statement = {
            'action': action,
            'subject': subject,
            'before': before,
            'after': after,
            'operator': operator,
            'at': wire(app.clock()),
        }
        proof = signer.sign(canonical(statement), purpose='market-admin')
        event = Event(
            id=new_id('audit'),
            type='root.market.' + action,
            time=app.clock(),
            request_id=new_id('local-market'),
            actor=ROOT_SUBJECT,
            subject=ROOT_SUBJECT,
            resources=(ResourceRef(id=ROOT_SUBJECT),),
            data={**statement, 'root_signature': wire(proof)},
        )
        await tx.append_event(event)
        await tx.append_audit(
            AuditEvent(
                event=event,
                authority=(ResourceRef(id=app.certificates.root_certificate.resource_id),),
                before_digest=digest(before),
                after_digest=digest(after),
                previous_digest=None,
                entry_digest='',
                result=action,
            )
        )
    return {'action': action, 'audit_event_id': event.id, 'result': after}


class MarketAdmin:
    def __init__(self, config_dir):
        self.config_dir = config_dir

    def execute(self, action, *, subject=None, policy_file=None):
        operator = require_local_console(self.config_dir)
        app = RootAdmin(self.config_dir)._app()

        async def run():
            await app.load()
            try:
                policy = loads(policy_file.read_bytes()) if policy_file is not None else None
                resolved, before = subject, None
                async with app.metadata.transaction(write=False) as tx:
                    if subject and subject.startswith('@'):
                        resolved = await tx.resolve('/' + subject)
                    if resolved:
                        row = tx.one(
                            'SELECT epoch,active FROM arbitrator_roles WHERE subject=?', (resolved,)
                        )
                        before = {'epoch': row[0], 'active': row[1]} if row else None
                preview = {
                    'action': action,
                    'subject': resolved,
                    'before': before,
                    'policy': policy,
                }
                print(canonical(preview).decode())
                approval = 'CONFIRM MARKET ' + digest(preview)
                require(
                    input('Type ' + approval + ' to continue: ') == approval, 'approval_cancelled'
                )
                pin = getpass.getpass('Root PIN/passphrase: ')
                signer = Ed25519Signer.from_bytes(
                    open_private_key(loads(root_envelope(self.config_dir).read_bytes()), pin)
                )
                return await apply_market(
                    app,
                    signer,
                    action=action,
                    operator=operator,
                    subject=resolved,
                    policy=policy,
                    expected=before,
                )
            finally:
                await app.close()

        return asyncio.run(run())
