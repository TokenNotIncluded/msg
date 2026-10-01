"""Friendly signed wallet commands, without importing server implementation."""

import re

from msg.core.errors import require


def amount_minor(value, scale):
    require(type(scale) is int and 0 <= scale <= 18, 'invalid_currency_scale')
    require(
        isinstance(value, str)
        and len(value) <= 80
        and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value) is not None,
        'invalid_money_amount',
    )
    whole, _, fraction = value.partition('.')
    require(len(fraction) <= scale, 'money_amount_precision')
    minor = int(whole) * 10**scale + int(fraction.ljust(scale, '0') or '0')
    require(0 < minor <= 2**63 - 1, 'invalid_money_amount')
    return minor


def display_amount(minor, scale):
    require(type(minor) is int and type(scale) is int and 0 <= scale <= 18, 'invalid_money_amount')
    sign = '-' if minor < 0 else ''
    whole, fraction = divmod(abs(minor), 10**scale)
    return sign + str(whole) + ('.' + str(fraction).zfill(scale).rstrip('0') if fraction else '')


async def run_command(client, args):
    currency = client.checked(await client.call('money.state', anonymous=True)).data
    scale = currency['scale']
    if args.action == 'balance':
        result = client.checked(await client.call('money.balance'))
    elif args.action == 'ledger':
        require(1 <= args.limit <= 100 and args.cursor >= 0, 'invalid_limit')
        result = client.checked(
            await client.call('money.ledger', {'limit': args.limit, 'cursor': args.cursor})
        )
    else:
        minor = amount_minor(args.amount, scale)
        recipient = args.recipient
        require('#' not in recipient, 'money_recipient_must_be_account')
        if not recipient.startswith('u_'):
            path = recipient if recipient.startswith('/@') else '/@' + recipient.lstrip('@')
            profile = client.checked(
                await client.call('discovery.get', {'id': path, 'fields': ['id', 'name', 'type']})
            ).data
            require(profile.get('type') == 'user', 'invalid_money_recipient')
            recipient = profile['id']
        signer = client.signer_override or client.state.signer
        require(signer is not None, 'money_transfer_signer_required')
        request = {
            'to_subject': recipient,
            'currency_id': currency['currency_id'],
            'amount_minor': minor,
        }
        if args.reference:
            request['reference'] = args.reference
        kwargs = {'signer': signer}
        if args.request_id:
            kwargs['request_id'] = args.request_id
        result = client.checked(await client.call('money.transfer', request, **kwargs))
    data = {**result.data, 'code': currency['code'], 'scale': scale}
    if 'balance_minor' in data:
        data['balance'] = display_amount(data['balance_minor'], scale)
    if 'items' in data:
        data['items'] = [
            {**item, 'amount': display_amount(item['amount_minor'], scale)}
            for item in data['items']
        ]
    return {'status': 'ok', 'data': data}
