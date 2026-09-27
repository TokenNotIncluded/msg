"""Opt-in Inbox webhook: durable queue, bounded payload and explicit uncertainty."""
from datetime import timedelta
import json
import secrets
import socket

import pytest

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.workers.effects import EffectWorker
from msg.workers.webhook import PublicResolver, sign_delivery, verify_delivery, validate_endpoint
from test_service import call, register, NOW


class Sender:
    def __init__(self, result='delivered'):
        self.result=result
        self.calls=[]

    async def send(self,url,secret,body,**kw):
        self.calls.append((url,secret,json.loads(body),kw,body))
        return self.result


@pytest.mark.asyncio
async def test_inbox_webhook_opt_in_dedupe_disable_and_bounded_payload(installed):
    app,_=installed
    sender_key,sender, sender_cert=await register(app,'webhook-sender')
    recipient_key,recipient,recipient_cert=await register(app,'webhook-recipient')
    post=await call(app,'content.post_create',{'parent':'/main','body':'private words must stay here'},
                    key=sender_key,subject=sender,certs=(sender_cert,))
    assert post.status=='ok',wire(post)
    args={'recipient':recipient,'resource':wire(post.resources[0])}
    first=await call(app,'communication.send',args,key=sender_key,subject=sender,
                     certs=(sender_cert,),rid='webhook-before-opt-in')
    assert first.status=='ok',wire(first)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='webhook'")[0]==0

    secret=secrets.token_bytes(32)
    setting=await call(app,'communication.webhook_set',
        {'url':'https://hooks.example.org/inbox','secret':b64(secret)},
        key=recipient_key,subject=recipient,certs=(recipient_cert,))
    assert setting.status=='ok',wire(setting)
    status=await call(app,'communication.webhook_status',{},key=recipient_key,
                      subject=recipient,certs=(recipient_cert,))
    assert status.data['enabled'] is True and 'secret' not in str(status.data)
    async with app.metadata.transaction(write=False) as tx:
        raw=tx.one('SELECT ciphertext FROM webhook_endpoints WHERE subject=?',(recipient,))[0]
        assert b64(secret) not in raw
    sent=await call(app,'communication.send',args,key=sender_key,subject=sender,
                    certs=(sender_cert,),rid='webhook-once')
    repeated=await call(app,'communication.send',args,key=sender_key,subject=sender,
                        certs=(sender_cert,),rid='webhook-once')
    assert sent.status=='ok' and repeated.replayed
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='webhook'")[0]==1
    sink=Sender()
    await EffectWorker(app,webhook_sender=sink).run_once()
    assert len(sink.calls)==1
    url,key,payload,headers,body=sink.calls[0]
    assert key==secret and url=='https://hooks.example.org/inbox'
    assert set(payload)=={'event_id','delivery_id','timestamp','subject_id','type'}
    assert 'private words' not in body.decode() and payload['subject_id']==recipient
    wire_headers={'Msg-Timestamp':headers['timestamp'],
                  'Msg-Signature':'sha256='+sign_delivery(secret,headers['timestamp'],body),
                  'Msg-Delivery-Id':headers['delivery_id'],
                  'Msg-Event-Id':headers['event_id']}
    seen=set()
    assert verify_delivery(wire_headers,body,secret,now=NOW,seen=seen)==payload['delivery_id']
    with pytest.raises(Failure,match='webhook_replay'):
        verify_delivery(wire_headers,body,secret,now=NOW,seen=seen)

    queued=await call(app,'communication.send',args,key=sender_key,subject=sender,
                      certs=(sender_cert,),rid='webhook-disabled-pending')
    assert queued.status=='ok'
    disabled=await call(app,'communication.webhook_disable',{},key=recipient_key,
                        subject=recipient,certs=(recipient_cert,))
    assert disabled.status=='ok'
    await EffectWorker(app,webhook_sender=sink).run_once()
    assert len(sink.calls)==1
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM jobs WHERE kind='webhook' AND state='failed'")[0]==1


@pytest.mark.asyncio
async def test_webhook_retries_definite_5xx_but_marks_network_ambiguity_uncertain(installed):
    app,_=installed
    sender_key,sender,sender_cert=await register(app,'webhook-retry-sender')
    recipient_key,recipient,recipient_cert=await register(app,'webhook-retry-recipient')
    await call(app,'communication.webhook_set',
        {'url':'https://hooks.example.org/inbox','secret':b64(secrets.token_bytes(32))},
        key=recipient_key,subject=recipient,certs=(recipient_cert,))
    post=await call(app,'content.post_create',{'parent':'/main','body':'source'},
                    key=sender_key,subject=sender,certs=(sender_cert,))
    sent=await call(app,'communication.send',
        {'recipient':recipient,'resource':wire(post.resources[0])},
        key=sender_key,subject=sender,certs=(sender_cert,))
    sink=Sender('retry')
    await EffectWorker(app,webhook_sender=sink).run_once()
    async with app.metadata.transaction(write=False) as tx:
        job=await tx.job(tx.one("SELECT id FROM jobs WHERE kind='webhook'")[0])
        assert job.state=='pending' and job.attempts==1
        assert job.next_attempt_at==NOW+timedelta(seconds=30)
    class Ambiguous(Sender):
        async def send(self,*args,**kwargs):
            raise Failure('external_uncertain')
    async with app.metadata.transaction(write=True) as tx:
        from dataclasses import replace
        await tx.save_job(replace(job,next_attempt_at=NOW))
    await EffectWorker(app,webhook_sender=Ambiguous()).run_once()
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.job(job.id)).state=='uncertain'


@pytest.mark.parametrize('url',['http://example.org/hook','https://127.0.0.1/hook',
    'https://[::1]/hook','https://10.1.2.3/hook','https://example.org:8443/hook',
    'https://user:pass@example.org/hook'])
def test_webhook_rejects_unsafe_endpoint(url):
    with pytest.raises(Failure):
        validate_endpoint(url)


def test_webhook_signature_covers_raw_body_and_timestamp():
    secret=secrets.token_bytes(32)
    timestamp=str(int(NOW.timestamp()))
    body=canonical({'event_id':'e_a','delivery_id':'job_a','timestamp':timestamp})
    headers={'Msg-Timestamp':timestamp,'Msg-Delivery-Id':'job_a','Msg-Event-Id':'e_a',
             'Msg-Signature':'sha256='+sign_delivery(secret,timestamp,body)}
    assert verify_delivery(headers,body,secret,now=NOW)=='job_a'
    with pytest.raises(Failure,match='invalid_webhook_signature'):
        verify_delivery(headers,body+b' ',secret,now=NOW)
    with pytest.raises(Failure,match='webhook_replay_window'):
        verify_delivery(headers,body,secret,now=NOW+timedelta(seconds=301))


@pytest.mark.asyncio
async def test_webhook_resolver_rejects_private_or_mixed_dns(monkeypatch):
    import asyncio
    loop=asyncio.get_running_loop()
    async def answer(*args,**kwargs):
        return [(socket.AF_INET,socket.SOCK_STREAM,0,'',('93.184.216.34',443)),
                (socket.AF_INET,socket.SOCK_STREAM,0,'',('127.0.0.1',443))]
    monkeypatch.setattr(loop,'getaddrinfo',answer)
    with pytest.raises(Failure,match='private_target_forbidden'):
        await PublicResolver().resolve('hooks.example.org',443)
