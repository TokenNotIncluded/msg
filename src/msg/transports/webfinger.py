"""RFC 7033 public account discovery; browser credentials never expand visibility."""

from datetime import timedelta
from urllib.parse import quote, urlsplit

from starlette.responses import Response

from msg.core.codec import canonical
from msg.core.errors import Failure, require
from msg.core.internet_address import (
    DELIVERY_REL,
    KEYS_REL,
    PROFILE_REL,
    SUBJECT_PROPERTY,
    account_address,
    address_parts,
)
from msg.core.requests import request_for
from msg.transports.http_common import BASE_HEADERS, error_status, json_response


async def webfinger(service, request):
    headers = {'Access-Control-Allow-Origin': '*'}
    try:
        require(request.method in {'GET', 'HEAD', 'OPTIONS'}, 'method_not_allowed')
        if request.method == 'OPTIONS':
            return Response(
                status_code=204,
                headers={
                    **BASE_HEADERS,
                    **headers,
                    'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
                },
            )
        resources = request.query_params.getlist('resource')
        require(len(resources) == 1, 'invalid_request')
        resource = resources[0]
        origin = service.settings.service_url
        if resource.startswith('acct:'):
            handle, authority = address_parts(resource[5:])
            require(authority == urlsplit(origin).netloc.lower(), 'not_found')
        else:
            parsed = urlsplit(resource)
            require(
                parsed.scheme == urlsplit(origin).scheme
                and parsed.netloc == urlsplit(origin).netloc
                and not parsed.query
                and not parsed.fragment
                and parsed.path.startswith('/@'),
                'not_found',
            )
            handle, _ = address_parts(parsed.path[2:] + '@' + urlsplit(origin).netloc)
        result = await service.executor.execute(
            request_for(
                'discovery.get',
                {'id': '/@' + handle, 'fields': ['id', 'name', 'type', 'state']},
                origin,
                expires_at=service.clock() + timedelta(minutes=3),
            )
        )
        require(
            not result.error
            and result.data.get('type') == 'user'
            and result.data.get('state') == 'active'
            and result.data.get('name') == '@' + handle,
            'not_found',
        )
        profile = origin + quote('/@' + handle, safe='/@')
        address = account_address('@' + handle, origin)
        links = [
            {'rel': PROFILE_REL, 'type': 'text/html', 'href': profile},
            {'rel': KEYS_REL, 'type': 'application/json', 'href': profile + '/k'},
        ]
        try:
            service.registry.operation('communication.internet_receive')
        except Failure:
            pass
        else:
            links.append({
                'rel': DELIVERY_REL,
                'type': 'application/json',
                'href': origin + '/-/p/communication.internet_receive',
            })
        rels = request.query_params.getlist('rel')
        if rels:
            links = [link for link in links if link['rel'] in rels]
        document = {
            'subject': 'acct:' + address,
            'aliases': [profile],
            'properties': {SUBJECT_PROPERTY: result.data['id']},
            'links': links,
        }
        return Response(
            canonical(document),
            media_type='application/jrd+json',
            headers={**BASE_HEADERS, **headers},
        )
    except (Failure, ValueError) as exc:
        failure = exc if isinstance(exc, Failure) else Failure('invalid_request')
        return json_response(
            {'status': 'error', 'error': failure.as_dict()},
            error_status(failure.code),
            headers=headers,
        )
