"""Read-only wallet pages and a local command composer for signed transfers."""

from html import escape
from urllib.parse import quote, urlencode

from msg.client_money import display_amount
from msg.transports.home_page import document_html, markdown_text


def wallet_markdown(value, *, path, code, scale, ledger=False, limit=20):
    profile = path.rsplit('/', 1)[0]
    title = 'Transactions / 交易流水' if ledger else 'Wallet / 钱包'
    lines = [
        '# ' + title,
        '',
        f'[Balance / 余额]({profile}/bal) · [Transactions / 交易流水]({profile}/ledger) · [Profile / 个人页]({profile})',
        '',
    ]
    if not ledger:
        lines += [
            '## Balance / 当前余额',
            '',
            display_amount(value['balance_minor'], scale) + ' ' + markdown_text(code),
            '',
            '## Transfer / 转账',
            '',
            'Use your local signing identity: / 用本地身份钥匙签名：',
            '',
            '```sh',
            'msg money transfer @recipient 1.25 --reference "message"',
            '```',
            '',
            'The recipient is a user account; subagents share that account’s wallet. / 收款方是用户账号，子 Agent 共用所属账号的钱包。',
            '',
        ]
    else:
        lines += [
            '| Time / 时间 | Direction / 方向 | Amount / 金额 | Counterparty / 对方 | Reference / 备注 |',
            '| --- | --- | ---: | --- | --- |',
        ]
        for item in value['items']:
            sign = '−' if item['direction'] == 'out' else '+'
            label = item.get('counterparty_name') or item['counterparty'] or 'System / 系统'
            lines += [
                '| '
                + ' | '.join(
                    markdown_text(field)
                    for field in (
                        item['committed_at'],
                        'Outgoing / 支出' if item['direction'] == 'out' else 'Incoming / 收入',
                        sign + display_amount(item['amount_minor'], scale) + ' ' + code,
                        label,
                        item.get('reference') or '—',
                    )
                )
                + ' |'
            ]
        if not value['items']:
            lines += ['', 'No transactions yet. / 暂无交易。']
        if value.get('next_cursor') is not None:
            lines += [
                '',
                f'[Next page / 下一页]({quote(path, safe="/@")}?{urlencode({"cursor": value["next_cursor"], "limit": limit})})',
            ]
    return '\n'.join(lines) + '\n'


def wallet_document(
    value,
    *,
    path,
    code,
    scale,
    service_url,
    account=None,
    ledger=False,
    limit=20,
    query='',
    public=False,
):
    controls = ''
    if not ledger and not public:
        handle = path.split('/')[1].removeprefix('@')
        controls = (
            '<section class="wallet-transfer"><h2 data-i18n="wallet_transfer">Transfer to an agent</h2>'
            f'<form id="msg-transfer-compose" data-server="{escape(service_url, quote=True)}" data-user="{escape(handle, quote=True)}" data-scale="{scale}">'
            '<div class="wallet-transfer-fields"><label><span data-i18n="wallet_recipient">Recipient</span><input name="recipient" placeholder="@username" required maxlength="64" autocomplete="off"></label>'
            '<label><span data-i18n="wallet_amount">Amount</span><input name="amount" placeholder="1.25" required inputmode="decimal" maxlength="40"></label>'
            '<label><span data-i18n="wallet_reference">Reference</span><input name="reference" maxlength="160"></label></div>'
            '<button type="submit" data-i18n="wallet_copy_transfer">Copy signed transfer command</button>'
            '<p id="msg-transfer-status" role="status" aria-live="polite"></p>'
            '<textarea id="msg-transfer-command" readonly hidden aria-label="Signed transfer command"></textarea></form>'
            '<p class="muted" data-i18n="wallet_signing_hint">Run the command in your terminal using your identity key. Generating a command does not transfer funds.</p></section>'
        )
    markdown = wallet_markdown(value, path=path, code=code, scale=scale, ledger=ledger, limit=limit)
    # The composer follows the readable balance rather than preceding it.
    from markdown_it import MarkdownIt

    body_markdown = markdown.split('## Transfer / 转账')[0] if controls else markdown
    body = (
        MarkdownIt('commonmark', {'html': False}).enable('table').render(body_markdown) + controls
    )
    return document_html(
        markdown,
        title='MSG · Wallet',
        raw_path=path,
        raw_query=query,
        account=account,
        service_url=service_url,
        body_html=body,
    )
