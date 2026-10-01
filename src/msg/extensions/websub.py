"""Public RSS publisher: committed, independent notifications for each hub."""

import time

import aiohttp

from msg.core.codec import digest
from msg.core.models import EffectJob, ExecutionContext, Principal
from msg.core.requests import request_for
from msg.plugins.common import new_id
from msg.workers.webhook import PublicResolver, validate_endpoint


async def enqueue_publication(app, tx, event):
    hubs = app.settings.websub_hubs
    if not hubs or not event.resources:
        return
    resources = [await tx.resource(ref.id) for ref in event.resources]
    if not any(resource.type in {'post', 'topic', 'space'} for resource in resources):
        return
    principal = Principal(
        actor=None,
        subject=None,
        credential_id=None,
        method='anonymous',
        certificates=(),
        ceiling=(),
    )
    request = request_for('discovery.feed', {}, app.settings.service_url)
    context = ExecutionContext(
        request_id=request.request_id,
        principal=principal,
        entry='network',
        now=event.time,
        deadline_monotonic=time.monotonic() + 30,
    )
    # The same anonymous visibility checks as the hub's subsequent GET. Never
    # send private IDs, actors, bodies, credentials or event data to a hub.
    output = await app.registry.operation('discovery.feed').handler(context, request, tx)
    fingerprint = digest(output.data['items'])
    previous = tx.setting('websub:public_feed', digest([]))
    if fingerprint == previous:
        return
    tx.set_setting('websub:public_feed', fingerprint)
    for hub in dict.fromkeys(hubs):
        await tx.enqueue(
            EffectJob(
                id=new_id('job'),
                event_id=event.id,
                kind='websub',
                dedupe_key=event.id + ':websub:' + digest(hub),
                principal=principal,
                operation='discovery.feed',
                arguments={'hub': hub, 'topic': app.settings.service_url + '/rss.xml'},
                state='pending',
                attempts=0,
                next_attempt_at=event.time,
                lease_until=None,
            )
        )


class WebSubSender:
    async def send(self, hub, topic):
        validate_endpoint(hub)
        connector = aiohttp.TCPConnector(
            resolver=PublicResolver(),
            use_dns_cache=False,
            ttl_dns_cache=0,
            limit=1,
        )
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as session:
                async with session.post(
                    hub,
                    data={'hub.mode': 'publish', 'hub.url': topic},
                    allow_redirects=False,
                ) as response:
                    if 200 <= response.status < 300:
                        return 'delivered'
                    if response.status in {408, 429} or response.status >= 500:
                        return 'retry'
                    return 'failed'
        except aiohttp.ClientError, TimeoutError, OSError:
            # Publish notifications are idempotent hints: retry after uncertain
            # delivery rather than permanently dropping an update.
            return 'retry'
