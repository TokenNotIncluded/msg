---
version: 1
slug: "shared-home-art"
primary_target: "src/msg/core/public_board_art.py"
related_targets: ["src/msg/transports/public_board.py"]
---

# Shared Home ASCII signal

## Scope and mode

Mode: Read, with one small authored identity moment. Primary source:
`src/msg/core/public_board_art.py`; presentation and motion controls belong to
`src/msg/transports/public_board.py`. This is the untouched shared Home board,
not the separate token-cloud hero or Root flight scene.

## Motion direction

The composition changes visibly over twelve seconds: a larger complete MSG
word, separated character streams, the ordered gathering into a readable word,
then a compact MSG hub above a two-route message network. One punctuation packet
travels along the straight route; another takes the lower branch. The scene
unfolds back into the large word and holds before repeating. No phase is a
blocking loader, and the animation-stripped default remains the complete word.

Three letters occupy the full width at the readable stops. During the relay
phase, the compact word sits above bracketed ASCII message nodes and connectors;
this is a new composition, rather than a color pulse on the old word. Gray-blue
ink and a brighter packet keep the incumbent identity. All visual marks are
ASCII text with explicit positions, including the route lines and nodes. The
subtitle remains `[ svg + text ]` with the short existing invitation below.

Only text glyphs, groups and bounded SMIL opacity/color/translation/scaling are
used. No script, filter, image, font request or dependency is introduced. Keep
the same 960 × 300 viewBox and existing limits: 16 KiB, 256 elements, 32 animations.
The existing renderer supplies still art for reduced motion, explicit Pause,
offscreen and hidden state; user pause continues to take precedence on return.

## Content boundary

This source provides generation 0 only. An existing shared board revision,
user SVG, avatar or text is not replaced. Saving the public board remains a
separate signed action; improving the release default does not perform it.

## Verification

Inspect the actual generated SVG at desktop and mobile display sizes, including
its animation-stripped still projection, character-stream and relay phases, and both sides of the loop
seam. Validate the deployed SVG sanitizer and bounded element/animation counts.
Check coherent spline segments and visible transform/color/opacity endpoints against their static
state. The coordinator owns integration, native release and public verification;
local visual checks do not establish a production or physical-phone result.

## ascii.rest integration — 2026-10-08

Home pairs the actual shared text with an interactive character stage. It reuses
bas3line/ascii's MIT Canvas renderer and three fixed upstream pieces: Galaxy,
Flow and Aurora. The shared SVG remains selectable, isolated in an image, and
editable. A custom SVG is the initial scene; changing SVG selects it immediately.
No-JS retains the shared image and text. The local runtime is also reused by
Root's optional Character studies panel; it never claims to show server state.

The Home stage uses midnight paper and pale blue/green ink, with a tab-like
scene selector, while the reading page retains the user's theme. At 760px the
text and real navigation precede the full-width stage. Browser Pause controls,
reduced motion, visibility and offscreen gating bound animation to 16 fps.
Canvas frames are decorative and excluded from accessible/DOM text. Markdown,
raw, discovery and WebMCP contracts receive no new art payloads. Runtime hashes
are pinned under the existing CSP; there is no CDN or new network authority.

Local browser acceptance: `scripts/check_ascii_browser.py`, disposable real MSG
fixture, desktop 1440px and mobile 390px. Evidence in `output/ascii-upgrade/`.
This describes local work, not a production release or physical-phone test.
