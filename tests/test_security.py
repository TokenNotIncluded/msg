from dataclasses import replace
from datetime import UTC, datetime, timedelta
import pytest

from msg.core.codec import canonical, decode, wire
from msg.core.errors import Failure
from msg.security.crypto import Ed25519Signer, verify, seal_private_key, open_private_key
from msg.security.policy import selected_class, class_bits, scope_contains, grant_covers
from msg.core.models import Scope, CapabilityGrant, ResourceId
from msg.storage.sqlite import SqliteMetadataStore
from test_foundation import item


def test_signatures_separate_purposes_and_reject_tampering():
    signer=Ed25519Signer.generate()
    sig=signer.sign(b'hello',purpose='request')
    verify(signer.public_key,b'hello',sig,purpose='request')
    for payload,purpose in [(b'hello','receipt'),(b'bye','request')]:
        with pytest.raises(Failure,match='invalid_signature'):
            verify(signer.public_key,payload,sig,purpose=purpose)


def test_encrypted_key_roundtrip_and_wrong_pin():
    signer=Ed25519Signer.generate()
    wrapped=seal_private_key(signer.private_bytes(), 'correct horse battery staple')
    assert open_private_key(wrapped,'correct horse battery staple')==signer.private_bytes()
    assert signer.private_bytes() not in canonical(wrapped)
    with pytest.raises(Failure,match='invalid_pin'):
        open_private_key(wrapped,'wrong horse battery staple')
    altered=dict(wrapped,public_key='altered')
    with pytest.raises(Failure): open_private_key(altered,'correct horse battery staple')
    for pin in ['', '1234','password','00000000']:
        with pytest.raises(Failure,match='weak_pin'):
            seal_private_key(signer.private_bytes(),pin)


def test_mode_selection_is_not_a_union():
    r=replace(item(),owner='alice',group='staff',mode=0o046)
    assert selected_class(r,'alice',{'staff'})=='owner'
    assert class_bits(r.mode,'owner')==0
    assert selected_class(r,'bob',{'staff'})=='group'
    assert selected_class(r,None,set())=='other'


async def test_scopes_follow_current_parent_and_operation_exactness(tmp_path):
    store=SqliteMetadataStore(tmp_path/'db')
    async with store.transaction(write=True) as tx:
        for r in [item('root'),item('a','root'),item('b','root'),item('child','a')]:
            await tx.insert(r)
        scope=Scope(resource_id='a',descendants=True)
        assert await scope_contains(scope,'child',tx)
        assert not await scope_contains(Scope(resource_id='a'),'child',tx)
        grant=CapabilityGrant(capability='resource.certified_write',version=1,scope=scope,
                              operations=frozenset({'content.post_create@1'}),constraints={})
        assert await grant_covers(grant,'resource.certified_write','content.post_create@1','child',tx)
        assert not await grant_covers(grant,'resource.write_override','content.post_create@1','child',tx)
        assert not await grant_covers(grant,'resource.certified_write','content.post_edit@1','child',tx)
        child=await tx.resource('child')
        await tx.replace(replace(child,parent='b',generation=1),0)
        assert not await scope_contains(scope,'child',tx)
