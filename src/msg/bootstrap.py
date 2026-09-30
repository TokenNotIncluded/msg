"""Versioned public bootstrap. Private-key provisioning lives in admin only."""

from __future__ import annotations

import base64
import re
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

from msg.constants import (
    ADMINS_GROUP as ADMINS_GROUP,
    CA_SPACE as CA_SPACE,
    CERT_SPACE as CERT_SPACE,
    CSR_SPACE as CSR_SPACE,
    ONLINE_CA as ONLINE_CA,
    PROTOCOL_VERSION as PROTOCOL_VERSION,
    PUBLIC_GROUP as PUBLIC_GROUP,
    ROOT_SPACE as ROOT_SPACE,
    ROOT_SUBJECT as ROOT_SUBJECT,
    TOOLS_SPACE as TOOLS_SPACE,
)
from msg.core.codec import canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.models import Organization, Resource, ResourceRef, Revision, Subject

RULE_NAMES = (
    'identity',
    'read-write',
    'auth',
    'topics',
    'files',
    'recovery',
    'security',
    'protocol',
)
SOURCE_HEADER = re.compile(r'<!-- rule_id: ([a-z][a-z0-9.\-]*); version: ([1-9][0-9]*) -->\n')
REQUIRES_HEADER = re.compile(r'<!-- requires_rules: ([^\n<>]*) -->')
ROOT_WEB_LOGO = files('msg.data').joinpath('logo.svg').read_text(encoding='utf-8')
# Original release page fingerprint is independent of later brand asset changes.
ROOT_WEB_LEGACY_DIGEST = 'sha256:b388bd34d1a64badd7844de6d44d8dacef0a51944f0e66e8334c12d71ce02f5b'
ROOT_WEB_SAMPLE = (
    files('msg.data')
    .joinpath('root-web.html')
    .read_text(encoding='utf-8')
    .replace('__LOGO__', ROOT_WEB_LOGO)
    .replace(
        '__SANS_FONT__',
        base64.b64encode(files('msg.data').joinpath('root-web-sans.woff2').read_bytes()).decode(),
    )
    .replace(
        '__SANS_BOLD_FONT__',
        base64.b64encode(
            files('msg.data').joinpath('root-web-sans-bold.woff2').read_bytes()
        ).decode(),
    )
).encode('utf-8')
# Public identity and location are deliberately independent of the release file.
# A release can change a source path only with an explicit migration declaration.
RULE_SPECS = (
    ('msg.bootstrap', 'r_agents', ROOT_SPACE, 'AGENTS.md', 'AGENTS.md', 1000),
    ('msg.rules.index', 'r_rule_index', 'r_rules', '_index.md', 'rules/_index.md', 2400),
    *(
        (
            f'msg.{name}',
            f'r_rule_{name.replace("-", "_")}',
            'r_rules',
            name,
            f'rules/{name}.md',
            4000,
        )
        for name in RULE_NAMES
    ),
)
RULE_PATHS = {
    rule_id: (
        '/AGENTS.md'
        if rid == 'r_agents'
        else '/_rules/_index.md'
        if rid == 'r_rule_index'
        else '/_rules/' + name
    )
    for rule_id, rid, _, name, _, _ in RULE_SPECS
}
# Release maintainers declare a relocation in both maps in the same change.
SOURCE_PATH_OVERRIDES = {}
SOURCE_MIGRATIONS = {}
# Release-only pins: rule_id -> {source_path: relative .md path, version: int,
# digest: sha256:<hex>}. Remove active links/operation dependencies in the same
# release; keep RULE_SPECS identity and this pin so upgrades preserve history.
# Retirement archives existing resources; fresh installs invent no old revision.
# No current rule is retired. A pin mismatch requires an explicit staged upgrade.
SOURCE_RETIREMENTS = {}


def system_source_root():
    packaged = files('msg.data').joinpath('system')
    require(packaged.is_dir(), 'system_source_missing')
    return packaged


async def sync_system_sources(
    tx,
    contents,
    now,
    *,
    source_root=None,
    namespace_root=ROOT_SPACE,
    source_paths=None,
    migrations=None,
    retirements=None,
    registry=None,
):
    """Sync each release-owned document independently; never delete a source implicitly."""
    root = Path(source_root or system_source_root())
    source_paths = dict(SOURCE_PATH_OVERRIDES if source_paths is None else source_paths)
    migrations = dict(SOURCE_MIGRATIONS if migrations is None else migrations)
    require(
        set(source_paths) <= RULE_PATHS.keys() and set(migrations) <= RULE_PATHS.keys(),
        'unknown_rule_id',
    )
    retirements = dict(SOURCE_RETIREMENTS if retirements is None else retirements)
    require(
        set(retirements) <= RULE_PATHS.keys() - {'msg.bootstrap', 'msg.rules.index'},
        'system_source_invalid_retirement',
    )
    require(
        not set(retirements) & (source_paths.keys() | migrations.keys()),
        'system_source_invalid_retirement',
    )
    for rule_id, pin in retirements.items():
        require(
            isinstance(pin, dict)
            and set(pin) == {'source_path', 'version', 'digest'}
            and isinstance(pin['source_path'], str)
            and pin['source_path'] == Path(pin['source_path']).as_posix()
            and not Path(pin['source_path']).is_absolute()
            and '..' not in Path(pin['source_path']).parts
            and pin['source_path'].endswith('.md')
            and type(pin['version']) is int
            and pin['version'] > 0
            and isinstance(pin['digest'], str)
            and re.fullmatch(r'sha256:[0-9a-f]{64}', pin['digest']) is not None,
            'system_source_invalid_retirement',
            rule_id,
        )
    if retirements:
        require(
            registry is not None
            and all(
                not set(spec.requires_rules) & retirements.keys() for spec in registry.operations()
            ),
            'system_source_retirement_referenced',
        )
    specs = []
    for rule_id, rid, parent, name, default_path, max_bytes in RULE_SPECS:
        if rule_id in retirements:
            continue
        relative = source_paths.get(rule_id, default_path)
        require(
            isinstance(relative, str)
            and relative.endswith('.md')
            and relative == Path(relative).as_posix()
            and not Path(relative).is_absolute()
            and '..' not in Path(relative).parts,
            'system_source_invalid_path',
            rule_id,
        )
        specs.append((
            relative,
            rid,
            namespace_root if parent == ROOT_SPACE else parent,
            name,
            rule_id,
            max_bytes,
        ))
    require(len({s[0] for s in specs}) == len(specs), 'duplicate_system_source_path')
    expected = {s[0] for s in specs}
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*.md')}
    for relative in sorted(actual):
        candidate = root / relative
        require(
            not candidate.is_symlink() and candidate.resolve().is_relative_to(root.resolve()),
            'system_source_invalid_path',
            relative,
        )
    observed = {}
    for relative in sorted(actual):
        match = SOURCE_HEADER.match((root / relative).read_text(encoding='utf-8'))
        require(match is not None, 'system_source_invalid_header', relative)
        rule_id = match.group(1)
        require(rule_id in RULE_PATHS, 'unknown_rule_id', relative)
        require(rule_id not in observed, 'duplicate_rule_id', rule_id)
        require(rule_id not in retirements, 'system_source_retirement_source_present', rule_id)
        observed[rule_id] = relative
    require(
        not expected - actual,
        'system_source_deleted_requires_migration',
        details={'paths': sorted(expected - actual)},
    )
    require(
        not actual - expected,
        'system_source_inventory_mismatch',
        details={'unknown': sorted(actual - expected)},
    )
    prepared = []
    rule_ids = set()
    for relative, rid, parent, name, expected_rule_id, max_bytes in specs:
        path = root / relative
        require(path.is_file() and not path.is_symlink(), 'system_source_missing', relative)
        raw = path.read_bytes()
        require(len(raw) <= max_bytes, 'system_source_too_large', relative)
        try:
            source_text = raw.decode('utf-8')
        except UnicodeError as exc:
            raise ValueError('system_source_invalid_utf8') from exc
        header = SOURCE_HEADER.match(source_text)
        require(header is not None, 'system_source_invalid_header', relative)
        rule_id, version = header.group(1), int(header.group(2))
        require(rule_id in RULE_PATHS and rule_id == expected_rule_id, 'unknown_rule_id', relative)
        require(rule_id not in rule_ids, 'duplicate_rule_id', rule_id)
        rule_ids.add(rule_id)
        note_match = re.search(r'<!-- change_note: ([^\n<>]{1,240}) -->', source_text)
        change_note = note_match.group(1) if note_match else None
        required = REQUIRES_HEADER.findall(source_text)
        require(
            len(required) == source_text.count('<!-- requires_rules:'),
            'dangling_requires_rules',
            rule_id,
        )
        require(len(required) <= 1, 'duplicate_requires_rules', rule_id)
        dependencies = tuple(part.strip() for part in required[0].split(',')) if required else ()
        require(
            not set(dependencies) & retirements.keys()
            and not any(
                re.search(re.escape(RULE_PATHS[retired]) + r'(?=[/#?\s)\]<>]|$)', source_text)
                for retired in retirements
            ),
            'system_source_retirement_referenced',
        )
        require(
            all(dep in RULE_PATHS and dep != rule_id for dep in dependencies)
            and len(set(dependencies)) == len(dependencies),
            'dangling_requires_rules',
            rule_id,
        )
        prepared.append((relative, rid, parent, name, rule_id, version, raw, change_note))
    require(rule_ids == RULE_PATHS.keys() - retirements.keys(), 'system_source_inventory_mismatch')
    retired = []
    for rule_id, pin in retirements.items():
        spec = next(spec for spec in RULE_SPECS if spec[0] == rule_id)
        rid = spec[1]
        old = tx.one(
            """SELECT source_path,source_version,source_digest,revision_id,source_kind,rule_id
            FROM system_sources WHERE resource_id=?""",
            (rid,),
        )
        if old is None:
            require(
                tx.one('SELECT id FROM resources WHERE id=?', (rid,)) is None,
                'system_source_retirement_mismatch',
                rule_id,
            )
            continue  # A fresh install has no historical revision to archive.
        require(
            tuple(old[:3]) == ('docs/system/' + pin['source_path'], pin['version'], pin['digest'])
            and old[4] in {'release', 'release_retired'}
            and old[5] == rule_id,
            'system_source_retirement_mismatch',
            rule_id,
        )
        resource = await tx.resource(rid)
        require(
            (
                resource.type,
                resource.parent,
                resource.name,
                resource.owner,
                resource.group,
                resource.mode,
            )
            == ('file', spec[2], spec[3], ROOT_SUBJECT, PUBLIC_GROUP, 0o444),
            'bootstrap_drift',
            rid,
        )
        require(resource.revision == old[3], 'system_source_pointer_drift')
        require(
            resource.state in {'active', 'archived'}
            and (old[4] != 'release_retired' or resource.state == 'archived'),
            'system_source_retirement_mismatch',
            rule_id,
        )
        retired.append((resource, old[0]))
    for rule_id, move in migrations.items():
        require(
            isinstance(move, (list, tuple))
            and len(move) == 2
            and all(isinstance(p, str) for p in move)
            and move[1]
            == source_paths.get(rule_id, next(s[4] for s in RULE_SPECS if s[0] == rule_id)),
            'system_source_invalid_migration',
            rule_id,
        )
        migrations[rule_id] = tuple(move)
    for rid, name, mode in (('r_rules', '_rules', '0555'), ('t_wiki', 'wiki', '1777')):
        await seed_resource(
            tx,
            contents,
            {
                'id': rid,
                'type': 'topic',
                'name': name,
                'parent': namespace_root,
                'owner': ROOT_SUBJECT,
                'group': PUBLIC_GROUP,
                'mode': mode,
            },
            now,
        )
        tx.execute(
            """INSERT INTO topic_settings (topic,membership_policy) VALUES (?,?)
            ON CONFLICT(topic) DO NOTHING""",
            (rid, 'open'),
            write=True,
        )
        tx.execute(
            """INSERT INTO topic_memberships
            (topic,subject,role,status,joined_at,invited_by) VALUES (?,?,?,?,?,?)
            ON CONFLICT(topic,subject) DO NOTHING""",
            (rid, ROOT_SUBJECT, 'admin', 'active', wire(now), None),
            write=True,
        )
    desired = set()
    for relative, rid, parent, name, rule_id, version, raw, change_note in prepared:
        source_path = 'docs/system/' + relative
        desired.add(source_path)
        resource = await seed_resource(
            tx,
            contents,
            {
                'id': rid,
                'type': 'file',
                'name': name,
                'parent': parent,
                'owner': ROOT_SUBJECT,
                'group': PUBLIC_GROUP,
                'mode': '0444',
            },
            now,
        )
        source_digest = digest(raw)
        old = tx.one(
            """SELECT source_path,rule_id,source_version,source_digest,revision_id,source_kind
            FROM system_sources WHERE resource_id=?""",
            (rid,),
        )
        if old is not None:
            require(old[5] != 'release_retired', 'system_source_retirement_required', rule_id)
            require(old[1] == rule_id, 'system_source_identity_drift')
            if old[0] != source_path:
                require(
                    migrations.get(rule_id) == (old[0].removeprefix('docs/system/'), relative),
                    'system_source_migration_required',
                    rule_id,
                )
            require(resource.revision == old[4], 'system_source_pointer_drift')
            if old[3] == source_digest:
                require(old[2] == version, 'system_source_version_drift')
                if old[0] != source_path:
                    tx.execute(
                        'UPDATE system_sources SET source_path=? WHERE resource_id=?',
                        (source_path, rid),
                        write=True,
                    )
                continue
            require(version > old[2], 'system_source_version_required')
        blob = await contents.put_bytes(raw, 'text/markdown')
        revision_id = 'v_rel_' + digest((rid, source_digest, version))[7:39]
        revision = Revision(
            format_version=1,
            id=revision_id,
            resource_id=rid,
            parents=(resource.revision,) if resource.revision else (),
            content=blob,
            relations=(),
            actor=ROOT_SUBJECT,
            subject=ROOT_SUBJECT,
            author=ROOT_SUBJECT,
            created_at=now,
            manifest_digest='',
            source_kind='release',
            source_version=version,
            source_digest=source_digest,
            change_note=change_note,
        )
        revision = replace(
            revision,
            manifest_digest=digest({
                k: v for k, v in wire(revision).items() if k not in {'signature', 'manifest_digest'}
            }),
        )
        await contents.pin(blob, revision_id)
        await contents.commit_revision(parent, revision)
        await tx.append_revision(revision)
        resource = replace(
            resource,
            generation=resource.generation + 1,
            revision=revision_id,
            modified_at=now,
            modified_by=ROOT_SUBJECT,
        )
        await tx.replace(resource, resource.generation - 1)
        if old is None:
            tx.execute(
                """INSERT INTO system_sources
                (resource_id,source_path,rule_id,source_kind,source_version,source_digest,revision_id)
                VALUES (?,?,?,?,?,?,?)""",
                (rid, source_path, rule_id, 'release', version, source_digest, revision_id),
                write=True,
            )
        else:
            tx.execute(
                """UPDATE system_sources SET source_path=?,source_kind='release',source_version=?,
                source_digest=?,revision_id=? WHERE resource_id=?""",
                (source_path, version, source_digest, revision_id, rid),
                write=True,
            )
    for resource, source_path in retired:
        desired.add(source_path)
        if resource.state != 'archived':
            await tx.replace(
                replace(
                    resource,
                    state='archived',
                    generation=resource.generation + 1,
                    modified_at=now,
                    modified_by=ROOT_SUBJECT,
                ),
                resource.generation,
            )
        tx.execute(
            "UPDATE system_sources SET source_kind='release_retired' WHERE resource_id=?",
            (resource.id,),
            write=True,
        )
    missing = {path for (path,) in tx.rows('SELECT source_path FROM system_sources')} - desired
    require(
        not missing, 'system_source_deleted_requires_migration', details={'paths': sorted(missing)}
    )


def manifest():
    return loads(files('msg.data').joinpath('bootstrap.json').read_bytes())


def feature_manifest(definition=None):
    """Validate the release inventory; a row is not proof that a feature works."""
    definition = manifest() if definition is None else definition
    resources = {row['id'] for row in definition['resources']}
    features = definition.get('features', ())
    require(isinstance(features, list) and bool(features), 'feature_manifest_invalid')
    seen = set()
    fields = {
        'feature_id',
        'enabled_by_default',
        'default_config',
        'sample_resource',
        'doctor_check',
        'selftest_case',
    }
    for feature in features:
        require(isinstance(feature, dict) and set(feature) == fields, 'feature_manifest_invalid')
        feature_id = feature['feature_id']
        require(
            isinstance(feature_id, str)
            and re.fullmatch(r'[a-z][a-z0-9_]*', feature_id) is not None
            and feature_id not in seen,
            'feature_manifest_invalid',
        )
        seen.add(feature_id)
        require(
            type(feature['enabled_by_default']) is bool
            and isinstance(feature['default_config'], dict),
            'feature_manifest_invalid',
        )
        require(
            feature['sample_resource'] is None or feature['sample_resource'] in resources,
            'feature_manifest_invalid',
        )
        require(
            all(
                feature[name] is None or (isinstance(feature[name], str) and bool(feature[name]))
                for name in ('doctor_check', 'selftest_case')
            ),
            'feature_manifest_invalid',
        )
    from msg.plugins.features import validate_feature_sources

    validate_feature_sources(features)
    return tuple(features)


async def seed_resource(tx, contents, data, now, body=None, media_type='text/markdown'):
    mode = data['mode']
    resource = Resource(
        **dict(
            data,
            mode=int(mode, 8) if isinstance(mode, str) else mode,
            type_version=1,
            generation=0,
            revision=None,
            state='active',
            created_at=now,
            created_by=ROOT_SUBJECT,
            modified_at=now,
            modified_by=ROOT_SUBJECT,
        )
    )
    found = tx.one('SELECT body FROM resources WHERE id=?', (resource.id,))
    if found is not None:
        previous = decode(Resource, loads(found[0]))
        legacy_tools_name = (
            resource.id == 't_tools' and previous.name == '_tools' and resource.name == 'tools'
        )
        fields = (
            ('type', 'parent', 'owner', 'group', 'mode')
            if legacy_tools_name
            else ('type', 'name', 'parent', 'owner', 'group', 'mode')
        )
        require(
            all(getattr(previous, n) == getattr(resource, n) for n in fields),
            'bootstrap_drift',
            resource.id,
        )
        return previous
    await tx.insert(resource)
    if body is not None:
        blob = await contents.put_bytes(
            body.encode() if isinstance(body, str) else body, media_type
        )
        revision_id = 'v_boot_' + digest((resource.id, blob.digest))[7:39]
        rev = Revision(
            format_version=1,
            id=revision_id,
            resource_id=resource.id,
            parents=(),
            content=blob,
            relations=(),
            actor=ROOT_SUBJECT,
            subject=ROOT_SUBJECT,
            author=ROOT_SUBJECT,
            created_at=now,
            manifest_digest='',
        )
        rev = replace(
            rev,
            manifest_digest=digest({
                k: v for k, v in wire(rev).items() if k not in {'signature', 'manifest_digest'}
            }),
        )
        await contents.pin(blob, revision_id)
        await contents.commit_revision(resource.parent or resource.id, rev)
        await tx.append_revision(rev)
        resource = replace(resource, generation=1, revision=rev.id)
        await tx.replace(resource, 0)
    return resource


async def sync_root_web_sample(tx, contents, now):
    """Update only the unchanged release sample; preserve custom deployments."""
    row = tx.one('SELECT body FROM resources WHERE id=?', ('w_root_web',))
    if row is None:
        return
    website = decode(Resource, loads(row[0]))
    if website.owner != ROOT_SUBJECT or website.state != 'active':
        return
    current = await tx.revision(ResourceRef(id=website.id))
    if current.id != 'v_boot_' + digest((website.id, current.content.digest))[7:39]:
        return
    manifest = loads(await contents.read_bytes(current.content))
    if manifest.get('deployment') != 't_root_web_deploy' or set(manifest.get('entries', {})) != {
        'index.html'
    }:
        return
    entry = manifest['entries']['index.html']
    if entry.get('id') != 'f_root_web_index':
        return
    file = await tx.resource('f_root_web_index')
    if (
        file.revision != entry.get('revision')
        or file.owner != ROOT_SUBJECT
        or file.state != 'active'
    ):
        return
    previous = await tx.revision(ResourceRef(id=file.id))
    # A release pointer must still be the deterministic bootstrap revision.
    if previous.id != 'v_boot_' + digest((file.id, previous.content.digest))[7:39]:
        return
    accepted = tx.setting('root_web_release_digest') or ROOT_WEB_LEGACY_DIGEST
    if previous.content.digest == digest(ROOT_WEB_SAMPLE):
        if tx.setting('root_web_release_digest') != previous.content.digest:
            tx.set_setting('root_web_release_digest', previous.content.digest)
        return
    if previous.content.digest != accepted:
        return

    async def update(resource, body, media_type):
        blob = await contents.put_bytes(body, media_type)
        revision_id = 'v_boot_' + digest((resource.id, blob.digest))[7:39]
        if tx.one('SELECT id FROM revisions WHERE id=?', (revision_id,)) is not None:
            # A downgrade reuses immutable history rather than reinserting its ID.
            revision = await tx.revision(ResourceRef(id=resource.id, revision=revision_id))
            require(
                revision.content == blob
                and revision.actor == ROOT_SUBJECT
                and revision.subject == ROOT_SUBJECT,
                'release_page_revision_corrupt',
            )
        else:
            revision = Revision(
                format_version=1,
                id=revision_id,
                resource_id=resource.id,
                parents=(resource.revision,),
                content=blob,
                relations=(),
                actor=ROOT_SUBJECT,
                subject=ROOT_SUBJECT,
                author=ROOT_SUBJECT,
                created_at=now,
                manifest_digest='',
                source_kind='release',
                source_version=2,
                source_digest=blob.digest,
                change_note='Update the packaged public welcome page',
            )
            revision = replace(
                revision,
                manifest_digest=digest({
                    k: v
                    for k, v in wire(revision).items()
                    if k not in {'signature', 'manifest_digest'}
                }),
            )
            await contents.pin(blob, revision.id)
            await contents.commit_revision(resource.parent or resource.id, revision)
            await tx.append_revision(revision)
        changed = replace(
            resource,
            revision=revision.id,
            generation=resource.generation + 1,
            modified_at=now,
            modified_by=ROOT_SUBJECT,
        )
        await tx.replace(changed, resource.generation)
        return revision.id

    revision = await update(file, ROOT_WEB_SAMPLE, 'text/html')
    manifest['entries']['index.html']['revision'] = revision
    await update(website, canonical(manifest), 'application/json')
    tx.set_setting('root_web_release_digest', digest(ROOT_WEB_SAMPLE))


async def bootstrap(store, contents, registry, now, *, selftest_run_id=None):
    definition = manifest()
    feature_manifest(definition)
    namespace_root = 't_selftest_' + selftest_run_id if selftest_run_id is not None else ROOT_SPACE
    async with store.transaction(write=True) as tx:
        for data in definition['resources']:
            if (
                selftest_run_id is not None
                and data['id'] != 'r_root'
                and data['parent'] == ROOT_SPACE
            ):
                data = dict(data, parent=namespace_root)
            registry.resource_type(data['type'], 1)
            body = None
            if data['type'] == 'tool':
                from msg.extensions.tools import descriptor

                body = canonical(descriptor(data['name'])).decode()
            elif data['id'] == 'r_honor_sample':
                body = canonical(definition['honor_sample']).decode()
            elif data['id'] == 'r_msg_entry_skill':
                body = MSG_ENTRY_SKILL
            await seed_resource(
                tx,
                contents,
                data,
                now,
                body,
                'application/json'
                if data['type'] == 'tool' or data['id'] == 'r_honor_sample'
                else 'text/markdown',
            )
            if selftest_run_id is not None and data['id'] == 'r_root':
                await seed_resource(
                    tx,
                    contents,
                    {
                        'id': 't_selftest',
                        'type': 'topic',
                        'name': '_test',
                        'parent': ROOT_SPACE,
                        'owner': ROOT_SUBJECT,
                        'group': ADMINS_GROUP,
                        'mode': '0711',
                    },
                    now,
                )
                await seed_resource(
                    tx,
                    contents,
                    {
                        'id': namespace_root,
                        'type': 'topic',
                        'name': selftest_run_id,
                        'parent': 't_selftest',
                        'owner': ROOT_SUBJECT,
                        'group': ADMINS_GROUP,
                        'mode': '0711',
                    },
                    now,
                )
        if selftest_run_id is None:
            # Stable bootstrap IDs make repeated starts idempotent. The hosting
            # read path uses these ordinary resources, not a special-case page.
            sample_blob_digest = digest(ROOT_WEB_SAMPLE)
            sample_revision = 'v_boot_' + digest(('f_root_web_index', sample_blob_digest))[7:39]
            manifest_body = canonical({
                'format_version': 1,
                'deployment': 't_root_web_deploy',
                'entries': {'index.html': {'id': 'f_root_web_index', 'revision': sample_revision}},
            })
            await seed_resource(
                tx,
                contents,
                {
                    'id': 'w_root_web',
                    'type': 'website',
                    'name': 'web',
                    'parent': 'u_root',
                    'owner': ROOT_SUBJECT,
                    'group': PUBLIC_GROUP,
                    'mode': '0755',
                },
                now,
                manifest_body,
                'application/json',
            )
            await seed_resource(
                tx,
                contents,
                {
                    'id': 't_root_web_deploy',
                    'type': 'topic',
                    'name': 'deploy-bootstrap',
                    'parent': 'w_root_web',
                    'owner': ROOT_SUBJECT,
                    'group': PUBLIC_GROUP,
                    'mode': '0755',
                },
                now,
            )
            await seed_resource(
                tx,
                contents,
                {
                    'id': 'f_root_web_index',
                    'type': 'file',
                    'name': 'index.html',
                    'parent': 't_root_web_deploy',
                    'owner': ROOT_SUBJECT,
                    'group': PUBLIC_GROUP,
                    'mode': '0644',
                },
                now,
                ROOT_WEB_SAMPLE,
                'text/html',
            )
        for data in definition['resources']:
            if data['type'] != 'topic':
                continue
            tx.execute(
                """INSERT INTO topic_settings (topic,membership_policy) VALUES (?,?)
                ON CONFLICT(topic) DO NOTHING""",
                (data['id'], 'open'),
                write=True,
            )
            tx.execute(
                """INSERT INTO topic_memberships
                (topic,subject,role,status,joined_at,invited_by) VALUES (?,?,?,?,?,?)
                ON CONFLICT(topic,subject) DO NOTHING""",
                (data['id'], ROOT_SUBJECT, 'admin', 'active', wire(now), None),
                write=True,
            )
        for id, kind, primary, local in (
            (ROOT_SUBJECT, 'system', ADMINS_GROUP, True),
            (ONLINE_CA, 'system', ADMINS_GROUP, False),
        ):
            if not tx.one('SELECT id FROM identities WHERE id=?', (id,)):
                await tx.update_identity(
                    Subject(
                        resource_id=id,
                        kind=kind,
                        primary_group=primary,
                        auth_version=0,
                        local_only=local,
                    ),
                    -1,
                )
        for id, builtin in ((PUBLIC_GROUP, 'public'), (ADMINS_GROUP, 'admins')):
            if not tx.one('SELECT id FROM identities WHERE id=?', (id,)):
                await tx.update_identity(
                    Organization(resource_id=id, membership_version=0, builtin=builtin), -1
                )
        for id, policy in definition['policies'].items():
            if tx.setting('policy:' + id) is None:
                tx.set_setting('policy:' + id, policy)
        for name, body in definition['templates'].items():
            resource = await seed_resource(
                tx,
                contents,
                {
                    'id': 'tpl_' + name,
                    'type': 'template',
                    'name': name,
                    'parent': 't_templates',
                    'owner': ROOT_SUBJECT,
                    'group': PUBLIC_GROUP,
                    'mode': '0644',
                },
                now,
                body,
                'application/msg-template',
            )
            tx.set_setting(f'template_version:{resource.id}:1', resource.revision)
        for key, schema in registry._schemas.items():
            await seed_resource(
                tx,
                contents,
                {
                    'id': key,
                    'type': 'file',
                    'name': key.removeprefix('schema:'),
                    'parent': 't_schema',
                    'owner': ROOT_SUBJECT,
                    'group': PUBLIC_GROUP,
                    'mode': '0444',
                },
                now,
                canonical(schema),
                'application/json',
            )
        await sync_system_sources(
            tx, contents, now, namespace_root=namespace_root, registry=registry
        )
        tx.set_setting(
            'bootstrap', {'version': definition['version'], 'digest': digest(definition)}
        )


MSG_ENTRY_SKILL = """# msg-entry

Read /AGENTS.md first, then the compact operation index at /-/d. Open only
the relevant namespace or operation detail at /-/d/<namespace>/<operation> and inspect
/-/schema when exact fields are needed. Ordinary paths are read-only.
Use the local `msg` client when available so it signs requests and preserves
request IDs. A skill explains how to call an operation; it grants no authority.
"""
