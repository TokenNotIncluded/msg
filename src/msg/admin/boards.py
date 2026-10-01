"""Local Root board appointments, with signed audit history and preserved memberships."""

from dataclasses import replace

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, digest, wire
from msg.core.errors import require
from msg.core.models import AuditEvent, Event, ResourceRef
from msg.plugins.common import new_id, resolve


async def appoint_administrator(
    app, topic_id, subject_id, signer, *, replace_admins=False, operator
):
    require(signer.public_key == app.certificates.root_public_key, 'root_key_mismatch')
    async with app.metadata.transaction(write=True) as tx:
        topic = await tx.resource(await resolve(tx, topic_id))
        user = await tx.resource(await resolve(tx, subject_id))
        from msg.plugins.board_presentation import is_channel

        require(await is_channel(tx, topic) and topic.state == 'active', 'topic_not_active')
        require(user.type == 'user' and user.state == 'active', 'subject_not_active')
        subject = await tx.subject(user.id)
        require(
            subject.kind == 'registered' and not subject.local_only, 'topic_admin_subject_required'
        )
        now = app.clock()
        ban = tx.one(
            "SELECT expires_at FROM topic_bans WHERE topic=? AND subject=? AND status='active'",
            (topic.id, user.id),
        )
        from msg.core.codec import parse_time

        require(not ban or ban[0] is not None and parse_time(ban[0]) <= now, 'topic_banned')
        before = [
            row[0]
            for row in tx.rows(
                "SELECT subject FROM topic_memberships WHERE topic=? AND role='admin' AND status='active' ORDER BY subject",
                (topic.id,),
            )
        ]
        if replace_admins:
            tx.execute(
                "UPDATE topic_memberships SET role='member' WHERE topic=? AND role='admin'",
                (topic.id,),
                write=True,
            )
        tx.execute(
            """INSERT INTO topic_memberships(topic,subject,role,status,joined_at,invited_by)
                      VALUES (?,?,'admin','active',?,?) ON CONFLICT(topic,subject)
                      DO UPDATE SET role='admin',status='active',invited_by=excluded.invited_by""",
            (topic.id, user.id, wire(now), ROOT_SUBJECT),
            write=True,
        )
        await tx.replace(
            replace(
                topic, generation=topic.generation + 1, modified_at=now, modified_by=ROOT_SUBJECT
            ),
            topic.generation,
        )
        statement = {
            'topic': topic.id,
            'administrator': user.id,
            'replace_administrators': replace_admins,
            'before': before,
            'operator': operator,
            'time': wire(now),
        }
        signature = wire(signer.sign(canonical(statement), purpose='board-admin'))
        event = Event(
            id=new_id('audit'),
            type='root.topic.appoint',
            time=now,
            request_id=new_id('local'),
            actor=ROOT_SUBJECT,
            subject=ROOT_SUBJECT,
            resources=(ResourceRef(id=topic.id), ResourceRef(id=user.id)),
            data={**statement, 'signature': signature},
        )
        await tx.append_audit(
            AuditEvent(
                event=event,
                authority=(ResourceRef(id=app.certificates.root_certificate.resource_id),),
                before_digest=digest(before),
                after_digest=digest(statement),
                previous_digest=None,
                entry_digest='',
                result='appointed',
            )
        )
    return {
        'topic': topic.id,
        'administrator': user.id,
        'root_administrator': ROOT_SUBJECT,
        'replaced': replace_admins,
        'generation': topic.generation + 1,
    }
