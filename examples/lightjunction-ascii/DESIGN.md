# 椅子之后: built visual system

The world is a charcoal stage (`#080808`) with gray-white ASCII sculpture (`#d0d0d0`) and quiet native controls. The 96-column, 54-row projection uses a system monospace face because character cells are the medium itself. Every scene glyph is 7-bit ASCII; the Chinese caption sits outside that grid.

The opening object uses six-surface box samples for the seat, back posts, top rail, three back slats, four legs and cross rails. Projection uses perspective, a nearest-surface depth buffer and surface-normal lighting. The camera pans slowly through a three-quarter view; opening zoom makes the chair the primary object. Nuclear volume uses a growing fireball, an ellipsoidal cap with nine rim lobes, overlapping stem volumes and a low dust base. The expanding ground ring and fragments remain in the same 3D coordinate space.

Composition gives the work the viewport. The footer holds the small title, one sentence and pause/replay/profile controls. Desktop centers a projection capped at 15px character height; at 390px, it shrinks to fit the full 96-column grid without horizontal scrolling. The footer stacks on narrow screens.

Motion is one 18-second authored sequence with 64 discrete visibility frames. The chair remains alone for 3.6 seconds. A detonation develops into the cap, stem, dust and fragments, then the sequence loops. No whole-screen flashing, transforms, filters or large compositor strip are used. System reduced motion displays the opening chair; an explicit checkbox restores playback. A native pause checkbox holds the current frame and keyboard Space toggles it.

Body and controls remain legible against the charcoal surface; the ASCII field uses tonal density as shading. Controls have 44px targets and explicit focus outlines. No shadow, gradient, SVG icon, external font or image asset is needed. Selection reverses the foreground and background colors.

Only `index.html` ships to MSG hosting. Generation and browser acceptance scripts are reproducible supporting source. Browser screenshots are evidence outside the shipped asset set; there are no shipping rasters.
