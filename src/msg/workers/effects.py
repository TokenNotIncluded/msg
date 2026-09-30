"""Durable external effects with leases, current authority and honest uncertainty.

Claiming a job is transactional; executing a network request is not. An expired
running lease is uncertain, never silently put back on the queue. The injected
runner is a test/installation boundary, not an alternative authentication path.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from msg.constants import ROOT_SUBJECT
from msg.core.codec import decode, wire
from msg.core.errors import Failure, require
from msg.core.models import CapabilityGrant, EmailSettings, ExecutionContext, Principal, ResourceRef
from msg.core.requests import request_for
from msg.core.tool_execution import ToolResult as ToolResult, ToolRunner
from msg.plugins.common import check_access, create_resource
from msg.security.network import intersect_policy
from msg.security.policy import scope_subset
from msg.security.quarantine import active as quarantine_active
from msg.workers.leases import current_attempt


async def current_principal(app, original: Principal, tx) -> Principal:
    """Intersect the captured credential with its *current* authority ceiling."""
    require(original.method not in {'anonymous', 'local'}, 'worker_identity_required')
    require(original.actor != ROOT_SUBJECT and original.subject != ROOT_SUBJECT, 'local_only')
    credential = await tx.credential(original.credential_id)
    now = app.clock()
    require(credential.subject_id == original.actor, 'credential_subject_mismatch')
    require(credential.revoked_at is None, 'credential_revoked')
    require(
        credential.not_before <= now
        and (credential.expires_at is None or now < credential.expires_at),
        'credential_expired',
    )
    for subject_id in {original.actor, original.subject}:
        subject = await tx.subject(subject_id)
        require(not subject.local_only, 'local_only')
    ceilings = []
    for captured in original.ceiling:
        for live in credential.ceiling:
            if (captured.capability, captured.version) != (live.capability, live.version):
                continue
            operations = captured.operations & live.operations
            if not operations:
                continue
            if await scope_subset(captured.scope, live.scope, tx):
                scope = captured.scope
            elif await scope_subset(live.scope, captured.scope, tx):
                scope = live.scope
            else:
                continue
            constraints = dict(captured.constraints)
            possible = True
            for name, value in live.constraints.items():
                if name not in constraints:
                    constraints[name] = value
                elif name in {'hosts', 'methods', 'ports', 'schemes'}:
                    combined = set(value) & set(constraints[name])
                    if not combined:
                        possible = False
                        break
                    constraints[name] = sorted(combined)
                elif name in {'timeout_ms', 'max_response_bytes', 'max_redirects'}:
                    constraints[name] = min(value, constraints[name])
                elif value != constraints[name]:
                    possible = False
                    break
            if possible:
                ceilings.append(
                    CapabilityGrant(
                        capability=captured.capability,
                        version=captured.version,
                        scope=scope,
                        operations=operations,
                        constraints=constraints,
                    )
                )
    require(ceilings, 'credential_ceiling')
    principal = replace(original, ceiling=tuple(ceilings))
    # Revocation, expiry, issuer rights and authority sources are all live checks.
    await app.authorizer.grants(principal, tx)
    return principal


def worker_context(app, job, principal):
    return ExecutionContext(
        request_id=job.arguments.get('request_id', job.id),
        principal=principal,
        entry='worker',
        now=app.clock(),
        deadline_monotonic=time.monotonic() + 60,
    )


def effect_request(app, job, principal):
    return request_for(
        job.operation,
        {},
        subject=principal.subject,
        service=app.settings.service_url,
        expires_at=app.clock() + timedelta(seconds=180),
        request_id=job.arguments.get('request_id', job.id),
    )


class EffectWorker:
    def __init__(
        self,
        app,
        *,
        tool_runner: ToolRunner | None = None,
        mail_sender=None,
        webhook_sender=None,
        lease_seconds=120,
    ):
        self.app = app
        self.stopping = lambda: False
        if tool_runner is None:
            from msg.workers.sandbox import BubblewrapRunner

            tool_runner = BubblewrapRunner(app)
        if mail_sender is None:
            from msg.workers.mail import SmtpSender

            mail_sender = SmtpSender(app.settings.server.mail)
        self.tool_runner, self.mail_sender = tool_runner, mail_sender
        if webhook_sender is None:
            from msg.workers.webhook import WebhookSender

            webhook_sender = WebhookSender()
        self.webhook_sender = webhook_sender
        self.lease_seconds = lease_seconds

    async def _claim(self):
        from msg.extensions.tools import tool_concurrency

        async with self.app.metadata.transaction(write=True) as tx:
            if self.stopping():
                return None, False
            self.app.runtime_generation.require_current(tx)
            if quarantine_active(tx):
                return None, False
            # Orphaned external work cannot be assumed not to have executed.
            running = {}
            for (raw_id,) in tx.execute(
                "SELECT id FROM jobs WHERE state='running' ORDER BY next_at,id"
            ):
                job = await tx.job(raw_id)
                if job.lease_until is None or job.lease_until <= self.app.clock():
                    await tx.save_job(replace(job, state='uncertain', lease_until=None))
                    tx.set_setting('job_status:' + job.id, {'code': 'expired_execution_lease'})
                    return job, False
                if job.kind == 'tool':
                    running[job.arguments['tool']['id']] = (
                        running.get(job.arguments['tool']['id'], 0) + 1
                    )
            # Other workers may hold this tool's jobs; only the running rows seen
            # under this write transaction are a shared fact, never a local lock.
            limits = {}
            for (raw_id,) in tx.execute(
                "SELECT id FROM jobs WHERE state='pending' AND next_at<=? ORDER BY next_at,id",
                (wire(self.app.clock()),),
            ):
                job = await tx.job(raw_id)
                if job.kind == 'tool':
                    ref = decode(ResourceRef, job.arguments['tool'])
                    if (ref.id, ref.revision) not in limits:
                        try:
                            limits[ref.id, ref.revision] = await tool_concurrency(self.app, tx, ref)
                        except Failure as exc:
                            if exc.retryable:
                                raise
                            await tx.save_job(replace(job, state='failed', lease_until=None))
                            tx.set_setting('job_status:' + job.id, {'code': exc.code})
                            return job, False
                    if running.get(ref.id, 0) >= limits[ref.id, ref.revision]:
                        continue
                job = replace(
                    job,
                    state='running',
                    attempts=job.attempts + 1,
                    lease_until=self.app.clock() + timedelta(seconds=self.lease_seconds),
                )
                await tx.save_job(job)
                return job, True
            return None, False

    async def _finish(self, job, state, code, *, status=None):
        async with self.app.metadata.transaction(write=True) as tx:
            self.app.runtime_generation.require_current(tx)
            current = await current_attempt(self.app, tx, job)
            if current is None:
                return
            await tx.save_job(replace(current, state=state, lease_until=None))
            tx.set_setting('job_status:' + job.id, {'code': code} if status is None else status)

    async def _retry(self, job, retry_code, exhausted_code):
        async with self.app.metadata.transaction(write=True) as tx:
            self.app.runtime_generation.require_current(tx)
            current = await current_attempt(self.app, tx, job)
            if current is None:
                return
            exhausted = current.attempts >= 8
            next_at = (
                current.next_attempt_at
                if exhausted
                else self.app.clock()
                + timedelta(seconds=min(3600, 30 * 2 ** (current.attempts - 1)))
            )
            await tx.save_job(
                replace(
                    current,
                    state='failed' if exhausted else 'pending',
                    lease_until=None,
                    next_attempt_at=next_at,
                )
            )
            tx.set_setting(
                'job_status:' + job.id, {'code': exhausted_code if exhausted else retry_code}
            )

    async def _retry_mail(self, job, code):
        await self._retry(job, code, code)

    async def _retry_webhook(self, job):
        await self._retry(job, 'webhook_retry_scheduled', 'webhook_attempts_exhausted')

    async def _tool(self, job, directory):
        from msg.core.models import NetworkPolicy
        from msg.extensions.tools import read_tool, tool_policies

        require(job.operation == 'tool.run', 'unknown_operation')
        async with self.app.metadata.transaction(write=False) as tx:
            self.app.runtime_generation.require_current(tx)
            principal = await current_principal(self.app, job.principal, tx)
            context, request = (
                worker_context(self.app, job, principal),
                effect_request(self.app, job, principal),
            )
            ref = decode(ResourceRef, job.arguments['tool'])
            await check_access(self.app, context, request, tx, ref.id, 'tool_use')
            tool = await read_tool(self.app, tx, ref.id, ref.revision)
            args = dict(job.arguments['input'])
            live = await tool_policies(self.app, principal, tool, args, tx)
            captured = tuple(decode(NetworkPolicy, p) for p in job.arguments['policies'])
            candidates = []
            for left in live:
                for right in captured:
                    try:
                        restrictions = {
                            name: getattr(right, name)
                            for name in (
                                'schemes',
                                'ports',
                                'methods',
                                'timeout_ms',
                                'max_response_bytes',
                                'max_redirects',
                            )
                        }
                        if right.hosts:
                            restrictions['hosts'] = right.hosts
                        policy = intersect_policy(left, restrictions)
                        candidates.append(
                            replace(
                                policy, allow_private=left.allow_private and right.allow_private
                            )
                        )
                    except Failure as exc:
                        if exc.code != 'network_policy_empty':
                            raise
            require(candidates, 'network_policy_denied')
            # Never send an internal content path or database reference to the
            # sandbox. Only a bounded, explicitly readable request body is copied.
            if 'body_ref' in args:
                body_ref = decode(ResourceRef, args.pop('body_ref'))
                await check_access(self.app, context, request, tx, body_ref.id, 'read')
                rev = await tx.revision(body_ref)
                input_path = directory / 'request-body'
                with input_path.open('xb') as output:
                    async for piece in self.app.contents.read(rev.content):
                        await asyncio.to_thread(output.write, piece)
                args['_body_path'] = str(input_path)
        result = await self.tool_runner(tool, args, tuple(candidates), directory)
        try:
            require(isinstance(result, ToolResult), 'invalid_tool_result')
            path = Path(result.path)
            require(
                path.parent.resolve() == directory.resolve()
                and path.is_file()
                and not path.is_symlink(),
                'invalid_tool_output_path',
            )
            require(
                path.stat().st_size <= max(p.max_response_bytes for p in candidates),
                'tool_response_too_large',
            )
            require(
                len(result.media_type) <= 255
                and '\r' not in result.media_type
                and '\n' not in result.media_type,
                'invalid_media_type',
            )

            async def pieces():
                with path.open('rb') as source:
                    while chunk := await asyncio.to_thread(source.read, 65536):
                        yield chunk

            blob = await self.app.contents.put(pieces(), result.media_type)
            await self.app.contents.pin(blob, 'job:' + job.id)
            async with self.app.metadata.transaction(write=True) as tx:
                self.app.runtime_generation.require_current(tx)
                current = await current_attempt(self.app, tx, job)
                if current is None:
                    return
                principal = await current_principal(self.app, job.principal, tx)
                context, request = (
                    worker_context(self.app, job, principal),
                    effect_request(self.app, job, principal),
                )
                await check_access(self.app, context, request, tx, ref.id, 'tool_use')
                await tool_policies(self.app, principal, tool, job.arguments['input'], tx)
                parent = tx.one(
                    "SELECT id FROM resources WHERE parent=? AND name='files'", (principal.subject,)
                )
                require(parent is not None, 'output_parent_missing')
                resource = await create_resource(
                    self.app,
                    context,
                    request,
                    tx,
                    parent=parent[0],
                    type='file',
                    name='tool-' + job.id,
                    body=blob,
                    media_type=blob.media_type,
                    mode=0o600,
                )
                output = ResourceRef(id=resource.id, revision=resource.revision)
                # A controlled authorization fact, not user-editable file metadata.
                tx.set_setting(
                    'tool_output:' + resource.id,
                    {'subject': principal.subject, 'tool_id': ref.id, 'job_id': job.id},
                )
                await tx.save_job(replace(current, state='done', result=output, lease_until=None))
                tx.set_setting(
                    'job_status:' + job.id,
                    {
                        'code': 'ok',
                        'size': blob.size,
                        'digest': blob.digest,
                        'media_type': blob.media_type,
                        'metadata': result.metadata,
                    },
                )
        except Failure as exc:
            # The external request already returned. A revoked credential or a
            # failed local commit does not prove that the external action failed.
            raise Failure('external_uncertain') from exc

    async def _mail(self, job):
        from msg.core.codec import loads, parse_time

        # Disabled means not even connecting, regardless of historical jobs.
        require(
            self.app.settings.server.mail is not None and self.app.settings.server.mail.enabled,
            'mail_disabled',
        )
        if job.arguments.get('order_notification'):
            from msg.market.delivery_notifications import project_notification

            async with self.app.metadata.transaction(write=False) as tx:
                self.app.runtime_generation.require_current(tx)
                principal = await current_principal(self.app, job.principal, tx)
                projected = await project_notification(self.app, tx, job, principal)
            state = await self.mail_sender.send(projected)
            require(state in {'sent', 'uncertain'}, 'invalid_delivery_result')
            await self._finish(job, 'done' if state == 'sent' else 'uncertain', state)
            return
        async with self.app.metadata.transaction(write=False) as tx:
            self.app.runtime_generation.require_current(tx)
            await current_principal(self.app, job.principal, tx)
            subject = job.arguments['recipient_subject']
            row = tx.one('SELECT body FROM emails WHERE subject=?', (subject,))
            require(row is not None, 'email_not_set')
            email = decode(EmailSettings, loads(row[0]))
            require(
                email.subject_id == subject and email.address == job.arguments['recipient'],
                'email_changed',
            )
            if job.arguments.get('verification'):
                challenge = tx.one(
                    'SELECT expires FROM email_challenges WHERE subject=?', (subject,)
                )
                require(
                    challenge is not None and parse_time(challenge[0]) > self.app.clock(),
                    'email_challenge_expired',
                )
            else:
                require(
                    email.verified_at is not None and job.operation in email.enabled_events,
                    'notification_disabled',
                )
        state = await self.mail_sender.send(job)
        require(state in {'sent', 'uncertain'}, 'invalid_delivery_result')
        await self._finish(job, 'done' if state == 'sent' else 'uncertain', state)

    async def _market_mail(self, job):
        from msg.market.email import render_verification
        from msg.market.targets import mail_enabled, render_notification

        require(mail_enabled(self.app), 'mail_disabled')
        async with self.app.metadata.transaction(write=False) as tx:
            self.app.runtime_generation.require_current(tx)
            await current_principal(self.app, job.principal, tx)
            outgoing = await (
                render_verification(self.app, tx, job)
                if job.kind == 'market_email_verify'
                else render_notification(self.app, tx, job)
            )
        state = await self.mail_sender.send(outgoing)
        require(state in {'sent', 'uncertain'}, 'invalid_delivery_result')
        await self._finish(job, 'done' if state == 'sent' else 'uncertain', state)

    async def _webhook(self, job):
        from msg.core.codec import canonical, decode, loads
        from msg.core.models import Event
        from msg.plugins.communication import WEBHOOK_DOMAIN_EVENTS, _webhook_subscription_key
        from msg.workers.webhook import open_secret, validate_endpoint

        require(
            job.operation in {'communication.send', 'communication.webhook_subscribe'},
            'invalid_webhook_event',
        )
        async with self.app.metadata.transaction(write=False) as tx:
            self.app.runtime_generation.require_current(tx)
            principal = await current_principal(self.app, job.principal, tx)
            recipient = job.arguments['recipient_subject']
            subject = await tx.subject(recipient)
            require(not subject.local_only, 'local_only')
            row = tx.one(
                """SELECT url,nonce,ciphertext,enabled,generation FROM webhook_endpoints
                          WHERE subject=?""",
                (recipient,),
            )
            require(
                row is not None and row[3] == 1 and row[4] == job.arguments['endpoint_generation'],
                'webhook_disabled',
            )
            if job.operation == 'communication.send':
                message = tx.one(
                    'SELECT sender,recipient,event_id FROM messages WHERE id=?',
                    (job.arguments['message_id'],),
                )
                require(
                    message is not None and message == (principal.subject, recipient, job.event_id),
                    'webhook_event_missing',
                )
                category = 'inbox.reference'
                reference = {}
            else:
                require(
                    principal.subject == recipient
                    and principal.actor == recipient
                    and principal.method == 'signature',
                    'webhook_subscription_owner',
                )
                rid = job.arguments['resource_id']
                scope_id = job.arguments['scope_id']
                category = job.arguments['category']
                subscription = tx.setting(_webhook_subscription_key(recipient, scope_id))
                require(
                    subscription is not None
                    and subscription.get('enabled')
                    and subscription.get('generation') == job.arguments['subscription_generation']
                    and subscription.get('endpoint_generation') == row[4]
                    and category in subscription.get('events', ())
                    and subscription['principal']['credential_id'] == principal.credential_id,
                    'webhook_subscription_disabled',
                )
                event_row = tx.one('SELECT body FROM events WHERE id=?', (job.event_id,))
                require(event_row is not None, 'webhook_event_missing')
                event = decode(Event, loads(event_row[0]))
                require(
                    WEBHOOK_DOMAIN_EVENTS.get(event.type) == category
                    and any(ref.id == rid for ref in event.resources),
                    'invalid_webhook_event',
                )
                scope = await tx.resource(scope_id)
                resource = await tx.resource(rid)
                require(
                    scope.owner == recipient
                    and scope.state == 'active'
                    and resource.state == 'active'
                    and (scope.id == resource.id or resource.parent == scope.id),
                    'webhook_scope_changed',
                )
                require(
                    await self.app.authorizer.has(
                        principal,
                        'webhook.domain',
                        'communication.webhook_subscribe@1',
                        scope.id,
                        tx,
                    ),
                    'capability_required',
                )
                context = worker_context(self.app, job, principal)
                request = effect_request(self.app, job, principal)
                await check_access(self.app, context, request, tx, scope.id, 'read')
                await check_access(self.app, context, request, tx, resource.id, 'read')
                reference = {'resource_id': rid}
            url = row[0]
            validate_endpoint(url)
            secret = open_secret(self.app, recipient, row[1], row[2])
        timestamp = str(int(self.app.clock().timestamp()))
        # Inbox carries no resource. Domain Event carries only a resource ID
        # after the current subscription, endpoint and ACL checks above.
        body = canonical({
            'event_id': job.event_id,
            'delivery_id': job.id,
            'timestamp': timestamp,
            'subject_id': recipient,
            'type': category,
            **reference,
        })
        state = await self.webhook_sender.send(
            url, secret, body, timestamp=timestamp, event_id=job.event_id, delivery_id=job.id
        )
        require(state in {'delivered', 'retry', 'failed'}, 'invalid_delivery_result')
        if state == 'retry':
            await self._retry_webhook(job)
        else:
            await self._finish(job, 'done' if state == 'delivered' else 'failed', state)

    async def run_once(self):
        if self.stopping():
            return False
        await self.app.executor.require_current_runtime()
        if self.app.executor.recovery_drill_active():
            return False
        if 'orders' in self.app.settings.server.plugins:
            from msg.market.escrow import resolve_due

            if await resolve_due(self.app):
                return True
            from msg.market.arbitration import resolve_cases

            if await resolve_cases(self.app):
                return True
        job, execute = await self._claim()
        if job is None:
            return False
        if not execute:
            return True
        try:
            # A queued effect must not outlive its installed operation contract.
            # Legacy jobs predate version recording and used the v1 worker path.
            version = job.arguments.get('contract_version', 1)
            require(type(version) is int and version >= 1, 'invalid_job_contract_version')
            self.app.registry.operation(job.operation, version)
            if job.kind == 'tool':
                self.app.settings.server.staging_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(
                    prefix='effect-', dir=self.app.settings.server.staging_dir
                ) as temp:
                    await self._tool(job, Path(temp))
            elif job.kind in {'market_mail', 'market_email_verify'}:
                await self._market_mail(job)
            elif job.kind == 'mail':
                await self._mail(job)
            elif job.kind == 'webhook':
                await self._webhook(job)
            elif job.kind == 'git.push':
                from msg.extensions.repositories import execute_push

                await execute_push(self.app, job)
            elif job.kind == 'maintenance':
                from msg.workers.maintenance import run_maintenance

                result = await run_maintenance(
                    self.app, job.arguments['action'], principal=job.principal
                )
                await self._finish(job, 'done', 'ok', status=result)
            elif job.kind == 'gc.resource':
                # Purge permission was consumed by the committed tombstone. This
                # idempotent cleanup cannot change another resource's retention.
                from msg.workers.maintenance import _collect

                async with self.app.metadata.transaction(write=True) as tx:
                    self.app.runtime_generation.require_current(tx)
                    resource = await tx.resource(job.arguments['id'])
                    require(resource.state == 'purged', 'purge_not_committed')
                    await _collect(self.app, tx)
                await self._finish(job, 'done', 'cleanup_requested')
            else:
                raise Failure('unknown_effect_kind')
        except Failure as exc:
            # Known pre-execution rejection is a definite failure. External calls
            # surface uncertain explicitly rather than inventing an exactly-once promise.
            if (
                job.kind in {'mail', 'market_mail', 'market_email_verify'}
                and exc.retryable
                and exc.code == 'mail_connection_failed'
            ):
                await self._retry_mail(job, exc.code)
                return True
            await self._finish(
                job,
                'uncertain' if exc.code in {'external_uncertain', 'job_lease_lost'} else 'failed',
                exc.code,
            )
        except asyncio.CancelledError:
            # Leave running with a lease: process cancellation may race an external
            # commit. The next worker marks the expired lease uncertain.
            raise
        except Exception:
            await self._finish(job, 'uncertain', 'external_uncertain')
        return True
