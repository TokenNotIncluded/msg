---
name: 椅子之后
description: Enter a seated person's eyes inside a real-time 3D ASCII nuclear scene.
colors:
  stage: "#080808"
  foreground: "#d6d6d6"
  status: "#c8c8c8"
  muted: "#aaa"
  control-background: "#101010"
  control-border: "#454545"
  control-hover: "#242424"
  control-disabled: "#888"
  disabled-border: "#303030"
  focus: "#e2e2e2"
  held: "#393939"
typography:
  display:
    fontFamily: 'ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace'
    fontSize: "12px"
    lineHeight: 1
    letterSpacing: "0"
  label:
    fontFamily: 'ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace'
    fontSize: "12px"
  body:
    fontFamily: 'ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace'
    fontSize: "12px"
    lineHeight: 1.6
  hint:
    fontFamily: 'ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace'
    fontSize: "11px"
    lineHeight: 1.5
rounded:
  control: "2px"
components:
  control-button:
    backgroundColor: "{colors.control-background}"
    textColor: "{colors.foreground}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "8px 11px"
  control-button-hover:
    backgroundColor: "{colors.control-hover}"
    textColor: "{colors.foreground}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "8px 11px"
  control-button-disabled:
    backgroundColor: "{colors.control-background}"
    textColor: "{colors.control-disabled}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "8px 11px"
  movement-button-held:
    backgroundColor: "{colors.held}"
    textColor: "{colors.foreground}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "8px"
---

# Design System: 椅子之后

## Overview

**Creative North Star: "椅子之后"**

This is the built visual system for the personal site at `/@lightjunction/web/`, an Experience surface. A chair and a slumped person become the visitor's body inside a real-time 3D ASCII nuclear scene, with a walkable underground shelter behind the chair. The user fixed the subject, rendering language, first-person choices and closed-door route to survival; the work owns the viewport and the interface stays quiet. [SURFACE.md](SURFACE.md) preserves that direction.

A charcoal stage, gray-white glyphs and a sparse perspective floor give the world its space. The opening side three-quarter view separates the brighter slumped person from the quieter chair, then moves into the seated person's eyes. Chinese status text and native text buttons sit below the world. This personal artwork has its own visual identity, separate from the bright `/@root/web/` introduction.

**Key Characteristics:**

- Runtime three-dimensional surface sampling, perspective, depth and character-density shading.
- A single adaptive ASCII world: 112 by 60 cells on desktop, 76 by 56 below 600px.
- Third person enters first person over six seconds; a distant nuclear burst appears at eight seconds and choices open at 8.5 seconds.
- Movement, facing, stance, coffee and a walkable shelter change the outcome; survival requires being inside with the door fully closed.
- Survivors can leave into cratered ground and ruins at scene time 28 seconds; only death automatically begins another round after six seconds.
- Native buttons, keyboard, relative mouse look and independent multitouch input with explicit reduced-motion start and lifecycle pauses.
- One self-contained HTML file with one hash-pinned inline runtime and no external assets or requests.

The frontmatter records literals extracted from `template.html` and `scene.js`; it does not introduce a new runtime token system. `build.py` combines `world_geometry.js` and `scene.js` into one inline runtime and reports the final HTML digest and script pin for each build. Publication must bind the deployed CSP to that build rather than reuse an older recorded hash.

## Colors

The neutral palette lets character density carry the shading. There is no accent-color hierarchy.

- **Stage:** page background and the negative space between world glyphs.
- **Foreground:** inherited world, heading and control text; also the selection background, with selected text using the stage color.
- **Status:** the current narrative and action feedback beneath the stage.
- **Muted:** the keyboard hint, plain author label and hovered control border.
- **Control background and border:** a quiet rectangular target for actions.
- **Control hover:** a brighter background when an enabled button is hovered.
- **Control disabled and disabled border:** unavailable actions remain visible and subdued.
- **Focus:** the keyboard focus outline.
- **Held:** the background of a pressed touch movement button.

## Typography

The system monospace stack is shared by the world and footer; no font is downloaded. Every world glyph is 7-bit ASCII, while the title, live status and accessible descriptions are Chinese.

- **Display:** one `pre` preserves whitespace with unit line height, zero letter spacing and no margin. The base size is recorded in the frontmatter; runtime sizing fits the current grid inside the available stage, with a minimum of `4px`. The world cannot be selected and pointer dragging controls the view.
- **Label:** the heading and action buttons use the label size. The heading has weight `400`, with no hero-scale title. The noninteractive author label uses `11px` text.
- **Body:** the status uses the body size and line height, with a `20px` minimum height to reserve feedback space.
- **Hint:** the keyboard instructions use the smaller hint size and line height.

At widths up to `600px`, buttons use `11px` text. The world changes its grid below `600px` and resizes glyphs rather than wrapping geometry.

## Layout

The body fills `100svh`, has a `560px` minimum height and uses grid rows `1fr auto` without horizontal overflow. The stage and footer have `min-width:0` so grid content can shrink. The stage centers one character projection with flex alignment and `14px 8px` padding. Runtime sizing takes the smaller of the available height per row and width per character cell, estimated at `0.61` times the font size.

The footer is a grid with `8px` gaps and `0 24px 16px` padding. Its first bar places the title and plain author label apart; a live status row and wrapping action buttons follow. The final bar holds the keyboard hint and touch movement controls. Bars use `12px` gaps; desktop action buttons use `8px` gaps.

At the `600px` CSS breakpoint, stage padding becomes `8px 4px`, footer padding becomes `0 12px 12px`, footer gaps become `6px` and action gaps become `5px`. The final bar stacks, the author label is hidden, the hint is capped at `40ch` and touch movement buttons appear. Runtime uses the smaller 76-by-56 grid below that width; desktop uses 112 by 60.

## Elevation & Depth

Interface surfaces have no box shadows, gradients or filters. Depth belongs to the ASCII world. The runtime samples box faces, ellipsoids, cylindrical limbs, straight cup walls, a circular rim and a toroidal handle, projects them through a perspective camera and keeps the nearest sample in a depth buffer. Surface-normal lighting selects glyphs from ` .,:;itfx*#%@`.

The chair, slumped body, table and mug occupy the same space as the dotted ground. The camera begins in third person, moves into first person and changes eye height when standing. Moving adds a small walking bob; eye height follows the stair floor and crater terrain. First-person hands and the coffee sip are sampled geometry projected in camera space. The cup has a dark inset coffee surface, an open rim, straight walls and a side handle; its lift stays inside the character field. The distant nuclear burst grows into a lobed cap, stacked stem volumes, dust base, ground shock ring and fragments; those forms are runtime geometry. On death, the camera lowers toward the current ground.

The shelter has an open ground collar, upright entrance frame, twelve stair treads and retaining walls. Stairs run from `z=-2` to `z=-5.05` and meet a `y=-2.6` floor. Room bounds are `x=±2.6`, `z=-5.05..-9.6`, with a `y=-0.25` ceiling. Coarse single-face samples describe the shell; while inside, per-cell analytical ray intersections fill wall, floor, ceiling and closed-door depth continuously. Ground and ceiling occlusion keep outside geometry from showing through the underground shell; the stair opening remains visible.

The door rotates its points and normals around the left hinge at `x=-0.85`, `z=-5.05`. Its closed face spans `x=±0.85` and `y=-2.6..-0.35`. After the blast, real terrain height creates craters and rims. Broken chair pieces, three jagged wall remnants, three leaning beams and scattered rubble occupy near, middle and far world positions, leaving the stair entrance clear. Their cached coarse meshes retain depth and orientation as the player turns. The player walks out using the same camera rather than being moved to a new composition.

## Shapes

Boxes, ellipsoids and sampled limbs form the scene through character cells. The perspective floor remains sparse. Controls are small rectangular native buttons with the frontmatter corner radius and a `1px` border; they do not become cards, icons or overlays. The author label is plain text without a control border or background.

## Components

### ASCII world and time

One `pre` has an image role and a Chinese accessible description. `requestAnimationFrame` advances scene time; rendering is limited to roughly one update per `65ms` on desktop or `85ms` below 600px. Delta time is capped at `0.08s`. A frame is generated from current position, view, stance, coffee and explosion state.

The first six seconds move from third to first person. The nuclear burst begins at eight seconds; movement and choice actions become available at 8.5 seconds. The shockwave expands from the distant explosion until it reaches the player's current position. Survival requires actual room occupancy, the closed-door target and a door angle at or below `0.04` radians; a closed door while outside or an open door while inside is insufficient. Keep the door shut until survivors enter aftermath at scene time 28 seconds, then permit walking out into craters and ruins. Survivors retain their camera and choose R to restart. Death alone stops movement and choice actions and resets after six seconds.

### Action buttons and status

Buttons have a `44px` minimum target height, the frontmatter padding and subdued borders. Hover changes background and border only when the button is enabled. Keyboard focus uses a `2px` outline offset by `3px`. Status is a polite live region for phase changes and action feedback.

WASD and arrow keys move relative to the current view and automatically stand the player up. Shift increases speed from `2.5` to `3.3` world units per second. E prioritizes the nearby shelter door; elsewhere it toggles stance. Sitting again requires being within `1.4` world units of the chair and returns the player to its seat. B and F ease the view toward the direction away from or toward the explosion. Direct mouse look or dragging cancels that eased turn.

The door button is enabled within `1.5` world units of its location while the player is below `y=-0.9` floor height. Its label distinguishes opening and closing. The door eases toward zero or a right angle; crossing its threshold requires the open target, at least `1.3` radians of opening and a centered passage within `x=±0.66`. Walkable bounds keep the player inside the room walls and stair corridor; no action teleports them into the shelter.

Coffee is available within `1.8` world units of the table's mug position, at least three seconds after the previous sip. The hand-and-cup action lasts `2.5s`. Availability is visible through native disabled button states. Dead players cannot move, turn, change stance, operate the door or drink.

### First-person look

Clicking the world with a mouse requests pointer lock. While captured, relative movement turns the view with rightward movement increasing yaw and vertical movement controlling pitch. Pitch is clamped to `±1.28` radians. If capture fails, the existing pointer drag remains available; touch uses that drag path. Esc releases capture, while P, visible-page blur, hidden state, restart and death also release it and invalidate pending requests. Each request carries a generation; a late grant from a cancelled generation exits even after play has resumed. Pointer-lock changes clear held movement and drag input.

### Pause, start and lifecycle

P or the pause button freezes scene time, clears held input and releases mouse capture; resume resets the timing baseline. R or restart resets time, position, view, stance, coffee, door, room, survival, aftermath and input. Restart uses the current reduced-motion preference.

Reduced motion begins with a still chair and person at time zero and a “开始互动” button. Explicit start opens the scene; restart and the next round wait again while that preference remains active. Switching to reduced motion during playback pauses it.

Hidden documents do not advance time and clear keyboard and pointer input. A visible page losing focus explicitly pauses a live scene and requires resume. Keep those behaviors separate: returning from a hidden tab must not create a catch-up jump or retain a movement key.

### Touch movement and author label

At the mobile CSS breakpoint, A/W/S/D and run buttons hold their movement key while pressed. Keyboard keys, held pointers and short button activation pulses are stored independently; multiple fingers can move, run and drag look at once. Releasing one pointer preserves other held keys and pointers. Cancellation and lost capture remove only that pointer and clear its held appearance when no other pointer still holds the same action. Each target is at least `44px` wide and high. The world uses `touch-action:none` for drag look, and dragging cancels B/F turn targets.

The author label is an unclickable `span` reading `@lightjunction`, hidden in the compact footer. The surface contains no links, navigation or forms. Neither scene controls nor the runtime perform account actions. Only `index.html` ships; source files, acceptance code and local screenshots support the build.

The opaque sandbox uses `allow-scripts allow-pointer-lock` and permits only the exact hash-pinned inline script. Its canonical CSP explicitly blocks script attributes, connections, frames, objects and workers; same-origin access remains absent. Keep the plain author label: a self-navigation link would leave the contained experience even without external asset requests.

## Do's and Don'ts

### Do:

- **Do** preserve the user-fixed chair, slumped person, first-person choices and walkable closed-door shelter route to survival.
- **Do** keep aftermath exploration under the survivor's control and reserve automatic restart for death.
- **Do** generate the 7-bit ASCII world from current three-dimensional state with perspective, depth and lighting.
- **Do** require an explicit start under reduced motion, including after reset and the next round.
- **Do** preserve native disabled states, visible focus, independent multitouch input, capture release and explicit pause on visible-page blur.
- **Do** publish only the final `index.html` and pin its sole inline script by the exact build hash in an opaque sandbox.

### Don't:

- **Don't** import the separate `/@root/web/` brand into this personal artwork.
- **Don't** substitute prerecorded playback for the interactive world or add WebGL, external fonts, image assets, audio or external requests.
- **Don't** add navigation or forms, or grant same-origin access, wildcard scripts or unpinned inline-script execution for this scene.
- **Don't** replace walking into the shelter with teleportation, grant protection to a player outside its closed door, or let movement, door, stance, facing or coffee act after death.
- **Don't** retain held input or mouse capture across pause, blur, hidden state, restart or death.
- **Don't** treat documented test coverage or local evidence as proof of final review or public signed publication.
