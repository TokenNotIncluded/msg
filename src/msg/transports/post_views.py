"""Approximate browser heat: visible-page events, current ACLs and daily dedupe."""

import hashlib
import hmac
import secrets
import time
from base64 import b64encode
from datetime import timedelta
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from msg.core.codec import b64, canonical, loads, unb64
from msg.core.errors import Failure, require
from msg.core.requests import request_for
from msg.security.oauth import OAuthService
from msg.security.quarantine import require_live_authority
from msg.storage.post_view_migration import record_view
from msg.transports.http_common import body_bytes, error_status, json_response
from msg.transports.url_safety import require_matching_host, require_safe_request_target

ENDPOINT = '/-/view-event'
TOKEN_TTL = 1800
SCRIPT = r"""(()=>{
const marker=document.querySelector('[data-msg-view-token]'),content=document.querySelector('#content');
if(!marker||!content)return;
let visible=false,timer=null,sent=false;
const update=()=>{
  clearTimeout(timer);timer=null;
  if(sent||!visible||document.visibilityState!=='visible')return;
  timer=setTimeout(async()=>{
    if(sent||!visible||document.visibilityState!=='visible')return;
    sent=true;observer.disconnect();document.removeEventListener('visibilitychange',update);
    try{
      const response=await fetch('/-/view-event',{method:'POST',credentials:'same-origin',redirect:'error',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:marker.dataset.msgViewToken}),signal:AbortSignal.timeout(10000)});
      if(!response.ok)return;
      const data=await response.json(),count=document.querySelector('[data-msg-view-count]');
      if(count&&Number.isSafeInteger(data.view_count))count.textContent=String(data.view_count);
    }catch{}
  },1000);
};
const observer=new IntersectionObserver(entries=>{visible=entries.some(entry=>entry.isIntersecting);update()});
observer.observe(content);document.addEventListener('visibilitychange',update);
})();"""
HASH = b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()


class PostViews:
    def __init__(self, service):
        self.service = service
        self.secure = urlsplit(service.settings.service_url).scheme == 'https'
        self.cookie_name = '__Host-msg_view' if self.secure else 'msg_view'
        self.buckets = {}
        self.global_bucket = (time.monotonic(), 100.0)

    @property
    def key(self):
        return hmac.digest(self.service._token_secret, b'msg-post-views-v1', 'sha256')

    def _sign(self, purpose, value):
        return b64(hmac.digest(self.key, purpose.encode() + b':' + value.encode(), 'sha256'))

    def _cookie(self, value):
        if not isinstance(value, str) or not value.isascii() or len(value) != 76:
            return False
        seed, _, signature = value.partition('.')
        return len(seed) == 32 and hmac.compare_digest(signature, self._sign('cookie', seed))

    def _day(self):
        return self.service.clock().astimezone(ZoneInfo('Asia/Taipei')).date().isoformat()

    def _visitor(self, cookie, subject, day):
        # Separate daily HMACs cannot be used to reconstruct browsing history.
        return self._sign('visitor', day + ':' + ('subject:' + subject if subject else cookie))

    async def prepare(self, request, resource):
        """Sign an already-authorized rendered post; never write on GET/HEAD."""
        if request.method != 'GET' or resource.get('type') != 'post':
            return '', None
        async with self.service.metadata.transaction(write=False) as tx:
            current = await tx.resource(resource['id'])
            if current.state != 'active':
                return '', None
        cookie = request.cookies.get(self.cookie_name, '')
        fresh = not self._cookie(cookie)
        if fresh:
            seed = b64(secrets.token_bytes(24))
            cookie = seed + '.' + self._sign('cookie', seed)
        browser = request.scope.get('state', {}).get('msg_browser_credentials')
        day = self._day()
        payload = b64(
            canonical({
                'id': resource['id'],
                'day': day,
                'visitor': self._visitor(cookie, browser[0] if browser else None, day),
                'cookie': self._sign('binding', cookie),
                'issued': int(self.service.clock().timestamp()),
            })
        )
        token = payload + '.' + self._sign('event', payload)
        markup = '<span hidden data-msg-view-token="' + escape(token, quote=True) + '"></span>'
        markup += '<script>' + SCRIPT + '</script>'
        return markup, cookie if fresh else None

    def decorate(self, response, markup, cookie):
        if not markup:
            return response
        response.body = response.body.replace(b'</body>', markup.encode() + b'</body>')
        response.headers['Content-Length'] = str(len(response.body))
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Vary'] = 'Accept, Cookie'
        csp = response.headers['Content-Security-Policy']
        response.headers['Content-Security-Policy'] = csp.replace(
            'script-src ', f"script-src 'sha256-{HASH}' "
        )
        # Browser POSTs need a non-opaque Origin in Chromium.
        response.headers['Referrer-Policy'] = 'strict-origin'
        if cookie:
            response.set_cookie(
                self.cookie_name,
                cookie,
                max_age=2592000,
                secure=self.secure,
                httponly=True,
                samesite='lax',
                path='/',
            )
        return response

    def _limit(self, visitor):
        now = time.monotonic()
        previous, tokens = self.global_bucket
        tokens = min(100.0, tokens + (now - previous) * 50)
        self.global_bucket = (now, max(0.0, tokens - 1))
        require(tokens >= 1, 'view_rate_limited', retryable=True)
        if visitor not in self.buckets and len(self.buckets) >= 4096:
            self.buckets = {
                key: value for key, value in self.buckets.items() if now - value[0] < 60
            }
            require(len(self.buckets) < 4096, 'view_rate_limited', retryable=True)
        previous, tokens = self.buckets.get(visitor, (now, 20.0))
        tokens = min(20.0, tokens + (now - previous) / 3)
        self.buckets[visitor] = (now, max(0.0, tokens - 1))
        require(tokens >= 1, 'view_rate_limited', retryable=True)

    async def event(self, request):
        try:
            require(request.method == 'POST', 'method_not_allowed')
            require_safe_request_target(
                request.scope.get('raw_path') or request.url.path.encode(),
                request.scope.get('query_string', b''),
                maximum=2048,
            )
            require(request.url.path == ENDPOINT and not request.url.query, 'invalid_request')
            selected = require_matching_host(
                request.headers.getlist('host'),
                urlsplit(self.service.settings.service_url),
                aliases=getattr(self.service.settings, 'service_aliases', ()),
            )
            require(
                request.headers.getlist('origin') == [f'{selected.scheme}://{selected.netloc}'],
                'forbidden_origin',
            )
            require(
                request.headers.get('sec-fetch-site') in {None, 'same-origin'}, 'forbidden_origin'
            )
            require(
                not any(
                    name in request.headers
                    for name in (
                        'authorization',
                        'x-msg-request',
                        'x-http-method-override',
                        'x-method-override',
                    )
                ),
                'ambiguous_credentials',
            )
            require(
                request.headers.get('content-type', '').split(';')[0] == 'application/json',
                'invalid_request',
            )
            args = loads(await body_bytes(request, 2048))
            require(
                isinstance(args, dict)
                and set(args) == {'token'}
                and isinstance(args['token'], str)
                and args['token'].isascii(),
                'invalid_request',
            )
            payload, _, signature = args['token'].partition('.')
            require(
                hmac.compare_digest(signature, self._sign('event', payload)), 'invalid_view_token'
            )
            data = loads(unb64(payload, limit=1024))
            require(
                isinstance(data, dict)
                and set(data) == {'id', 'day', 'visitor', 'cookie', 'issued'},
                'invalid_view_token',
            )
            now = self.service.clock()
            require(
                type(data['issued']) is int
                and 0 <= now.timestamp() - data['issued'] <= TOKEN_TTL
                and data['day'] == self._day(),
                'expired_view_token',
            )
            cookie = request.cookies.get(self.cookie_name, '')
            require(
                self._cookie(cookie)
                and hmac.compare_digest(data['cookie'], self._sign('binding', cookie)),
                'invalid_view_token',
            )
            self._limit(data['visitor'])
            async with self.service.metadata.transaction(write=True) as tx:
                self.service.runtime_generation.require_current(tx)
                require_live_authority(tx)
                require(not self.service.executor.recovery_drill_active(), 'recovery_quarantined')
                require(
                    tx.setting('runtime_config', {}).get('accept_writes', True), 'writes_paused'
                )
                browser = None
                session_name = '__Host-msg_session' if self.secure else 'msg_session'
                session = request.cookies.get(session_name, '')
                if session and self.service.settings.oauth and self.service.settings.oauth.enabled:
                    browser = await OAuthService(self.service).browser_credentials(tx, session)
                require(
                    hmac.compare_digest(
                        data['visitor'],
                        self._visitor(cookie, browser[0] if browser else None, data['day']),
                    ),
                    'invalid_view_token',
                )
                packet = request_for(
                    'discovery.get',
                    {'id': data['id'], 'view': 'meta'},
                    self.service.settings.service_url,
                    subject=browser[0] if browser else None,
                    token=(browser[1], browser[2]) if browser else None,
                    source='manual',
                    expires_at=now + timedelta(minutes=1),
                )
                readable = await self.service.executor.execute(packet)
                if readable.error:
                    raise Failure(readable.error.code)
                require(
                    readable.data.get('type') == 'post' and readable.data.get('state') == 'active',
                    'invalid_view_target',
                )
                count = record_view(
                    tx,
                    data['id'],
                    data['day'],
                    data['visitor'],
                    self._visitor(cookie, None, data['day']),
                )
            return json_response({'view_count': count})
        except Failure as exc:
            status = (
                429
                if exc.code == 'view_rate_limited'
                else 405
                if exc.code == 'method_not_allowed'
                else error_status(exc.code)
            )
            return json_response({'error': exc.as_dict()}, status)
