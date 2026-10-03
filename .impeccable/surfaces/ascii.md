---
version: 1
slug: "ascii"
primary_target: "src/msg/data/lightjunction-ascii.html"
related_targets: ["src/msg/transports/hosted_release.py", "route:/@lightjunction/web"]
---

# 椅子之后: /@lightjunction/web/

## Scope and mode

Mode: Experience. Primary target: `src/msg/data/lightjunction-ascii.html`;
related boundary: `src/msg/transports/hosted_release.py`. One self-contained,
real-time 3D ASCII scene renders perspective samples from surfaces at runtime,
with depth and occlusion; it is not a prerecorded character animation.

## Confirmed world — 2026-10-03

The first view centers a chair and slumped person on a black stage. The camera
moves from a three-quarter view into the person's eyes. Before shelter entry,
the outside world is gray-white monochrome. The visitor can stand, move, turn
away, face the blast or drink coffee. Descending the stairs and closing the
shelter door allows survival.

Inside, material-colored glyphs render the room and two real 3D posters for
`msg.lmm.best` and `api.lmm.best`. They sit on the shelter wall with perspective
and occlusion, rather than screen overlays. After surviving and stepping out,
color remains outside on grass, trees, ruins, crater relief and editable blocks.
Four build materials are grass, dirt, stone and wood. A visible target controls
placement/removal within reach; replay resets placed blocks and the story.
The dark mono/spatial reference does not copy assets or layouts from
`gadgets.muse.ai` or recolor ordinary MSG documents.

## Controls and physics

Desktop uses movement keys, mouse capture/scene drag for FPS look, explicit
story actions, pause and restart. Touch has a left two-dimensional walking stick
and a separate scene-look drag on the right; movement is relative to the current
view and stick magnitude controls speed. Independent pointer IDs allow walking,
looking and action taps together. Releasing one pointer leaves the others held;
cancel, lost capture, blur and page hiding clear control state. Portrait and
landscape layouts keep the scene, stick and build actions usable.

Terrain/build support, falling, jumping, head clearance and collision use the
local 3D world. Support is selected relative to the player's feet, so an
unsupported overhang does not pull a player up onto its top. The shelter stairs,
door and crater elevation remain part of the same movement model. Reduced
motion waits for an explicit start; Pause stops the scene clock. These source
behaviors do not establish OS background behavior or physical mobile acceptance.

## Hosting boundary and evidence

The reviewed source is commit `f2ec638e005aa4b5634ef4774d6770571ee5b522`,
HTML digest `085c0ffdda3c2e033185e8dd579d54fe5d54b6ca92212fdc3e82e4fff4adcda5`.
Execution requires the exact reviewed website ID, `index.html` path and blob
digest, with separately pinned inline script. The sandbox remains opaque;
network, tracking and account authority are not granted. A modified page fails
closed and does not inherit the Root release's same-origin exception.

This brief records the local source and existing iteration evidence. The ASCII
iteration awaits native45 publication; it is not a production or physical-phone
acceptance claim. No new test or browser run is needed for this documentation
handoff.
