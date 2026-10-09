"""Real Python/Rust content interoperability on disposable, shared directories.

Not an authenticated executor, full server acceptance or a recovery journal test.
All test identities, paths and hooks are synthetic. No production service access.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import selectors
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from msg.core.codec import canonical, decode, wire
from msg.core.models import BlobRef, Revision
from msg.storage.git import GitContentStore

REPORT = []
NOW = datetime(2026, 10, 6, tzinfo=UTC)


def check(condition, name):
    if not condition:
        raise AssertionError(name)
    REPORT.append(name)


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


class Native:
    def __init__(self, binary, root, *, env=None):
        root.mkdir(exist_ok=True)
        (root / '.msg-native-content-test').write_bytes(b'disposable test content\n')
        self.process = subprocess.Popen(
            [str(binary), str(root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    def send(self, **value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=20):
                self.stop(kill=True)
                raise AssertionError('native content fixture timed out')
        line = self.process.stdout.readline()
        if not line:
            self.process.wait(timeout=5)
            raise AssertionError(self.process.stderr.read())
        return json.loads(line)

    def call(self, **value):
        result = self.send(**value)
        if 'error' in result:
            raise AssertionError(result)
        return result['data']

    def stop(self, *, kill=False):
        if self.process.poll() is None:
            if kill:
                self.process.kill()
            else:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
                raise
        for stream in [self.process.stdin, self.process.stdout, self.process.stderr]:
            stream.close()


def revision(blob, name, parents=()):
    return Revision(
        format_version=1,
        id=name,
        resource_id='r_content',
        parents=list(parents),
        content=blob,
        relations=[],
        actor='u_alice',
        subject='u_alice',
        author='u_alice',
        created_at=NOW,
        manifest_digest='sha256:' + 'a' * 64,
        signature=None,
    )


def git(store, *args):
    return subprocess.run(
        ['git', '--git-dir', str(store.repo), *args],
        check=True,
        capture_output=True,
        env=store.env,
        timeout=20,
    ).stdout


async def interoperate(binary, root):
    store = GitContentStore(root)
    native = Native(binary, root)
    try:
        kinds = [
            'text/markdown',
            'application/json',
            'application/msg-template',
            'application/octet-stream',
        ]
        payloads = [b'', 'Hello\0中文\n'.encode(), bytes(range(251)) * 600]
        for index, media in enumerate(kinds):
            for number, data in enumerate(payloads):
                label = f'{index}-{number}'
                py_blob = await store.put_bytes(data, media)
                check(
                    native.call(action='read', blob=wire(py_blob), range=None) == b64(data),
                    'python-to-rust-' + label,
                )
                native_blob = native.call(
                    action='put', bytes=b64(data), media_type=media, expected=None
                )
                check(native_blob == wire(py_blob), 'blob-ref-' + label)
                check(
                    await store.read_bytes(decode(BlobRef, native_blob)) == data,
                    'rust-to-python-' + label,
                )
                index_path = store.index / py_blob.digest.removeprefix('sha256:')
                entry = json.loads(index_path.read_bytes())
                if entry['kind'] == 'git':
                    check(
                        git(store, 'cat-file', 'blob', entry['oid']) == data, 'git-object-' + label
                    )
                else:
                    check(
                        (store.binary / py_blob.digest.removeprefix('sha256:')).read_bytes()
                        == data,
                        'binary-object-' + label,
                    )
                lease = '../../opaque/lease中文'
                check(
                    native.call(action='pin', blob=native_blob, lease=lease), 'native-pin-' + label
                )
                check(await store.pinned(py_blob, lease), 'python-sees-pin-' + label)
                await store.unpin(py_blob, lease)
                check(
                    not native.call(action='pinned', blob=native_blob, lease=lease),
                    'native-sees-unpin-' + label,
                )
                await store.pin(py_blob, lease)
                check(
                    not native.call(action='unpin', blob=native_blob, lease=lease),
                    'native-unpin-' + label,
                )
                check(not await store.pinned(py_blob, lease), 'python-sees-unpin-' + label)
                ranges = [(0, len(data)), (len(data), len(data))]
                if len(data) > 65536:
                    ranges += [(65532, 65540), (5, 70000)]
                for begin, end in ranges:
                    py_bytes = b''.join([
                        chunk async for chunk in store.read(py_blob, (begin, end))
                    ])
                    check(
                        native.call(action='read', blob=native_blob, range=[begin, end])
                        == b64(py_bytes),
                        f'range-{label}-{begin}-{end}',
                    )
        for media in ['text/plain', 'application/octet-stream']:
            blob = await store.put_bytes(b'parent', media)
            suffix = 'text' if media.startswith('text') else 'binary'
            parent = revision(blob, 'v_python_' + suffix)
            parent_oid = await store.commit_revision('t_fixture', parent)
            check(
                native.call(action='revision', topic='t_fixture', revision=wire(parent))
                == parent_oid,
                'reuse-python-commit-' + suffix,
            )
            child = revision(blob, 'v_rust_' + suffix, [parent.id])
            child_oid = native.call(action='revision', topic='t_fixture', revision=wire(child))
            check(
                git(store, 'show', f'{child_oid}:manifest.json') == canonical(child),
                'canonical-manifest-' + suffix,
            )
            check(
                git(store, 'rev-parse', f'{child_oid}^').strip().decode() == parent_oid,
                'native-parent-' + suffix,
            )
            descendant = revision(blob, 'v_python_after_' + suffix, [child.id])
            descendant_oid = await store.commit_revision('t_fixture', descendant)
            check(
                git(store, 'rev-parse', f'{descendant_oid}^').strip().decode() == child_oid,
                'python-parent-' + suffix,
            )
            conflict = replace(child, author='u_different')
            check(
                native.send(action='revision', topic='t_fixture', revision=wire(conflict))
                == {'error': 'revision_content_conflict'},
                'immutable-revision-' + suffix,
            )
            check(
                native.call(action='revision', topic='t_fixture', revision=wire(child))
                == child_oid,
                'retry-native-commit-' + suffix,
            )
        large = native.call(
            action='generated',
            length=4_194_305,
            media_type='application/octet-stream',
            hold_during=False,
            hold_after=False,
        )
        expected = 'sha256:' + hashlib.sha256(b'x' * large['size']).hexdigest()
        check(
            native.call(action='hash', blob=large) == {'digest': expected, 'size': large['size']},
            'streaming-native-large-blob',
        )
        py_hash = hashlib.sha256()
        async for chunk in store.read(decode(BlobRef, large)):
            py_hash.update(chunk)
        check('sha256:' + py_hash.hexdigest() == expected, 'streaming-python-large-blob')
        bad = native.send(
            action='put',
            bytes=b64(b'wrong'),
            media_type='text/plain',
            expected='sha256:' + '0' * 64,
        )
        check(bad == {'error': 'digest_mismatch'}, 'digest-mismatch')
        check(not (store.index / ('0' * 64)).exists(), 'digest-mismatch-no-index')
        check(not list(store.staging.iterdir()), 'normal-paths-clean-staging')
    finally:
        native.stop()


async def hardlinks_and_corruption(binary, root):
    store = GitContentStore(root)
    blob = await store.put_bytes(b'correct', 'application/octet-stream')
    path = store.binary / blob.digest.removeprefix('sha256:')
    link = root / 'lfs-shared-link'
    os.link(path, link)
    before = path.stat().st_ino
    native = Native(binary, root)
    try:
        native.call(action='put', bytes=b64(b'correct'), media_type=blob.media_type, expected=None)
        check(
            path.stat().st_ino == before and link.stat().st_nlink == 2,
            'binary-inode-and-lfs-hardlink-preserved',
        )
        path.write_bytes(b'corrupt')
        check(
            native.send(action='read', blob=wire(blob), range=[0, 1])
            == {'error': 'content_digest_mismatch'},
            'corrupt-range-rejected',
        )
        check(
            native.send(
                action='put', bytes=b64(b'correct'), media_type=blob.media_type, expected=None
            )
            == {'error': 'content_digest_mismatch'},
            'corrupt-duplicate-not-replaced',
        )
        check(link.read_bytes() == b'corrupt', 'existing-inode-never-rewritten')
        path.unlink()
        path.symlink_to(link)
        check(
            native.send(action='read', blob=wire(blob), range=None)
            == {'error': 'unsafe_content_path'},
            'binary-symlink-rejected',
        )
    finally:
        native.stop()


def hermetic_git(binary, root):
    root.mkdir()
    env = os.environ | {
        'GIT_DIR': str(root / 'wrong'),
        'GIT_OBJECT_DIRECTORY': str(root / 'wrong-objects'),
        'GIT_CONFIG_COUNT': '1',
        'GIT_CONFIG_KEY_0': 'core.bare',
        'GIT_CONFIG_VALUE_0': 'false',
    }
    native = Native(binary, root, env=env)
    try:
        native.call(action='put', bytes=b64(b'ready'), media_type='text/plain', expected=None)
        hooks = root / 'private.git' / 'hooks'
        hooks.mkdir(exist_ok=True)
        marker = root / 'hook-executed'
        hook = hooks / 'reference-transaction'
        hook.write_text('#!/bin/sh\nprintf bad > "' + str(marker) + '"\n')
        hook.chmod(0o700)
        native.call(
            action='put', bytes=b64(b'no inherited config'), media_type='text/plain', expected=None
        )
        check(not marker.exists(), 'git-hooks-disabled')
        check(not (root / 'wrong-objects').exists(), 'inherited-git-environment-ignored')
    finally:
        native.stop()


async def crashes(binary, root):
    root.mkdir()
    native = Native(binary, root)
    try:
        checkpoint = native.send(
            action='generated',
            length=131_072,
            media_type='text/plain',
            hold_during=True,
            hold_after=False,
        )
        check(checkpoint == {'checkpoint': 'held'}, 'crash-before-publication-checkpoint')
    finally:
        native.stop(kill=True)
    check(not list((root / 'index').iterdir()), 'crash-before-publication-no-index')
    # SIGKILL leaves staging bytes intentionally: no unsafe GC/recovery claim.
    check(bool(list((root / 'staging').iterdir())), 'crash-staging-retained-for-recovery')
    native = Native(binary, root)
    length = 131_073
    blob = BlobRef(
        digest='sha256:' + hashlib.sha256(b'x' * length).hexdigest(),
        size=length,
        media_type='text/plain',
    )
    try:
        checkpoint = native.send(
            action='generated',
            length=length,
            media_type=blob.media_type,
            hold_during=False,
            hold_after=True,
        )
        check(checkpoint == {'checkpoint': 'held'}, 'crash-after-publication-checkpoint')
    finally:
        native.stop(kill=True)
    store = GitContentStore(root)
    check(await store.read_bytes(blob) == b'x' * length, 'python-reads-after-lost-native-response')
    native = Native(binary, root)
    try:
        check(
            native.call(
                action='generated',
                length=length,
                media_type=blob.media_type,
                hold_during=False,
                hold_after=False,
            )
            == wire(blob),
            'retry-after-lost-response',
        )
    finally:
        native.stop()


async def main(binary):
    with tempfile.TemporaryDirectory(prefix='msg-content-parity-') as name:
        root = Path(name)
        await interoperate(binary, root / 'interop')
        await hardlinks_and_corruption(binary, root / 'hardlinks')
        hermetic_git(binary, root / 'hermetic')
        await crashes(binary, root / 'crashes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.binary.resolve()))
    result = {
        'checks': len(REPORT),
        'passed': REPORT,
        'scope': 'content storage primitives; not a server or recovery journal',
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
