"""Hardware boundaries and recovery use real Ed25519 verification, with a fake reader."""

from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from test_service import NOW
from yubikit.piv import KEY_TYPE, PIN_POLICY, TOUCH_POLICY

from msg.client import ClientState, MsgClient
from msg.client_yubikey import (
    MAX_MESSAGE,
    YubiKeySigner,
    attach,
    initialize,
    load_profile,
    run_command,
    save_profile,
)
from msg.core.codec import canonical, loads
from msg.core.errors import Failure
from msg.security.crypto import Ed25519Signer
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


@pytest.fixture
def reader(monkeypatch):
    from msg import client_yubikey as hardware

    key = Ed25519Signer.generate()
    values = SimpleNamespace(occupied=False, generation=0, pin=0, objects={}, key=key)

    class Session:
        def generate_key(self, slot, algorithm, pin, touch):
            assert algorithm == KEY_TYPE.ED25519
            assert pin == PIN_POLICY.ONCE and touch == TOUCH_POLICY.ALWAYS
            values.occupied = True
            values.generation += 1
            return key._private.public_key()

        def sign(self, slot, algorithm, message, hash_algorithm):
            assert hash_algorithm is None and algorithm == KEY_TYPE.ED25519
            return key._private.sign(message)

        def put_object(self, identifier, raw):
            values.objects[identifier] = loads(raw)

    @contextmanager
    def device(slot=None, public=None):
        if public is not None:
            assert public == key.public_key
        yield Session()

    def metadata(session, slot):
        return (
            SimpleNamespace(
                key_type=KEY_TYPE.ED25519,
                public_key=key._private.public_key(),
                generated=True,
                pin_policy=PIN_POLICY.ONCE,
                touch_policy=TOUCH_POLICY.ALWAYS,
            )
            if values.occupied
            else None
        )

    monkeypatch.setattr(hardware, 'device_session', device)
    monkeypatch.setattr(hardware, 'metadata_or_none', metadata)
    monkeypatch.setattr(hardware, 'authenticate_management', lambda session: None)
    monkeypatch.setattr(hardware, 'verify_pin', lambda session: None)
    monkeypatch.setattr(
        hardware,
        'read_directory',
        lambda session: values.objects.get(
            hardware.PROFILE_OBJECT, {'format': 'msg-yubikey-v1', 'profiles': []}
        ),
    )
    return values


def test_hardware_state_has_no_identity_seed_and_round_trips(tmp_path, reader):
    state = ClientState(tmp_path, server='https://example.test')
    initialized = initialize(state, '83')
    assert initialized['private_key_exported'] is False
    assert reader.generation == 1
    assert not state.key_path.exists()
    assert state.hardware_path.stat().st_mode & 0o077 == 0
    restored = ClientState(tmp_path)
    assert restored.signer.public_key == reader.key.public_key
    with pytest.raises(Failure, match='hardware_private_key_not_exportable'):
        restored.signer.private_bytes()
    with pytest.raises(Failure, match='identity_key_already_exists'):
        restored.save_signer(reader.key)


def test_occupied_slot_refuses_generation_before_authentication(tmp_path, reader, monkeypatch):
    reader.occupied = True
    monkeypatch.setattr(
        'msg.client_yubikey.authenticate_management', lambda session: pytest.fail('must not prompt')
    )
    with pytest.raises(Failure, match='yubikey_slot_occupied'):
        initialize(ClientState(tmp_path, server='https://example.test'), '83')
    assert reader.generation == 0
    assert not (tmp_path / 'identity.key').exists()
    assert not (tmp_path / 'hardware-signer.json').exists()


def test_large_message_fails_before_touch_or_open(tmp_path, reader, monkeypatch):
    state = ClientState(tmp_path, server='https://example.test')
    initialize(state, '83')
    monkeypatch.setattr(
        'msg.client_yubikey.device_session', lambda *args: pytest.fail('must not open')
    )
    with pytest.raises(Failure, match='yubikey_message_too_large'):
        state.signer.sign(b'x' * MAX_MESSAGE, purpose='request')


def test_unplugged_key_never_falls_back_to_software(tmp_path, reader, monkeypatch):
    state = ClientState(tmp_path, server='https://example.test')
    initialize(state, '83')

    def absent(*args):
        raise Failure('yubikey_not_found')

    monkeypatch.setattr('msg.client_yubikey.device_session', absent)
    state = ClientState(tmp_path)
    with pytest.raises(Failure, match='yubikey_not_found'):
        state.signer.sign(b'test', purpose='request')
    assert not state.key_path.exists()


def test_corrupt_or_conflicting_stub_does_not_generate_key(tmp_path, reader):
    state = ClientState(tmp_path, server='https://example.test')
    initialize(state, '83')
    descriptor = state.signer.descriptor()
    descriptor['key_id'] = 'k_wrong'
    state.hardware_path.write_bytes(canonical(descriptor))
    with pytest.raises(Failure, match='hardware_key_mismatch'):
        ClientState(tmp_path)
    state.hardware_path.write_bytes(canonical(state.signer.descriptor()))
    state.key_path.write_bytes(reader.key.private_bytes())
    with pytest.raises(Failure, match='client_key_backend_conflict'):
        ClientState(tmp_path)
    assert reader.generation == 1


def test_resume_generated_key_without_generating_another(tmp_path, reader):
    reader.occupied = True
    state = ClientState(tmp_path, server='https://example.test')
    attach(state, '83')
    assert isinstance(state.signer, YubiKeySigner) and reader.generation == 0


@pytest.mark.asyncio
async def test_hardware_registration_new_machine_login_and_tampered_directory(
    installed, tmp_path, reader
):
    app, _ = installed
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    )
    transport = HTTPTransport(app.settings.service_url, http=http)
    state = ClientState(tmp_path / 'first', server=app.settings.service_url)
    initialize(state, '83')
    client = MsgClient(state, transport, clock=lambda: NOW)
    registration = await client.register('hardware-test')
    assert registration.status == 'ok', registration
    save_profile(state)
    restored = ClientState(tmp_path / 'second', server=state.server)
    result = await run_command(
        MsgClient(restored, transport, clock=lambda: NOW),
        SimpleNamespace(action='login', server=state.server, slot='83'),
    )
    assert result['logged_in'] and restored.subject == state.subject
    assert not restored.key_path.exists() and not restored.age_key_path.exists()
    assert result['encryption_key_restored'] is False
    assert reader.generation == 1
    directory = next(iter(reader.objects.values()))
    directory['profiles'][0]['body']['subject_id'] = 'u_other'
    with pytest.raises(Failure, match='invalid_signature'):
        load_profile(state.server, '83')
    await http.aclose()
