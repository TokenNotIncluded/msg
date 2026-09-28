"""Site authority, recipient ownership, SMTP minimisation and resumable payloads."""
import asyncio
import subprocess
from dataclasses import replace

import pytest
from test_market_lifecycle import buy, market
from test_service import call

from msg.core.codec import b64, canonical, digest, loads, unb64, wire
from msg.core.errors import Failure
from msg.core.models import MailConfig
from msg.plugins.money import _balance
from msg.workers.effects import EffectWorker


class Sender:
    def __init__(self, result='sent'):
        self.messages = []
        self.result = result

    async def send(self, job):
        self.messages.append(dict(job.arguments))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def enable_mail(app):
    config = MailConfig(enabled=True,host='localhost',port=2525,tls='starttls',
                        sender='test@example.invalid',credential_file=None)
    app.settings = replace(app.settings,server=replace(app.settings.server,mail=config))


async def verified_address(app,key,subject,address):
    # Test the real signed account-email flow; don't manufacture verification rows.
    result=await call(app,'identity.email_set',{'address':address},key=key,subject=subject)
    assert result.status=='ok',wire(result)
    async with app.metadata.transaction(write=False) as tx:
        raw=tx.one("SELECT body FROM jobs WHERE kind='mail' AND body::jsonb->'principal'->>'subject'=? ORDER BY id DESC",(subject,))
        if raw is None:  # Jobs index actor/subject in the canonical Principal, not columns.
            raise AssertionError('verification job missing')
    job=loads(raw[0])
    token=job['arguments']['text'].rsplit(': ',1)[1]
    result=await call(app,'identity.email_verify',{'token':token},key=key,subject=subject)
    assert result.status=='ok',wire(result)


async def drain(app,sender):
    worker=EffectWorker(app,mail_sender=sender)
    for _ in range(100):
        if not await worker.run_once():
            return
    raise AssertionError('worker did not converge')


@pytest.mark.asyncio
@pytest.mark.parametrize('smtp', ['sent','uncertain','connection_failed','disabled'])
async def test_verified_checkout_mail_is_minimal_and_not_claimed(installed,smtp):
    app,root=installed
    enable_mail(app)
    sk,seller,bk,buyer,listing,_=await market(app,root)
    await verified_address(app,bk,buyer,'buyer@example.invalid')
    if smtp=='disabled':
        app.settings=replace(app.settings,server=replace(app.settings.server,mail=None))
    result=await buy(app,bk,buyer,listing,email='buyer@example.invalid')
    assert result.status=='ok' and result.data['order']['state']=='settled',wire(result)
    order=result.data['order']; oid=order['id']
    sender=Sender(Failure('mail_connection_failed',retryable=True) if smtp=='connection_failed' else smtp)
    await drain(app,sender)
    expected={'sent':'smtp_accepted','uncertain':'delivered_unknown',
              'connection_failed':'pending','disabled':'disabled'}[smtp]
    got=await call(app,'orders.get',{'order_id':oid},key=bk,subject=buyer)
    assert got.data['order']['email_status']==expected,wire(got)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state,claimed_at FROM store_deliveries WHERE order_id=?',(oid,))==('prepared',None)
        assert _balance(tx,buyer)==15_000_000 and _balance(tx,seller)==5_000_000
    if smtp=='disabled':
        assert not sender.messages
    else:
        assert len(sender.messages)==1
        mail=sender.messages[0]
        body=loads(mail['text'])
        assert body=={'order_id':oid,'handle':'@order-buyer',
            'pickup':app.settings.service_url+'/_orders/'+oid+'/_delivery'}
        assert mail['recipient']=='buyer@example.invalid'
        assert all(secret not in mail['text'] for secret in
            ('delivery-ok','store selftest','token','buyer@example.invalid',seller))
        assert set(mail)=={'recipient','recipient_subject','subject','text'}
    seller_view=await call(app,'orders.get',{'order_id':oid},key=sk,subject=seller)
    assert 'buyer@example.invalid' not in canonical(seller_view).decode()


@pytest.mark.asyncio
async def test_unverified_checkout_has_no_order_information_until_owner_verifies(installed):
    app,root=installed; enable_mail(app)
    sk,seller,bk,buyer,listing,_=await market(app,root)
    bought=await buy(app,bk,buyer,listing,email='new@example.invalid')
    assert bought.status=='ok' and bought.data['order']['state']=='settled',wire(bought)
    oid=bought.data['order']['id']; sender=Sender()
    await drain(app,sender)
    assert len(sender.messages)==1
    body=loads(sender.messages[0]['text'])
    assert set(body)=={'challenge_id','token','purpose'}
    assert all(value not in sender.messages[0]['text'] for value in
        (oid,buyer,seller,'order-buyer','delivery-ok','store selftest','pickup'))
    args={'order_id':oid,'challenge_id':body['challenge_id'],'token':body['token']}
    wrong=await call(app,'orders.email_verify',args,key=sk,subject=seller)
    assert wrong.error.code=='order_not_found'
    confirmed=await call(app,'orders.email_verify',args,key=bk,subject=buyer,rid='verify-order')
    assert confirmed.status=='ok',wire(confirmed)
    repeat=await call(app,'orders.email_verify',args,key=bk,subject=buyer,rid='verify-order')
    assert repeat.replayed
    other=await call(app,'orders.email_verify',args,key=bk,subject=buyer)
    assert other.error.code=='email_challenge_invalid'
    await drain(app,sender)
    assert len(sender.messages)==2 and loads(sender.messages[1]['text'])['order_id']==oid
    # Order verification does not overwrite the subject's account email preferences.
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT 1 FROM emails WHERE subject=?',(buyer,)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['disable','address','wrong_owner'])
async def test_send_time_revalidation_prevents_stale_or_cross_subject_delivery(installed,change):
    app,root=installed; enable_mail(app)
    _sk,seller,bk,buyer,listing,_=await market(app,root)
    await verified_address(app,bk,buyer,'buyer@example.invalid')
    bought=await buy(app,bk,buyer,listing,email='buyer@example.invalid'); oid=bought.data['order']['id']
    if change=='disable':
        result=await call(app,'orders.email_disable',{'order_id':oid},key=bk,subject=buyer)
        assert result.status=='ok',wire(result)
    elif change=='address':
        result=await call(app,'identity.email_set',{'address':'replacement@example.invalid'},key=bk,subject=buyer)
        assert result.status=='ok',wire(result)
    else:
        async with app.metadata.transaction(write=True) as tx:
            row=tx.one('SELECT delivery_target FROM store_orders WHERE id=?',(oid,))
            target=loads(row[0]); target['email']['subject_id']=seller
            tx.execute('UPDATE store_orders SET delivery_target=? WHERE id=?',(canonical(target).decode(),oid),write=True)
    sender=Sender(); await drain(app,sender)
    assert all(oid not in message['text'] for message in sender.messages)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state FROM store_orders WHERE id=?',(oid,))[0]=='settled'
        assert _balance(tx,seller)==5_000_000


@pytest.mark.asyncio
async def test_sealed_secret_requires_buyer_key_and_explicit_acceptance(installed,monkeypatch,tmp_path):
    import msg.security.age_keys as ages
    original=ages.generate_age_key; pairs=[]
    def generate():
        pair=original(); pairs.append(pair); return pair
    monkeypatch.setattr(ages,'generate_age_key',generate)
    app,root=installed; enable_mail(app)
    sk,seller,bk,buyer,listing,_=await market(app,root,mode='sealed_manual',kind='secret')
    private,recipient=pairs[-1]
    bought=await buy(app,bk,buyer,listing,email='new@example.invalid')
    assert bought.status=='ok',wire(bought)
    order=bought.data['order']; oid=order['id']
    encrypted=await asyncio.to_thread(subprocess.run, ['age','-r',recipient],
        input=b'private-secret-123',capture_output=True,check=True)
    ciphertext=encrypted.stdout
    private_file=tmp_path/'identity'; private_file.write_text(private+'\n'); private_file.chmod(0o600)
    decrypted=await asyncio.to_thread(subprocess.run, ['age','-d','-i',str(private_file)],
        input=ciphertext,capture_output=True,check=True)
    assert decrypted.stdout==b'private-secret-123'
    uploaded=await call(app,'content.file_put',{'parent':'/@order-seller/files','name':'encrypted.age',
        'data':b64(ciphertext),'media_type':'application/age'},key=sk,subject=seller)
    assert uploaded.status=='ok',wire(uploaded)
    args={'order_id':oid,'payload_ref':wire(uploaded.resources[0]),'payload_digest':digest(ciphertext),
          'recipient_key_id':order['recipient_key']['key_id']}
    wrong=await call(app,'delivery.submit',{**args,'recipient_key_id':'other-key'},key=sk,subject=seller)
    assert wrong.error.code=='delivery_recipient_mismatch'
    sent=await call(app,'delivery.submit',args,key=sk,subject=seller)
    assert sent.status=='ok' and sent.data['state']=='delivered',wire(sent)
    delivered=await call(app,'delivery.get',{'order_id':oid},key=bk,subject=buyer,contract_version=2)
    assert unb64(delivered.data['delivery']['payloads'][0]['data'])==ciphertext
    sender=Sender(); await drain(app,sender)
    assert all('private-secret-123' not in m['text'] for m in sender.messages)
    async with app.metadata.transaction(write=False) as tx:
        assert _balance(tx,seller)==0
    accepted=await call(app,'delivery.accept',{'order_id':oid,'delivery_digest':sent.data['delivery_digest']},
        key=bk,subject=buyer,contract_version=2)
    assert accepted.status=='ok' and accepted.data['state']=='settled',wire(accepted)


@pytest.mark.asyncio
async def test_large_service_transfer_has_buyer_acl_and_never_implies_acceptance(installed):
    app,root=installed
    sk,seller,bk,buyer,listing,_=await market(app,root,mode='service',kind='service')
    body=bytes(range(256))*8193  # Larger than 2 MiB; no oversized JSON result.
    opened=await call(app,'transfer.open',{'direction':'upload','size':len(body),'digest':digest(body),
        'requested_part_bytes':65536},key=sk,subject=seller)
    assert opened.status=='ok',wire(opened)
    tid=opened.data['transfer_id']; part=opened.data['part_bytes']
    for offset in range(0,len(body),part):
        chunk=body[offset:offset+part]
        result=await call(app,'transfer.part_put',{'transfer_id':tid,'offset':offset,
            'data':b64(chunk),'digest':digest(chunk)},key=sk,subject=seller)
        assert result.status=='ok',wire(result)
    sealed=await call(app,'transfer.seal',{'transfer_id':tid,'final_size':len(body),'final_digest':digest(body)},
        key=sk,subject=seller)
    assert sealed.status=='ok',wire(sealed)
    bought=await buy(app,bk,buyer,listing); oid=bought.data['order']['id']
    submitted=await call(app,'delivery.submit',{'order_id':oid,'payload_ref':wire(sealed.output),
        'payload_digest':digest(body)},key=sk,subject=seller)
    assert submitted.status=='ok',wire(submitted)
    before=await call(app,'delivery.get',{'order_id':oid},key=bk,subject=buyer,contract_version=2)
    assert before.status=='ok' and 'data' not in before.data['delivery']['payloads'][0],wire(before)
    args=before.data['delivery']['payloads'][0]['transfer']['arguments']
    exported=await call(app,'delivery.transfer_open',args,key=bk,subject=buyer)
    assert exported.status=='ok',wire(exported)
    transfer=exported.data['transfer_id']; output=bytearray(); part=exported.data['part_bytes']
    for offset in range(0,len(body),part):
        result=await call(app,'transfer.part_get',{'transfer_id':transfer,'offset':offset,
            'length':min(part,len(body)-offset)},key=bk,subject=buyer)
        assert result.status=='ok',wire(result)
        output.extend(unb64(result.data['data']))
    assert bytes(output)==body
    wrong=await call(app,'transfer.part_get',{'transfer_id':transfer,'offset':0,'length':1},key=sk,subject=seller)
    assert wrong.error.code=='transfer_owner_required'
    async with app.metadata.transaction(write=False) as tx:
        resource=await tx.resource(exported.resources[0].id)
        assert resource.owner==buyer and resource.mode==0o600
        assert _balance(tx,seller)==0
        assert tx.one('SELECT state FROM store_deliveries WHERE order_id=?',(oid,))[0]=='prepared'
        assert tx.one('SELECT state FROM store_orders WHERE id=?',(oid,))[0]=='delivered'
