"""Certificate representations; all displayed authority comes from authorized reads."""

from datetime import UTC, datetime
from html import escape
from urllib.parse import quote

from msg.core.codec import canonical
from msg.transports.home_page import document_html, markdown_text


def status(value, now):
    cert = value['certificate']
    if value.get('revoked'):
        return 'revoked'
    if now < datetime.fromisoformat(cert['not_before'].replace('Z', '+00:00')):
        return 'pending'
    if now >= datetime.fromisoformat(cert['expires_at'].replace('Z', '+00:00')):
        return 'expired'
    return 'active'


def text(key, fallback):
    return f'<span data-i18n="cert_{key}">{escape(fallback)}</span>'


def identity(value, key):
    cert = value['certificate']
    person = value.get('people', {}).get(cert[key])
    if person:
        return f'<a href="{escape(quote(person["path"], safe="/@*&"), quote=True)}">{escape(person["name"])}</a>'
    return f'<span class="cert-identifier">{escape(cert[key])}</span>'


def certificate_markdown(value, *, now=None):
    now = now or datetime.now(UTC)
    if 'certificates' in value:
        path = quote(value['path'].removesuffix('/'), safe='/@*&')
        lines = ['# Certificates / 证书', '']
        for item in value['certificates']:
            state = 'Revoked / 已撤销' if item['revoked'] else 'Not revoked / 未撤销'
            lines.append(
                f'- [{markdown_text(item["id"])}]({path}/{quote(item["id"], safe="")}) · {state}'
            )
        if not value['certificates']:
            lines.append('No certificates yet. / 暂无证书。')
        return '\n'.join(lines) + '\n'
    cert = value['certificate']
    lines = ['# Authorization certificate / 授权证书', '']
    for label, field in [
        ('Type / 类型', 'kind'),
        ('Holder / 持有人', 'subject_id'),
        ('Issuer / 签发者', 'issuer_id'),
        ('Serial / 编号', 'serial'),
        ('Valid from / 生效', 'not_before'),
        ('Valid until / 截止', 'expires_at'),
        ('Service / 服务', 'target_service'),
    ]:
        lines.append(f'- {label}: {markdown_text(cert[field])}')
    lines.extend([
        f'- Status / 状态: {status(value, now)}',
        '',
        '## Authorization scope / 授权范围',
        '',
    ])
    for grant in cert['grants']:
        scope = grant['scope']
        lines.extend([
            f'### {markdown_text(grant["capability"])} · v{grant["version"]}',
            '',
            f'- Scope: {markdown_text(scope["resource_id"])}',
            f'- Includes descendants: {scope["descendants"]}',
            '- Operations: ' + ', '.join(markdown_text(op) for op in sorted(grant['operations'])),
            '- Constraints: `'
            + canonical(grant['constraints']).decode().replace('`', '\\u0060')
            + '`',
            '',
        ])
    lines.extend(['## Signed data / 签名数据', '', '```json', canonical(cert).decode(), '```', ''])
    return '\n'.join(lines)


SEAL = '<svg class="certificate-seal" viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="42"/><circle cx="50" cy="50" r="36"/><path d="M25 33h50M25 67h50M18 50h7M75 50h7"/><text x="50" y="56" text-anchor="middle">MSG</text></svg>'


def certificate_paper(value, *, now, compact=False, href=None):
    cert = value['certificate']
    state = status(value, now)
    kind = cert['kind']

    def field(key, label, shown):
        return f'<div><dt>{text(key, label)}</dt><dd>{shown}</dd></div>'

    header = 'h2' if compact else 'h1'
    output = [
        f'<article class="certificate-paper{" certificate-compact" if compact else ""}" data-status="{state}">',
        '<div class="certificate-heading">',
        f'<{header}>{text(kind, kind.capitalize() + " certificate")}</{header}>',
        f'<span class="certificate-state">{text(state, {"active": "Within validity period", "revoked": "Revoked", "expired": "Expired", "pending": "Not yet valid"}[state])}</span></div>',
        f'<p class="certificate-holder-label">{text("holder", "Issued to")}</p>',
        '<p class="certificate-holder">' + identity(value, 'subject_id') + '</p>',
        '<dl class="certificate-fields">',
        field('issuer', 'Issued by', identity(value, 'issuer_id')),
        field(
            'serial',
            'Serial number',
            f'<span class="cert-identifier">{escape(cert["serial"])}</span>',
        ),
        field(
            'start',
            'Valid from',
            f'<time datetime="{escape(cert["not_before"], quote=True)}">{escape(cert["not_before"][:19].replace("T", " "))} UTC</time>',
        ),
        field(
            'end',
            'Valid until',
            f'<time datetime="{escape(cert["expires_at"], quote=True)}">{escape(cert["expires_at"][:19].replace("T", " "))} UTC</time>',
        ),
        field('service', 'Issued for', escape(cert['target_service'])),
        '</dl>',
    ]
    if not compact:
        output += [
            '<section class="certificate-grants"><h2>'
            + text('grants', 'Authorization scope')
            + '</h2><ul>'
        ]
        for grant in cert['grants']:
            scope = grant['scope']
            output += [
                '<li><details><summary><strong>'
                + escape(grant['capability'])
                + f'</strong> <span>v{grant["version"]}</span></summary>',
                '<p><code>'
                + escape(scope['resource_id'])
                + '</code> · '
                + text(
                    'descendants' if scope['descendants'] else 'no_descendants',
                    'Includes descendants' if scope['descendants'] else 'This resource only',
                )
                + '</p>',
                '<ul class="certificate-operations">'
                + ''.join(
                    '<li><code>' + escape(op) + '</code></li>' for op in sorted(grant['operations'])
                )
                + '</ul>',
                '<pre>'
                + escape(canonical(grant['constraints']).decode())
                + '</pre></details></li>',
            ]
        output += ['</ul></section>']
    output += [
        '<footer class="certificate-signature">' + SEAL,
        '<div><span>'
        + text('issuer', 'Issued by')
        + '</span><p>'
        + identity(value, 'issuer_id')
        + '</p><small>'
        + escape(cert['signature']['algorithm'])
        + '</small></div>',
        '</footer>',
    ]
    if href:
        output += [
            f'<a class="certificate-open" href="{escape(quote(href, safe="/@*&"), quote=True)}">{text("open", "View certificate")} →</a>'
        ]
    image = {
        key: cert[key]
        for key in ('kind', 'serial', 'not_before', 'expires_at', 'target_service', 'resource_id')
    }
    image.update(
        status=state,
        holder=value.get('people', {}).get(cert['subject_id'], {}).get('name', cert['subject_id']),
        issuer=value.get('people', {}).get(cert['issuer_id'], {}).get('name', cert['issuer_id']),
        algorithm=cert['signature']['algorithm'],
        grants=[
            {
                'name': grant['capability'],
                'version': grant['version'],
                'scope': grant['scope']['resource_id'],
                'descendants': grant['scope']['descendants'],
            }
            for grant in cert['grants']
        ],
    )
    output += [
        f'<button type="button" class="certificate-copy-image" data-cert-image="{escape(canonical(image).decode(), quote=True)}" data-i18n="cert_copy_image">Copy certificate image</button>'
    ]
    output += ['</article>']
    if not compact:
        output += [
            '<p class="certificate-note">'
            + text(
                'note',
                'Dates and revocation describe this certificate only. The server checks current authorization for every operation.',
            )
            + '</p>',
            '<details class="certificate-technical"><summary>'
            + text('details', 'Signature and technical details')
            + '</summary><pre>'
            + escape(canonical(cert).decode())
            + '</pre></details>',
        ]
    return ''.join(output)


def certificate_document(value, *, now, account=None, raw_path='/', previews=(), service_url=None):
    if 'certificate' in value:
        body = certificate_paper(value, now=now)
    else:
        body = (
            '<h1>'
            + text('collection', 'Certificates')
            + '</h1><div class="certificate-collection">'
        )
        for item, preview in zip(value['certificates'], previews, strict=True):
            href = value['path'].rstrip('/') + '/' + item['id']
            if preview:
                body += certificate_paper(preview, now=now, compact=True, href=href)
            else:
                body += (
                    '<section class="certificate-unavailable"><p class="cert-identifier">'
                    + escape(item['id'])
                    + '</p><p>'
                    + text('open', 'View certificate')
                    + f'</p><a href="{escape(quote(href, safe="/@*&"), quote=True)}">'
                    + text('open', 'View certificate')
                    + '</a></section>'
                )
        if not value['certificates']:
            body += '<p>' + text('empty', 'No certificates yet.') + '</p>'
        body += '</div>'
    return document_html(
        certificate_markdown(value, now=now),
        title='MSG · Certificates',
        account=account,
        raw_path=raw_path,
        body_html=body,
        service_url=service_url,
    )
