# ascii.rest in MSG

Upstream: https://github.com/bas3line/ascii
Pinned commit: fcb644403bfc25af0002e415d1006dd10b99ca34
License: MIT (see LICENSE). The files in src/ are unmodified upstream sources.

MSG reuses the Canvas glyph atlas, changed-cell renderer, resize handling,
visibility gating and reduced-motion support, plus galaxy, flow-field and aurora.
Only these three pieces are bundled; there is no catalog fetch or CDN request.
msg-entry.ts adapts the renderer to the editable Home board and Root's character
studies. A Canvas keeps animation frames out of DOM text and accessibility trees.
The shared SVG is still isolated in an img, remains editable, and stays visible
without JS. Custom SVGs default to the community scene, and SVG updates select it.

Build with Bun 1.4.0 (no install or lifecycle scripts):

    bun scripts/build_ascii.mjs

The generated bundle is checked in and packaged with MSG. Both surfaces use
byte-pinned inline scripts under their existing CSP. No external script, asset,
network origin or credential permission is added. Animation is capped at 16 fps;
Pause, reduced motion, offscreen and hidden tabs stop it. It is decorative, never
an observation of server activity, a mailbox state or a gameplay result.
