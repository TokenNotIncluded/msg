# WebSub publication

MSG can push public feed changes through several WebSub hubs. The feed remains at `/rss.xml` (`/rss` and `/-/rss` are compatible read aliases). Its HTTP `Link` header and Atom links advertise the canonical feed and every configured hub.

Configure `/etc/msgd/msgd.toml`:

```toml
[websub]
hubs = ["https://pubsubhubbub.appspot.com/", "https://your-hub.example/hub"]
```

Use HTTPS hubs on port 443. Duplicate URLs are removed; up to 16 are supported. Restart both `msgd.service` and `msgd-worker.service` after changing this configuration. An empty list disables publication.

Each committed change to the anonymous public feed queues one durable job per hub. The effects worker sends `hub.mode=publish` and `hub.url=<service>/rss.xml` as form data. A timeout, 408, 429 or server error retries independently with bounded exponential backoff (eight attempts). An expired worker lease retries the idempotent publication hint. Removed hubs are rejected before delivery. DNS targets are checked against private networks and redirects are disabled.

Subscribers discover a hub from the feed, subscribe using their own callback, handle the hub's verification challenge and renew their lease. A hub's acceptance of a publication request is not proof that a particular subscriber received it.

Only the default anonymous feed is advertised for push. Signed, filtered and paginated RSS reads retain their normal authorization and do not advertise public hubs. Notifications contain only the public feed URL. Public feed visibility changes, including removal of a post, notify hubs; edits that leave the public feed unchanged do not. The feed currently exposes the latest 30 visible posts as references, titles and revision digests.

Protocol: [W3C WebSub](https://www.w3.org/TR/websub/).
