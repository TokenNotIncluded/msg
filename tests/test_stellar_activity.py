"""Rolling public activity is exact only after one complete ACL-filtered scan."""

from dataclasses import replace
from datetime import timedelta

from test_service import NOW, call, register

from msg.core.codec import canonical, wire
from msg.plugins.star_projection import unknown_activity
from msg.storage.session import RelationalSession


async def post_at(app, key, subject, created_at=NOW):
    result = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'public activity fixture'},
        key=key,
        subject=subject,
    )
    assert result.status == 'ok', result.error
    rid = result.resources[0].id
    if created_at != NOW:
        # Seed historical/future resource times without moving the signing
        # clock or changing the immutable current revision's actual author.
        async with app.metadata.transaction(write=True) as tx:
            resource = replace(await tx.resource(rid), created_at=created_at)
            tx.execute(
                'UPDATE resources SET created_at=?,body=? WHERE id=?',
                (wire(created_at), canonical(resource).decode(), rid),
                write=True,
            )
    return rid


async def star(app, subject):
    result = await call(app, 'discovery.get', {'id': subject, 'fields': ['star']})
    assert result.status == 'ok', result.error
    return result.data['star']


async def test_stellar_activity_empty_scan_is_observed_zero_not_unknown(installed):
    app, _ = installed
    _, subject, _ = await register(app, 'activity-empty')
    facts = await star(app, subject)
    assert facts['activity'] == {
        'window_days': 14,
        'window_end': wire(NOW),
        'recent_posts': 0,
        'previous_posts': 0,
        'active_days': 0,
        'previous_active_days': 0,
        'exact': True,
    }
    assert unknown_activity(NOW) == {
        'window_days': 14,
        'window_end': wire(NOW),
        'recent_posts': None,
        'previous_posts': None,
        'active_days': None,
        'previous_active_days': None,
        'exact': False,
    }


async def test_stellar_activity_seven_and_fourteen_day_boundaries_exclude_future(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'activity-boundaries')
    for age in (
        timedelta(),
        timedelta(hours=1),
        timedelta(days=1),
        timedelta(days=7) - timedelta(microseconds=1),
        timedelta(days=7),
        timedelta(days=13),
        timedelta(days=14),
        -timedelta(microseconds=1),
    ):
        await post_at(app, key, subject, NOW - age)
    facts = await star(app, subject)
    assert facts['post_count']['public'] == 7
    assert facts['last_public_post_at'] == wire(NOW)
    assert facts['activity'] == {
        'window_days': 14,
        'window_end': wire(NOW),
        'recent_posts': 4,
        'previous_posts': 2,
        'active_days': 3,
        'previous_active_days': 2,
        'exact': True,
    }


async def test_stellar_activity_distinguishes_one_day_burst_from_repeated_days(installed):
    app, _ = installed
    burst_key, burst, _ = await register(app, 'activity-burst')
    spread_key, spread, _ = await register(app, 'activity-spread')
    for index in range(8):
        await post_at(app, burst_key, burst, NOW - timedelta(hours=index))
    for day in range(4):
        await post_at(app, spread_key, spread, NOW - timedelta(days=day, hours=1))
    burst_activity = (await star(app, burst))['activity']
    spread_activity = (await star(app, spread))['activity']
    assert (burst_activity['recent_posts'], burst_activity['active_days']) == (8, 1)
    assert (spread_activity['recent_posts'], spread_activity['active_days']) == (4, 4)


async def test_stellar_activity_rechecks_hidden_posts_and_ancestors(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'activity-hidden')
    visible = await post_at(app, key, subject)
    hidden = await post_at(app, key, subject, NOW - timedelta(days=8))
    assert (await star(app, subject))['activity']['previous_posts'] == 1
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(hidden)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    activity = (await star(app, subject))['activity']
    assert (activity['recent_posts'], activity['previous_posts']) == (1, 0)
    async with app.metadata.transaction(write=True) as tx:
        topic = await tx.resource((await tx.resource(visible)).parent)
        await tx.replace(
            replace(topic, mode=0o700, generation=topic.generation + 1), topic.generation
        )
    activity = (await star(app, subject))['activity']
    assert activity['exact'] is True
    assert all(
        activity[name] == 0
        for name in ('recent_posts', 'previous_posts', 'active_days', 'previous_active_days')
    )


async def test_stellar_activity_uses_current_revision_author_and_original_resource_time(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'activity-author')
    _, new_owner, _ = await register(app, 'activity-new-owner')
    rid = await post_at(app, key, subject, NOW - timedelta(days=15))
    async with app.metadata.transaction(write=False) as tx:
        resource = await tx.resource(rid)
    edited = await call(
        app,
        'content.post_edit',
        {'id': rid, 'expected_revision': resource.revision, 'body': 'edited today'},
        key=key,
        subject=subject,
        expected=((rid, resource.generation),),
    )
    assert edited.status == 'ok', edited.error
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(rid)
        await tx.replace(
            replace(resource, owner=new_owner, generation=resource.generation + 1),
            resource.generation,
        )
    author_facts, owner_facts = await star(app, subject), await star(app, new_owner)
    assert author_facts['post_count']['public'] == 1
    assert owner_facts['post_count']['public'] == 0
    assert author_facts['activity']['recent_posts'] == 0
    assert author_facts['activity']['previous_posts'] == 0
    assert owner_facts['activity']['recent_posts'] == 0


async def test_stellar_activity_inexact_scan_never_projects_partial_window_as_zero(
    installed, monkeypatch
):
    app, _ = installed
    key, subject, _ = await register(app, 'activity-inexact')
    await post_at(app, key, subject)
    await post_at(app, key, subject, NOW - timedelta(days=8))
    monkeypatch.setattr('msg.plugins.star_projection.MAX_POST_COUNT_CANDIDATES', 1)
    facts = await star(app, subject)
    assert facts['post_count']['exact'] is False
    assert facts['activity'] == unknown_activity(NOW)
    assert facts['activity']['recent_posts'] is None


async def test_stellar_activity_batch_reuses_the_existing_single_post_scan(installed, monkeypatch):
    app, _ = installed
    key, subject, _ = await register(app, 'activity-single-scan')
    await post_at(app, key, subject)
    scans = []
    original_rows = RelationalSession.rows

    def rows(tx, sql, parameters=()):
        if 'FROM resources r JOIN revisions v ON v.id=r.revision' in sql:
            scans.append(sql)
        return original_rows(tx, sql, parameters)

    monkeypatch.setattr(RelationalSession, 'rows', rows)
    result = await call(app, 'discovery.get', {'id': subject, 'fields': ['star_batch']})
    assert result.status == 'ok', result.error
    assert result.data['star_batch']['stars'][subject]['activity']['recent_posts'] == 1
    assert len(scans) == 1
