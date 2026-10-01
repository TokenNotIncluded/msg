# Post universe

`/@root/web/` is an interactive view of the existing MSG resource graph, not a
separate social network or a simulated chat. Every publicly readable account is
a star with an ID-derived, stable position. Selected accounts reveal their posts
as satellites. Reply relations form arcs; unrelated users are not connected just
because their positions are close. Decorative dust is not a user and cannot be
selected. The counter describes the loaded sector, not global activity.

## Explore and participate

Drag to orbit, scroll or pinch to approach, Shift/right-drag to pan; WASD pans,
arrows rotate, Q/E changes distance, and H returns home. The catalog and search
provide native-button alternatives, including on phones. “Find a signal” chooses
a real loaded user. The small expedition tracks three visited stars, two opened
posts and one followed reply in memory for this visit; reading does not submit
ACKs, follows, likes, or other writes. Pause and reduced motion stop ambient motion.

The native WebGL renderer has no third-party script or CDN dependency. Where
WebGL is unavailable a Canvas renderer projects the same 3D geometry and camera.
Loss of a running WebGL context exposes the catalog until restoration. No graphics
support still leaves the catalog usable. Pixel ratio is capped at 1.75. Hidden
pages stop rendering.

## Public read boundary

`GET /_universe?kind=users|posts` uses the existing anonymous read executor even
when a browser session exists. Users are paged at 100, posts at 24. Selecting a star loads its own public
posts with an optional author filter and independently bound pagination. A continuation
is accepted only for exactly that projection; the existing cursor machinery also
checks principal, snapshot and expiration. Post previews are bounded. Authors and
reply targets must pass a separate current anonymous read before their identifiers
or links enter the projection. The next sector is loaded explicitly, and current
page content is refreshed after successful writes. Search is over loaded content.
No cached public graph or search index is persisted by the browser.

`GET /_universe/me` takes no subject or other query parameters. It obtains the
current browser account from the existing read-only session adapter, executes
`communication.dm_list`, and requires the result subject to equal that account.
Conversation bodies come from their already-authorized canonical read paths.
Messages in the opened conversation become private satellites around its star;
selecting one rechecks its current read permission.
Private data is never merged into the public graph, statistics or search. These
responses are `private, no-store` and vary on Cookie; no wildcard CORS is granted.
The UI revalidates private access every 30 seconds and clears private data, labels,
drafts and signing state on hiding/leaving the page or leaving the private view.
A revoked session also fails subsequent body reads on the server.

## Explicit signed writes

Posting, replying, requesting/accepting/declining a conversation, and private
messages use only their existing `/-/p/<operation>` contracts. A browser session
never authorizes a write. A user can explicitly connect a non-root CLI identity
key: a 32-byte seed is converted locally to a non-extractable WebCrypto Ed25519
key, matched to the server's active public identity by a challenge, and the seed
buffers are overwritten. Neither the key nor credentials enter storage, URLs,
logs or network requests. The signing reference expires after ten minutes or
when the page is hidden. JavaScript memory clearing is not a secure-erasure
promise about browser/OS copies of the original file.

The packet uses the existing v1 payload digest, six-digit UTC timestamp,
domain-separated request signature and certificate list. Writes omit cookies.
Only the six explicit content/communication operations are available. An uncertain
delivery retains its request ID; an explicit unchanged retry is idempotent. There
are no automatic retries or optimistic “sent” messages. Server rejection stays
visible. The account matching a private view must also match the connected key.

## Trusted release page, untrusted hosted content

The old release-owned introduction was an offline game. Live data requires a
narrow new trust exception: only `w_root_web/index.html` whose complete blob
digest equals the assembled release page gets `sandbox allow-scripts
allow-same-origin` and `connect-src 'self'`. Executable script bytes are separately
SHA-256 pinned. This applies to the release file, not arbitrary root-uploaded or
user-hosted HTML. A different path, website, or modified byte receives the original
opaque sandbox with no scripts/networking. External assets, forms, framing and
base URLs remain forbidden. User text is inserted with `textContent`, never HTML.

Tests cover anonymous projections, hidden ancestors, hidden reply targets, cursor
binding/pagination, authenticated private views, wrong-subject rejection, exact
CSP pins and cross-language browser signatures against the real executor. Browser
fixtures are explicitly synthetic and exist only in the verification script.
