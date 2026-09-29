"""Optional content-group sharing grants read, never staging or write authority."""
import importlib
import os

import pytest

from msg.core.errors import Failure
from msg.storage.git import GitContentStore


@pytest.mark.parametrize('group_read', [False, True])
async def test_new_publication_uses_exact_opt_in_read_group_mode(tmp_path, group_read):
    previous = os.umask(0o077)
    try:
        store = GitContentStore(tmp_path / 'content', binary_dir=tmp_path / 'blobs',
            staging_dir=tmp_path / 'staging', group_read=group_read)
        text = await store.put_bytes(b'<h1>group reader</h1>', 'text/html')
        binary = await store.put_bytes(b'\0\x01\x02\xff', 'application/octet-stream')
    finally:
        os.umask(previous)
    expected = 0o040 if group_read else 0
    for path in (store.index / text.digest[7:], store.index / binary.digest[7:],
                 store.binary / binary.digest[7:]):
        assert path.stat().st_mode & 0o077 == expected
    assert store.staging.stat().st_mode & 0o077 == 0
    if group_read:
        for path in (store.path, store.index, store.binary):
            assert path.stat().st_mode & 0o2777 == 0o2750
        entry = store._entry(text)
        obj = store.repo / 'objects' / entry['oid'][:2] / entry['oid'][2:]
        assert obj.stat().st_mode & 0o077 == 0o040
        reader_class = importlib.import_module('msg.storage.read_only').GitContentReader
        reader = reader_class(store.path, binary_dir=store.binary)
        assert reader_class.read is GitContentStore.read
        assert await reader.read_bytes(text) == b'<h1>group reader</h1>'
        assert await reader.read_bytes(binary) == b'\0\x01\x02\xff'
        assert not hasattr(reader, 'put_bytes')
        assert not hasattr(reader, 'staging')


def test_enabling_group_reads_never_silently_changes_existing_permissions(tmp_path):
    previous = os.umask(0o077)
    try:
        private = GitContentStore(tmp_path / 'content', binary_dir=tmp_path / 'blobs',
                                  staging_dir=tmp_path / 'staging')
    finally:
        os.umask(previous)
    before = {str(p): p.stat().st_mode for p in tmp_path.rglob('*')}
    with pytest.raises(Failure, match='content_sharing_not_prepared'):
        GitContentStore(private.path, binary_dir=private.binary,
                        staging_dir=private.staging, group_read=True)
    assert before == {str(p): p.stat().st_mode for p in tmp_path.rglob('*')}
