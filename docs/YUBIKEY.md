# YubiKey hardware identity

The `yubikey` backend ships with msgctl 0.2.7 by default, using Yubico's official Python SDK. Initial physical-device acceptance uses a YubiKey 5C NFC, firmware 5.8.0, on Linux. Windows/macOS and a separate physical computer are not yet acceptance-tested.

## Install

PIV Ed25519 requires firmware 5.7 or newer and the smart-card/CCID interface. FIDO-only security keys are not supported by this backend. Linux needs PC/SC; building pyscard from source also needs its headers and SWIG:

```sh
# Arch Linux
sudo pacman -S --needed pcsclite swig
sudo systemctl enable --now pcscd.socket
# Debian/Ubuntu alternative
sudo apt install pcscd libpcsclite-dev swig
# Then install the client, including its built-in hardware backend:
uv tool install --python 3.15 --refresh-package msgctl msgctl==0.2.7
```

The native server is not needed on the client computer. The existing one-command installer still pins an older client and does not deliver this feature yet. Close other smart-card applications if they hold an exclusive device connection; the plugin does not kill GPG or reset another app automatically.

## Create a hardware identity

Use `--account NAME` to select an account on the same service; software and YubiKey signers use the same XDG layout. Inspect a candidate slot first. `82` is the default, but this case uses `83` because `82` already contains an unrelated key.

```sh
msg --account light --server https://msg.lmm.best yubikey init --slot 83
msg --account light --server https://msg.lmm.best identity new light
msg --account light --server https://msg.lmm.best yubikey save-profile
```

Initialization checks an empty extension slot, authenticates PIV management locally, and generates Ed25519 inside the device with PIN ONCE and touch ALWAYS. Existing keys are never overwritten. PIN and management key prompts use the local terminal or a desktop password dialog, never arguments or environment variables. Incorrect PINs are not retried automatically. Repeated failed PIN attempts can block the shared PIV application, so stop and verify the correct PIN locally.

The generated signing private key has no export operation. `hardware-signer.json` is a public reference (backend, slot, public key, fingerprint), protected by directory mode 0700 and file mode 0600. No `identity.key` is created. A separate local age/X25519 encryption key is still created during registration: this release protects the **identity signing key**, not every encryption/session secret.

If device generation succeeded but the local stub was lost, `yubikey attach --slot 83` recovers a generated Ed25519 key with touch ALWAYS without generating a replacement. Registration retains its normal resumable request journal.

## Select an account or move to another computer

```sh
msg --server https://msg.lmm.best account list
msg --server https://msg.lmm.best account use light
# On another computer, install the client/PCSC, insert the same key, then:
msg --account light --server https://msg.lmm.best yubikey login --slot 83
# Approve a browser login using the chosen identity:
msg --account light --server https://msg.lmm.best auth approve CODE
```

Do not run `init` or register again when moving computers. The public hardware reference and account state are recreated under `msg/services/msg.lmm.best/accounts/light` in their respective XDG roots. A legacy portable directory can be moved with `msg --server https://msg.lmm.best account import light /path/to/old-directory`; stop its background clients first.

`save-profile` writes a signed, nonsecret account directory into custom PIV object `5F4D53`, refusing unrelated or malformed content. It contains service origin, stable subject ID, handle, certificate IDs, and the public signer reference. Login requires an explicitly supplied service origin; it never silently connects to an untrusted URL from the card.

The plugin verifies the directory signature against the actual slot public key, then makes a signed certificate-renewal request to the service. Only a live successful response establishes local account state. Invalid/revoked keys, conflicting accounts and changed hardware policies fail closed. Removing the card does not generate a software replacement.

The directory supports up to eight entries, with a conservative total encoded size limit of 2800 bytes. Each origin/slot pair has one entry. It contains no PIN, token or encryption private key. Recovery into a clean config restores signing identity; it does not restore the original local age decryption key or automatically copy old encrypted data.

## Agents and browsers

The existing API-key mechanism can issue short-lived, server-enforced limited credentials after hardware approval. Its default ceiling is read-only and excludes identity/Root operations. Use it for background reads and listeners; token state is separate from the public hardware stub. Narrow ceilings can specify capability, resource/descendants and operation versions, within the owner's current authority.

Existing operations that require a personal signature still require a signer. This release does not add a general-purpose ephemeral signing-session protocol for unattended Agent writes. It does not grant admin/CA authority. Browser login continues through `msg auth approve CODE`; direct PIV access from a web page is not implemented.

The hardware signer signs the existing MSG frame without prehashing or changing its protocol. The conservative frame length limit is 2800 bytes; long posts/batches are rejected before a PIN prompt or touch. Removing the key prevents new hardware signatures but does not revoke existing short-lived tokens or browser sessions.

## Recovery

Keep a second independent authorized key or another verified recovery path. A key generated inside the device cannot be copied to a spare YubiKey later. Avoid deleting existing recovery credentials until both signing and recovery have been exercised.

## Sources

- [PIV slots and signing](https://docs.yubico.com/yesdk/users-manual/application-piv/slots.html)
- [Firmware 5.7 Ed25519 support](https://docs.yubico.com/hardware/yubikey/yk-tech-manual/yk5-firmware-5.7.html)
- [PIV data objects](https://docs.yubico.com/yesdk/users-manual/application-piv/get-and-put-data.html)
- [Yubico Python SDK](https://github.com/Yubico/yubikey-manager)
