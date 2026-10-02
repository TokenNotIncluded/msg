# Surface: /@lightjunction/web/

Mode: Experience. The visitor watches the work directly: a recognisable 3D chair becomes an ASCII nuclear-explosion sculpture. The user fixed this subject and rendering language; it is independent of the bright `/@root/web/` brand.

The first view gives the chair most of the stage height, with a perspective floor beneath it. There is no introduction panel, card or promotional claim. A small caption and native pause/replay controls remain below the projection. The memorable transition is the chair's surface breaking into real spatial fragments while a mushroom cap rises above its stem and a ground shock ring expands.

Constraints: one script-free self-contained HTML file; opaque-origin CSP unchanged; only ASCII characters make scene geometry; responsive desktop and 390px mobile; reduced-motion static by default with explicit opt-in; no external requests or account actions. Duration 18 seconds, 64 discrete frames, first detonation at 3.6 seconds. Code-led build, not a WebGL implementation or raster illustration.

Motion constraint: all frames inherit one integer CSS clock on the projection; opacity selects exactly one frame. Keep the opening chair still during streamed parsing and start frame 0 only after the trailing `#scene-ready` marker arrives. Pause must freeze that parent clock and visible frame in normal playback and after reduced-motion opt-in; frame elements must not run independent animations.

No unresolved visual direction. Final design review and public signed publication are separate from the local browser acceptance.
