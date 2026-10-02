# Root web: planets, token ribbons and local flight

This refinement extends the existing root web universe. It does not create a
second game directory, identities, public presence or an additional write API.
The patch was prepared against the verified relevant files at upstream commit
`c6be2080307a1a067c2117b9791e3a7a995a08de` in TokenNotIncluded/msg.

## Appearance

Bodies use identity-seeded, closed faceted terrain meshes and directional shading.
Root remains the white luminous anchor with its layered corona and rays. Valid
certificate size, tint, hexagonal seal and diamond, expiring activity brightness,
and the separate reserve arc still come from the unchanged appearance model.
These channels do not invent authority, presence, balances or relationships.

Three monochrome, diffuse token ribbons occupy world space. Passing through them
creates depth and parallax instead of moving a screen-space background. Scenery
uses a per-visit random seed, independent of stable identity positions, and never
becomes selectable accounts. Scenery is bounded to 2,300 tokens and 72 soft sprites.
The existing spatial index remains; detailed bodies are limited to 64 in WebGL or
24 on Canvas, plus root, selected and hovered nodes. Planet meshes are cached per
node. The ship trail holds at most 36 samples and expires after 1.4 seconds.

## Controls

Choose Pilot or focus the canvas and press F. The silver ship occupies a real
world-space position, with a third-person follow camera, acceleration and inertia.

| Input | Flight action |
| --- | --- |
| W / S | Forward / reverse thrust |
| A / D | Strafe left / right |
| Q / E | Descend / ascend |
| Arrows or canvas drag | Turn and pitch |
| Shift + W | Boost |
| Space | Brake; overrides thrust |
| Enter or nearby Inspect | Select a currently loaded nearby identity |
| H | Leave flight and return to the overview |
| F / Escape / Exit flight | Leave flight |

Touch users can hold the steering and thrust pads simultaneously. Releasing one
finger does not release another control. Lost captures, cancelled gestures, focus
changes, window blur, page hiding and context loss clear held input and inertia.
Keyboard controls do not intercept typing in search or the composer. Catalog,
search and post controls remain available; flight never grants write authority.
The ordinary orbit/pan/zoom interaction remains available outside flight.

The flight integrator normalizes diagonal acceleration, caps long frame deltas,
uses exponential drag, checks swept contacts against loaded planet bodies, and
bounds navigation to the loaded scene's surroundings. Speed is in scene units,
not a physical distance or account statistic. H provides an immediate escape
from empty scenery. This is local exploration, not networked multiplayer flight.

Reduced motion removes boost, trails and banking, keeps direct navigation and
stops ambient motion. It also retains working touch controls from a resting frame.
Pause exits flight. Flight does not automatically select, fetch account details,
post, ACK, follow or spend money. Explicit nearby inspection re-resolves its target
from the current graph before delegating to the existing selection callback.
Graph replacement immediately clears the nearby label, including private labels.

## Release integration and evidence

Changes stay in the existing root-web renderer and assembled HTML. No extra
runtime dependency, CDN, font binary, script URL, permission exception or backend
route is introduced. The existing release assembly includes these script/style
bytes and computes the trusted-release pins; do not loosen the hosting sandbox.
A customized hosted page is not automatically made a trusted release by this patch.

Run from the complete repository after applying the patch:

```sh
node --test tests/js/nebula.test.cjs tests/js/flight.test.cjs
python scripts/check_flight_browser.py
```

The browser verifier requires Playwright and Chromium. `MSG_BROWSER_PATH` selects
an installed browser. It uses the actual model, renderer, HTML and CSS with an
explicitly synthetic callback host, no server and no external requests. Synthetic
accounts are labelled in every screenshot. It is not the production app's complete
session/signing/private-data integration. Font data is omitted from the fixture;
production continues to use the existing bundled fonts.

Local results: 21 new flight/scenery tests and 24 unchanged nebula/model regression
tests passed (45 total). Six browser cases passed: desktop, phone portrait, phone
landscape, reduced motion on desktop and phone, and forced Canvas fallback. All
six actually used Canvas 3D in this environment. Lifecycle handler tests dispatch
synthetic events; they are not evidence of OS backgrounding or GPU restoration.

Still required before deployment: native WebGL shader/render/context-restoration
acceptance in the existing production-CSP browser workflow, complete root-web app
integration, PostgreSQL/backend and release-upgrade regressions, and pinned Ruff
0.16.9 formatting/lint checks (that executable was unavailable locally). This
patch has not been pushed, merged, released or deployed. No production-frame-rate
or multiplayer claim is made.
