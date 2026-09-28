"""The read gate catches both same-count SQL mutations and pre-send effects."""
import pytest

from msg.workers.mail import SmtpSender
from read_only_evidence import readonly_evidence


@pytest.mark.asyncio
async def test_read_evidence_rejects_update_without_changing_row_count(installed, monkeypatch):
    app, _ = installed
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('read_evidence_probe', 'before')
        count = tx.one('SELECT COUNT(*) FROM settings')[0]
    with pytest.raises(AssertionError, match='read changed authoritative row values'):
        async with readonly_evidence(app, monkeypatch):
            async with app.metadata.transaction(write=True) as tx:
                tx.set_setting('read_evidence_probe', 'after')
                assert tx.one('SELECT COUNT(*) FROM settings')[0] == count


@pytest.mark.asyncio
async def test_read_evidence_rejects_sender_before_network_access(installed, monkeypatch):
    app, _ = installed
    with pytest.raises(AssertionError, match='read attempted publication'):
        async with readonly_evidence(app, monkeypatch):
            await SmtpSender(None).send(None)


@pytest.fixture
async def signalled_install(installed, monkeypatch):
    """Use the real signal interface, with a hard stop before socket access."""
    from unittest.mock import Mock
    from msg.storage.valkey_bus import ValkeyOutboxSignal

    app, _ = installed
    signal = ValkeyOutboxSignal('redis://127.0.0.1:56379/0')
    network = Mock(side_effect=AssertionError('read evidence missed a socket-bound publish'))
    monkeypatch.setattr(signal.client, 'publish', network)
    monkeypatch.setattr(app.metadata, 'signal', signal)
    try:
        yield app, network
    finally:
        network.assert_not_called()
        signal.client.close()


@pytest.mark.asyncio
async def test_read_evidence_accepts_idle_configured_signal(signalled_install, monkeypatch):
    app, _ = signalled_install
    async with readonly_evidence(app, monkeypatch):
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT 1')[0] == 1


@pytest.mark.asyncio
async def test_read_evidence_detects_signal_even_when_store_swallows_failure(
        signalled_install, monkeypatch):
    app, _ = signalled_install
    with pytest.raises(AssertionError, match='read attempted publication'):
        async with readonly_evidence(app, monkeypatch):
            # Exercise the real post-commit notification path without writing a
            # job or any business row. The store catches publication exceptions;
            # the evidence context must still reject this attempted side effect.
            async with app.metadata.transaction(write=False) as tx:
                tx.pending_effect_ids.append('test-only-not-persisted')


@pytest.mark.asyncio
async def test_read_evidence_detects_caught_sender_failure(installed, monkeypatch):
    app, _ = installed
    with pytest.raises(AssertionError, match='read attempted publication'):
        async with readonly_evidence(app, monkeypatch):
            try:
                await SmtpSender(None).send(None)
            except AssertionError:
                pass  # A caller cannot hide a forbidden effect by catching it.
