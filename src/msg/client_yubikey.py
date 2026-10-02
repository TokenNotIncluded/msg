"""Built-in PIV signer. No identity seed or PIN is exported or persisted."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from msg.core.codec import b64, canonical, decode, loads, unb64, wire
from msg.core.errors import Failure, require
from msg.core.models import Signature
from msg.security.crypto import framed, key_id, verify
from msg.service_origin import service_origin

PROFILE_OBJECT = 0x5F4D53
MAX_MESSAGE = 2800  # Conservative bound below Yubico's 3062-byte APDU ceiling.
MAX_DIRECTORY = 2800


def slot_number(value):
    require(isinstance(value, str), 'invalid_yubikey_slot')
    try:
        slot = int(value, 16)
    except ValueError:
        raise Failure('invalid_yubikey_slot') from None
    require(0x82 <= slot <= 0x95, 'yubikey_extension_slot_required')
    return slot


def secret_prompt(prompt):
    """Local terminal/desktop only; never accept PINs in argv or environment."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt + ': ')
    if (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) and shutil.which('zenity'):
        try:
            result = subprocess.run(
                ['zenity', '--password', '--title', prompt, '--timeout=90'],
                capture_output=True,
                text=True,
                timeout=95,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise Failure('yubikey_prompt_cancelled') from None
        require(result.returncode == 0, 'yubikey_prompt_cancelled')
        return result.stdout.rstrip('\n')
    raise Failure('yubikey_local_prompt_required')


def sdk():
    try:
        from smartcard.ExclusiveConnectCardConnection import ExclusiveConnectCardConnection
        from ykman.device import list_ccid_devices
        from ykman.pcsc import ScardSmartCardConnection
        from yubikit.piv import PivSession
    except ImportError:
        raise Failure('yubikey_driver_unavailable') from None

    def open_exclusive(device):
        return ScardSmartCardConnection(
            ExclusiveConnectCardConnection(device.reader.createConnection())
        )

    return list_ccid_devices, open_exclusive, PivSession


def public_bytes(metadata):
    from yubikit.piv import KEY_TYPE

    require(metadata.key_type == KEY_TYPE.ED25519, 'yubikey_ed25519_required')
    return metadata.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def metadata_or_none(session, slot):
    from yubikit.core.smartcard import SW, ApduError
    from yubikit.piv import SLOT

    try:
        return session.get_slot_metadata(SLOT(slot))
    except ApduError as exc:
        if exc.sw == SW.REFERENCE_DATA_NOT_FOUND:
            return None
        raise


@contextmanager
def device_session(slot=None, public=None):
    list_devices, connection_type, session_type = sdk()
    connections = []
    selected = None
    try:
        for device in list_devices():
            connection = connection_type(device)
            connections.append(connection)
            session = session_type(connection)
            if public is not None:
                metadata = metadata_or_none(session, slot)
                from yubikit.piv import KEY_TYPE, PIN_POLICY, TOUCH_POLICY

                if (
                    metadata is None
                    or metadata.key_type != KEY_TYPE.ED25519
                    or public_bytes(metadata) != public
                ):
                    connection.close()
                    connections.remove(connection)
                    continue
                require(
                    metadata.generated
                    and metadata.touch_policy == TOUCH_POLICY.ALWAYS
                    and metadata.pin_policy in {PIN_POLICY.ONCE, PIN_POLICY.ALWAYS},
                    'yubikey_key_policy_changed',
                )
            require(selected is None, 'yubikey_ambiguous_device')
            selected = session
        require(selected is not None, 'yubikey_not_found')
        require(selected.version >= (5, 7, 0), 'yubikey_firmware_57_required')
        yield selected
    except Failure:
        raise
    except Exception as exc:
        # Never expose exception text: it may contain reader names or credentials.
        details = {'type': type(exc).__name__}
        if isinstance(getattr(exc, 'sw', None), int):
            details['status_word'] = hex(exc.sw)
        raise Failure('yubikey_device_error', details=details) from None
    finally:
        for connection in connections:
            connection.close()


def verify_pin(session):
    from yubikit.piv import InvalidPinError

    pin = secret_prompt('MSG · YubiKey PIV PIN')
    try:
        session.verify_pin(pin)
    except InvalidPinError as exc:
        raise Failure(
            'yubikey_invalid_pin', details={'attempts_remaining': exc.attempts_remaining}
        ) from None
    return pin


def authenticate_management(session):
    from ykman.piv import derive_management_key, get_pivman_data, get_pivman_protected_data

    pin = verify_pin(session)
    data = get_pivman_data(session)
    if data.has_stored_key:
        management = get_pivman_protected_data(session).key
    elif data.has_derived_key:
        management = derive_management_key(pin, data.salt)
    else:
        encoded = secret_prompt('MSG · PIV management key (hex)')
        try:
            management = bytes.fromhex(encoded)
        except ValueError:
            raise Failure('yubikey_invalid_management_key') from None
    session.authenticate(management)
    session.verify_pin(pin)


@dataclass(frozen=True)
class YubiKeySigner:
    public_key: bytes
    slot: str

    def __post_init__(self):
        slot_number(self.slot)
        require(len(self.public_key) == 32, 'invalid_public_key')
        Ed25519PublicKey.from_public_bytes(self.public_key)

    @property
    def key_id(self):
        return key_id(self.public_key)

    def descriptor(self):
        return {
            'version': 1,
            'backend': 'yubikey-piv',
            'algorithm': 'ed25519',
            'slot': self.slot,
            'public_key': b64(self.public_key),
            'key_id': self.key_id,
        }

    @classmethod
    def from_descriptor(cls, value):
        require(
            isinstance(value, dict)
            and set(value) == {'version', 'backend', 'algorithm', 'slot', 'public_key', 'key_id'},
            'invalid_hardware_signer',
        )
        require(
            value['version'] == 1
            and value['backend'] == 'yubikey-piv'
            and value['algorithm'] == 'ed25519',
            'invalid_hardware_signer',
        )
        signer = cls(unb64(value['public_key'], limit=32), value['slot'])
        require(signer.key_id == value['key_id'], 'hardware_key_mismatch')
        return signer

    def private_bytes(self):
        raise Failure('hardware_private_key_not_exportable')

    def sign(self, payload, *, purpose):
        message = framed(payload, purpose)
        require(
            len(message) <= MAX_MESSAGE,
            'yubikey_message_too_large',
            details={'maximum': MAX_MESSAGE, 'bytes': len(message)},
        )
        with device_session(slot_number(self.slot), self.public_key) as session:
            from yubikit.piv import KEY_TYPE, SLOT

            verify_pin(session)
            print('MSG: touch your YubiKey to authorize this signature.', file=sys.stderr)
            signature = Signature(
                key_id=self.key_id,
                algorithm='ed25519',
                value=session.sign(SLOT(slot_number(self.slot)), KEY_TYPE.ED25519, message, None),
            )
        verify(self.public_key, payload, signature, purpose=purpose)
        return signature


def state_operation(method):
    @wraps(method)
    def locked(state, *args, **kwargs):
        from msg.client_upgrade import locked_state

        with locked_state(state):
            return method(state, *args, **kwargs)

    return locked


@state_operation
def initialize(state, slot):
    require(state.subject is None and state.signer is None, 'identity_already_configured')
    number = slot_number(slot)
    with device_session() as session:
        from yubikit.piv import KEY_TYPE, PIN_POLICY, SLOT, TOUCH_POLICY

        require(metadata_or_none(session, number) is None, 'yubikey_slot_occupied')
        read_directory(session)  # Refuse an unrelated application object before writing any key.
        authenticate_management(session)
        # Check again on the same connection after authentication, before generation.
        require(metadata_or_none(session, number) is None, 'yubikey_slot_occupied')
        public = session.generate_key(
            SLOT(number), KEY_TYPE.ED25519, PIN_POLICY.ONCE, TOUCH_POLICY.ALWAYS
        )
        signer = YubiKeySigner(public.public_bytes(Encoding.Raw, PublicFormat.Raw), f'{number:02x}')
        state.save_signer(signer)
    return {'initialized': True, 'signer': signer.descriptor(), 'private_key_exported': False}


@state_operation
def attach(state, slot):
    require(state.subject is None and state.signer is None, 'identity_already_configured')
    with device_session() as session:
        metadata = metadata_or_none(session, slot_number(slot))
        require(metadata is not None, 'yubikey_slot_empty')
        from yubikit.piv import TOUCH_POLICY

        require(
            metadata.generated and metadata.touch_policy == TOUCH_POLICY.ALWAYS,
            'yubikey_generated_touch_key_required',
        )
        signer = YubiKeySigner(public_bytes(metadata), slot.lower())
        state.save_signer(signer)
    return {'attached': True, 'signer': signer.descriptor()}


def read_directory(session):
    from yubikit.core.smartcard import SW, ApduError

    try:
        raw = session.get_object(PROFILE_OBJECT)
    except ApduError as exc:
        if exc.sw == SW.FILE_NOT_FOUND:
            return {'format': 'msg-yubikey-v1', 'profiles': []}
        raise
    require(len(raw) <= MAX_DIRECTORY, 'hardware_directory_too_large')
    value = loads(raw)
    require(
        isinstance(value, dict)
        and set(value) == {'format', 'profiles'}
        and value['format'] == 'msg-yubikey-v1'
        and isinstance(value['profiles'], list)
        and len(value['profiles']) <= 8,
        'hardware_directory_conflict',
    )
    for entry in value['profiles']:
        validate_profile(entry)
    return value


def validate_profile(entry):
    require(
        isinstance(entry, dict) and set(entry) == {'body', 'signature'}, 'invalid_hardware_profile'
    )
    body = entry['body']
    require(
        isinstance(body, dict)
        and set(body) == {'server', 'subject_id', 'handle', 'certificates', 'signer'},
        'invalid_hardware_profile',
    )
    signer = YubiKeySigner.from_descriptor(body['signer'])
    require(service_origin(body['server']) == body['server'], 'invalid_hardware_profile')
    require(
        isinstance(body['subject_id'], str)
        and body['subject_id'].startswith('u_')
        and isinstance(body['handle'], str)
        and 0 < len(body['handle']) <= 64
        and isinstance(body['certificates'], list)
        and len(body['certificates']) <= 8
        and all(isinstance(x, str) and x.startswith('cert_') for x in body['certificates']),
        'invalid_hardware_profile',
    )
    verify(
        signer.public_key,
        canonical(body),
        decode(Signature, entry['signature']),
        purpose='hardware-profile-v1',
    )
    return signer


@state_operation
def save_profile(state):
    require(isinstance(state.signer, YubiKeySigner) and state.subject, 'hardware_identity_required')
    signer = state.signer
    body = {
        'server': state.server,
        'subject_id': state.subject,
        'handle': state.data['handle'],
        'certificates': list(state.certificates),
        'signer': signer.descriptor(),
    }
    payload = canonical(body)
    message = framed(payload, 'hardware-profile-v1')
    require(len(message) <= MAX_MESSAGE, 'yubikey_message_too_large')
    with device_session(slot_number(signer.slot), signer.public_key) as session:
        from yubikit.piv import KEY_TYPE, SLOT

        directory = read_directory(session)
        profiles = [
            x
            for x in directory['profiles']
            if (x['body']['server'], x['body']['signer']['slot']) != (state.server, signer.slot)
        ]
        require(len(profiles) < 8, 'hardware_directory_full')
        authenticate_management(session)
        print('MSG: touch your YubiKey to save this account directory.', file=sys.stderr)
        signature = Signature(
            key_id=signer.key_id,
            algorithm='ed25519',
            value=session.sign(SLOT(slot_number(signer.slot)), KEY_TYPE.ED25519, message, None),
        )
        verify(signer.public_key, payload, signature, purpose='hardware-profile-v1')
        entry = {'body': body, 'signature': wire(signature)}
        directory['profiles'] = [*profiles, entry]
        raw = canonical(directory)
        require(len(raw) <= MAX_DIRECTORY, 'hardware_directory_too_large')
        session.put_object(PROFILE_OBJECT, raw)
        require(read_directory(session) == directory, 'hardware_directory_write_failed')
    return {
        'saved_on_device': True,
        'subject_id': state.subject,
        'slot': signer.slot,
        'contains_private_key': False,
    }


def load_profile(server, slot):
    require(server is not None, 'hardware_server_confirmation_required')
    server = service_origin(server)
    with device_session() as session:
        directory = read_directory(session)
        entries = [
            x
            for x in directory['profiles']
            if x['body']['server'] == server and x['body']['signer']['slot'] == slot.lower()
        ]
        require(len(entries) == 1, 'hardware_profile_not_found')
        entry = entries[0]
        signer = validate_profile(entry)
        metadata = metadata_or_none(session, slot_number(slot))
        require(
            metadata is not None and public_bytes(metadata) == signer.public_key,
            'hardware_key_mismatch',
        )
    return entry['body']


def add_commands(commands):
    actions = commands.add_parser(
        'yubikey', help='Built-in hardware identity signer.'
    ).add_subparsers(dest='action', required=True)
    for action in ('init', 'attach', 'login'):
        cmd = actions.add_parser(action)
        cmd.add_argument('--slot', default='82')
    actions.add_parser('save-profile')
    actions.add_parser('status')


async def run_command(client, args):
    state = client.state
    if args.action == 'init':
        return initialize(state, args.slot)
    if args.action == 'attach':
        return attach(state, args.slot)
    if args.action == 'save-profile':
        return save_profile(state)
    if args.action == 'status':
        return {
            'hardware': isinstance(state.signer, YubiKeySigner),
            'signer': state.signer.descriptor()
            if isinstance(state.signer, YubiKeySigner)
            else None,
            'subject_id': state.subject,
            'server': state.server,
        }
    body = load_profile(args.server, args.slot)
    signer = YubiKeySigner.from_descriptor(body['signer'])
    require(state.subject in (None, body['subject_id']), 'client_identity_conflict')
    require(
        state.signer is None
        or (
            isinstance(state.signer, YubiKeySigner)
            and state.signer.descriptor() == signer.descriptor()
        ),
        'client_identity_conflict',
    )
    # Do not replace established state until a live signed request accepts this key.
    packet = client.prepare(
        'identity.certificate_renew', {}, subject=body['subject_id'], signer=signer, certificates=()
    )
    result = client.checked(await client.send(packet))
    if state.signer is None:
        state.save_signer(signer)
    state.data.update(
        subject_id=body['subject_id'],
        handle=body['handle'],
        certificates=[result.data['certificate_id']],
    )
    state._save()
    return {
        'logged_in': True,
        'subject_id': state.subject,
        'handle': body['handle'],
        'hardware': True,
        'slot': signer.slot,
        'private_key_exported': False,
        'encryption_key_restored': state.encryption_recipient is not None,
    }
