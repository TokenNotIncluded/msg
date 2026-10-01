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

## Token field refinement (local changes, 2026-10-01)

The stage is neutral black, not blue; the scenery is a folded, deterministic
field of small monochrome token glyphs, not a simulated galaxy or extra accounts.
A new layout version places the real `u_root` at the origin and keeps an inner
clearance around it. Coordinates are visual, not a protocol or stored identity
attribute. Colors belong to identities, not to the background.

Independent visual channels prevent money, presence and authority being confused:

- Root: white geometric core, layered corona and radial rays; always prominent,
  never an assertion that the administrator is currently online. Identification
  uses the reserved subject ID, never a display name or user-controlled role.
- Certificate: a 1.37× body, quiet colored crystal, hexagonal seal and diamond
  badge. A current publicly readable certificate must pass the existing complete
  chain validator, including expiry, issuer/key revocation and scope validity.
  This is a scoped authority marker, not a reputation rating. All unverified,
  hidden or expired observations fail closed, without a badge.
- Activity: brightness follows valid, explicitly self-reported presence. On TTL
  expiry it immediately stops claiming presence; a recent **public post** can
  provide a weaker, smoothly decaying glow. Missing presence means unknown, not
  offline. No login history, private posts or messages affect public brightness.
- Reserve: a thin segmented lower arc uses `log10(1 + units)`, capped in size.
  Exact amounts use integer minor units encoded as decimal strings (including
  int64 values above JavaScript's safe integer range), and the configured
  currency code and scale. Money never enlarges the body. Undisclosed, unavailable
  and zero values remain distinct in the inspector.

`discovery.get` and the existing flat user read query accept an explicit `star`
field; default responses are unchanged. It is an anonymous-only projection so a
privileged reader cannot accidentally populate a shared graph with private facts.
Each subject summary scans at most 32 candidate certificates and 64 recent post
candidates, checks current access and the execution deadline, and whitelists
presence fields. No presence message, capability hint or hidden certificate path
is included. A bounded scan without an observation is not proof of inactivity.

`/_universe?kind=users` requests that field; the initial page also returns a
separately authorized root `anchor`, even if root sorts beyond the loaded page.
`/_universe?ids=u_...,...` refreshes up to 100 unique user IDs, with at most four
concurrent executor reads. It cannot be combined with a cursor or author filter.
Every response is still read-only, no-store, and uses anonymous authority. Page
cursors issued for the earlier, smaller projection must be restarted on upgrade.

Visible public scenes refresh loaded identities every 30 seconds. An observation
older than 90 seconds stops supporting a certificate, presence or balance display;
expired certificates and presence stop immediately, even in paused scenes. The
server's observation time prevents a skewed device clock from inventing status.
A user that becomes unreadable disappears on the next successful refresh. This
is a bounded-freshness visualization, not a real-time revocation guarantee.

The reserve publication policy is shared with `money.public_balance`: root and
active banks have mandatory public balances, others require an explicit opt-in.
`/_universe/me` reads the current session owner's `money.balance`, requires both
envelope and data subject to match, and returns a **self-only** reserve. A read
failure is unavailable, never a zero. This private value lives only in My orbit
and is removed when switching or hiding the page. It cannot influence the public
scene, search, counts, arc geometry or any other account's display.

Intro copy and the mission widget fade after real pointer movement, clicking,
scrolling or keyboard interaction. They become inert/hidden to accessibility APIs
and do not reappear during the visit. `?` and the help button reopen the field
legend and current expedition progress. Reduced motion removes the fade and
ambient movement. Selected mobile stars are framed above the inspector; software
Canvas projects the same certificate seals, balance arcs and root geometry.

### Local validation

```
node --test tests/js/nebula.test.cjs
python scripts/check_nebula_browser.py
uv run pytest tests/test_star_projection.py tests/test_universe.py \
  tests/test_presence_claim.py tests/test_money_public.py
```

The browser script uses isolated fixtures only. `MSG_BROWSER_PATH` selects an
installed Chromium. Restricted environments can explicitly select
`MSG_NEBULA_OFFLINE_DOM=1` and `MSG_NEBULA_EXPECT_RENDERER=canvas-3d`; this tests
actual scene/UI code and the inline script pin without navigation. It **does not**
validate the HTTP response sandbox, real sessions, the database, WebCrypto writes,
or WebGL. No browser policy is disabled. PostgreSQL, Python 3.15 and the pinned
project dependencies remain necessary for full backend acceptance.
