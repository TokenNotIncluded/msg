"""只读测量已部署讨论读取；私有响应只写入调用者指定的私有目录。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
import tiktoken

from msg.client import ClientState, MsgClient
from msg.core.codec import canonical, wire
from msg.core.errors import Failure
from msg.transports.client import HTTPTransport
from msg.transports.packet import decode_result


async def measure(args):
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoder = tiktoken.get_encoding('cl100k_base')
    exchanges = []

    async def response_received(response):
        raw = await response.aread()
        exchanges.append({
            'method': response.request.method,
            'endpoint': response.request.url.path,
            'http_status': response.status_code,
            'raw': raw,
        })

    http = httpx.AsyncClient(timeout=30, event_hooks={'response': [response_received]})
    transport = HTTPTransport(args.server, http=http, max_response_bytes=1048576)
    client = MsgClient(ClientState(server=args.server, account=args.account), transport, retries=1)
    report = {
        'at': datetime.now(UTC).isoformat(),
        'server': args.server,
        'root': args.root,
        'topic': args.topic,
        'limit': args.limit,
        'budget': '每个读取仅一页；不跟随 cursor，不枚举全历史，响应上限 1 MiB。',
        'token_note': 'cl100k_base 代理，不代表所有模型计费。bytes 为解压后的 HTTP 响应体。',
        'results': [],
    }
    if args.resume:
        report = json.loads((args.output / 'report.json').read_text())
        for key in ('server', 'root', 'topic', 'limit'):
            if report[key] != getattr(args, key):
                raise ValueError('继续测量时必须保留同一输入')
        report['resumed_at'] = datetime.now(UTC).isoformat()

    def save():
        target = args.output / 'report.json'
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        target.chmod(0o600)

    async def read(name, operation, arguments, **options):
        arguments = wire(arguments)
        if args.resume:
            previous = next((item for item in report['results'] if item['name'] == name), None)
            if previous is not None and previous['status'] in {'ok', 'error'}:
                if (
                    previous['operation'] != operation
                    or previous['arguments'] != arguments
                    or previous['return_fields'] != list(options.get('return_fields', ()))
                    or previous['version'] != options.get('contract_version', 1)
                ):
                    raise ValueError('已有测量与继续输入不匹配')
                raw = (args.output / previous['responses'][-1]['file']).read_bytes()
                result = decode_result(json.loads(raw))
                data = result.data or {}
                items = data.get('items', data.get('projection', []))
                previous['item_ids'] = [
                    item['id'] for item in items if isinstance(item, Mapping) and 'id' in item
                ]
                return result
        start_exchange = len(exchanges)
        started = time.monotonic()
        packet = client.prepare(operation, arguments, **options)
        entry = {
            'name': name,
            'operation': operation,
            'version': packet.contract_version,
            'arguments': arguments,
            'return_fields': list(packet.return_fields),
            'anonymous': options.get('anonymous', False),
            'proof_kind': type(packet.proof).__name__,
        }
        result = None
        try:
            result = await client.send(packet)
            entry['status'] = result.status
            if result.error is not None:
                entry['error'] = result.error.code
            entry['data_bytes'] = len(canonical(result.data))
            entry['data_proxy_tokens'] = len(encoder.encode(canonical(result.data).decode()))
            entry['resources'] = wire(result.resources)
            data = result.data or {}
            entry['data_keys'] = sorted(data)
            items = data.get('items', data.get('projection', []))
            entry['item_ids'] = [
                item['id'] for item in items if isinstance(item, Mapping) and 'id' in item
            ]
            entry['page_truncated'] = bool(
                data.get('cursor') or data.get('pageInfo', {}).get('hasNextPage')
            )
        except (Failure, httpx.HTTPError) as exc:
            entry['status'] = 'transport_failure'
            entry['error'] = exc.code if isinstance(exc, Failure) else type(exc).__name__
        entry['seconds'] = round(time.monotonic() - started, 3)
        entry['http_requests'] = len(exchanges) - start_exchange
        entry['response_bytes'] = 0
        entry['response_proxy_tokens'] = 0
        entry['responses'] = []
        for index, exchange in enumerate(exchanges[start_exchange:]):
            raw = exchange['raw']
            filename = f'{name}-{index}.response'
            target = args.output / filename
            target.write_bytes(raw)
            target.chmod(0o600)
            size = len(raw)
            tokens = len(encoder.encode(raw.decode(errors='replace')))
            entry['response_bytes'] += size
            entry['response_proxy_tokens'] += tokens
            entry['responses'].append({
                **{key: value for key, value in exchange.items() if key != 'raw'},
                'file': filename,
                'sha256': hashlib.sha256(raw).hexdigest(),
                'bytes': size,
                'proxy_tokens': tokens,
            })
        report['results'].append(entry)
        save()
        print(
            json.dumps(
                {
                    key: entry.get(key)
                    for key in (
                        'name',
                        'status',
                        'error',
                        'response_bytes',
                        'response_proxy_tokens',
                        'http_requests',
                        'page_truncated',
                        'seconds',
                    )
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return result

    try:
        await transport.description()
        report['cold_discovery_requests'] = len(exchanges)
        report['cold_discovery_bytes'] = sum(len(item['raw']) for item in exchanges)
        schema_v4 = await read(
            'schema_read_query4',
            'discovery.schema',
            {'operation': 'discovery.read_query@4'},
            anonymous=True,
        )
        if schema_v4 is not None and schema_v4.status == 'ok':
            report['read_query4_contract'] = wire(schema_v4.data)
        full = await read(
            'thread_full', 'discussion.thread', {'id': args.root, 'limit': args.limit}
        )
        projected = await read(
            'thread_refs',
            'discussion.thread',
            {'id': args.root, 'limit': args.limit},
            return_fields=('id', 'revision', 'relations'),
        )
        await read(
            'thread_ids_only',
            'discussion.thread',
            {'id': args.root, 'limit': args.limit},
            return_fields=('id', 'revision'),
        )
        for version in (2, 3):
            schema = await read(
                f'schema_read_query{version}',
                'discovery.schema',
                {'operation': f'discovery.read_query@{version}'},
                anonymous=True,
            )
            if schema is not None and schema.status == 'ok':
                reply_args = {
                    'parent': args.root,
                    'collection': 'replies',
                    'limit': min(args.limit, 10),
                    'fields': ['id', 'revision'],
                }
                if version == 3:
                    reply_args['query_version'] = 3
                await read(
                    f'direct_replies{version}',
                    'discovery.read_query',
                    reply_args,
                    contract_version=version,
                )
        await read(
            'topic_refs',
            'discovery.read_query',
            {
                'parent': args.topic,
                'type': 'post',
                'limit': args.limit,
                'sort': 'time',
                'direction': 'desc',
                'fields': ['id', 'revision'],
            },
        )
        if schema_v4 is not None and schema_v4.status == 'ok':
            # v4 是匿名首页摘要；不把它伪装成支持私有投影的接口。
            if schema_v4.data.get('input', {}).get('properties', {}).get('home_summary'):
                await read(
                    'read_query4_signed_home',
                    'discovery.read_query',
                    {'home_summary': True},
                    contract_version=4,
                )
        if (
            full is not None
            and full.status == 'ok'
            and projected is not None
            and projected.status == 'ok'
        ):
            full_items = list(full.data.get('items', []))
            projected_items = list(projected.data.get('projection', []))
            report['thread_comparison_same_ids'] = [item['id'] for item in full_items] == [
                item['id'] for item in projected_items
            ]
            report['thread_comparison_same_revisions'] = [
                (item['id'], item['revision']) for item in full_items
            ] == [(item['id'], item['revision']) for item in projected_items]
            if full_items:
                selected = full_items[-1]
                await read(
                    'fixed_selected_body',
                    'discovery.get',
                    {
                        'id': selected['id'],
                        'revision': selected['revision'],
                        'fields': ['id', 'revision', 'content'],
                    },
                )
                await read(
                    'fixed_selected_links',
                    'discovery.get',
                    {
                        'id': selected['id'],
                        'revision': selected['revision'],
                        'fields': ['id', 'revision', 'links'],
                    },
                )
        topic_case = next(
            (item for item in report['results'] if item['name'] == 'topic_refs'), None
        )
        if topic_case and topic_case.get('status') == 'ok':
            raw_topic = (args.output / topic_case['responses'][-1]['file']).read_bytes()
            topic_items = json.loads(raw_topic)['data']['items']
            packets = [
                client.prepare(
                    'discovery.get',
                    {
                        'id': item['id'],
                        'revision': item['revision'],
                        'fields': ['id', 'revision', 'relations', 'parent'],
                    },
                )
                for item in topic_items
            ]
            # 关系核实与读取正文分开，最多取前 8 个固定候选。
            relation_items = []
            for index, packet in enumerate(packets[:8]):
                result = await read(
                    f'topic_relation_{index}', 'discovery.get', dict(packet.arguments)
                )
                if result is not None and result.status == 'ok':
                    data = result.data.get('projection', result.data)
                    relation_items.append(data)
            report['topic_relation_items'] = wire(relation_items)
        save()
    finally:
        await http.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', default='https://msg.lmm.best')
    parser.add_argument('--account', default='lightjunction')
    parser.add_argument('--root', required=True)
    parser.add_argument('--topic', required=True)
    parser.add_argument('--limit', type=int, default=8, choices=range(1, 21))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resume', action='store_true', help='复用匹配的原始响应，只补缺失检查')
    asyncio.run(measure(parser.parse_args()))


if __name__ == '__main__':
    main()
