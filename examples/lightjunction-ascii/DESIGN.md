---
name: 椅子之后
description: A three-dimensional ASCII chair becomes a nuclear-explosion sculpture.
colors:
  stage: "#080808"
  body: "#d4d4d4"
  sculpture: "#d0d0d0"
  title-focus: "#dedede"
  controls: "#b5b5b5"
  caption: "#aaa"
  link-hover: "#fff"
typography:
  display:
    fontFamily: 'ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace'
    fontSize: "clamp(5px,min(1.55vw,1.58vh),15px)"
    lineHeight: 1
    letterSpacing: "0"
  label:
    fontFamily: 'ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace'
    fontSize: "12px"
    lineHeight: 1.6
  body:
    fontFamily: 'ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace'
    fontSize: "11px"
    lineHeight: 1.6
components:
  control-link:
    textColor: "{colors.controls}"
    typography: "{typography.label}"
  control-link-hover:
    textColor: "{colors.link-hover}"
    typography: "{typography.label}"
---

# Design System: 椅子之后

## Overview

**Creative North Star: "椅子之后"**

This is the built visual system for the personal site at `/@lightjunction/web/`, an Experience surface. A recognisable chair becomes a spatial ASCII nuclear sculpture. The user fixed the subject and rendering language; the work owns the viewport and the interface stays quiet. [SURFACE.md](SURFACE.md) preserves that direction.

A charcoal stage, gray-white glyphs and a sparse perspective floor give the sculpture its space. The opening chair uses a three-quarter view and enlarged projection. The small Chinese title and caption live outside the ASCII field. This personal artwork has its own visual identity, separate from the bright `/@root/web/` introduction.

**Key Characteristics:**

- Real three-dimensional surface sampling and character-density shading.
- One large 96-column, 54-row projection with a small footer.
- An 18-second sequence with 64 discrete frames and a 3.6-second chair opening.
- Native controls and an explicit reduced-motion playback choice.
- One self-contained, script-free HTML file without external assets.

The frontmatter records literals extracted from `index.html` and `generate.py`; it does not introduce CSS custom properties or a new runtime token system. The recorded HTML SHA256 is `03265926c573b991e4e20f7a9f344122930d2c9b36587fe4e9ec22e52f2c2dbd`.

## Colors

The neutral palette lets character density carry the shading. There is no accent-color hierarchy.

- **Stage:** the page and sculpture background.
- **Body:** inherited foreground, native checkbox accent and selection background; selected text uses the stage color.
- **Sculpture:** the entire ASCII projection foreground.
- **Title and focus:** the heading and keyboard focus outline.
- **Controls:** the footer's inherited foreground for labels and links.
- **Caption:** the quieter sentence beneath the title.
- **Link hover:** the brighter foreground when a link is hovered.

## Typography

The system monospace stack is shared by the projection and footer; no font is downloaded. Each scene glyph is 7-bit ASCII, while the title, description and accessible scene label are Chinese.

- **Display:** the character grid uses the frontmatter display size, unit line height and zero letter spacing. Frames are absolutely positioned `pre` elements with preserved whitespace and no margins. Scene text cannot be selected.
- **Label:** the title, controls and footer share the label size and line height. The title inherits the footer font rather than behaving as a large hero heading.
- **Body:** the caption uses the smaller body size, with a `2px` top margin and no other paragraph margin.

At widths up to `600px`, the projection font size becomes `min(1.67vw,1.58vh)`. The grid keeps its columns rather than wrapping its geometry.

## Layout

The body is a full-height grid with rows `1fr auto`, a `100svh` minimum height and no horizontal overflow. The stage centers its projection with flex alignment. The projection is `96ch` wide and `54em` tall, with no flex shrinking; its character size scales with viewport width and height and caps at `15px` on desktop.

Desktop stage padding is `24px 12px 0`. The footer uses a horizontal flex layout, `20px` gap and `0 28px 22px` margin, with the caption at the left and controls at the right. Controls have `16px` gaps.

At the `600px` breakpoint, stage padding becomes `12px 4px 0`; the footer stacks with start alignment, `12px` gap and `0 18px 16px` margin. Control gaps become `24px`, and the caption has a `32ch` maximum width. The existing 390px mobile layout fits the complete grid without horizontal scrolling.

## Elevation & Depth

Interface surfaces have no box shadows, gradients or filters. Depth belongs to the ASCII sculpture. The generator samples all six surfaces of each chair box, projects them through a perspective camera and keeps the nearest sample in a depth buffer. Surface-normal lighting selects glyphs from ` .,:;i!tfx*#%@`. The camera slowly pans through the three-quarter view, then zooms out as the explosion grows.

The chair includes a seat, four legs, back posts, a top rail, three back slats and cross rails. A growing fireball becomes an ellipsoidal mushroom cap with nine rim lobes, overlapping stem volumes and a low dust base. The ground shock ring, chair fragments and seeded debris remain in the same three-dimensional coordinate space. These are generated samples, not a flat dissolve or raster image.

## Shapes

The scene's forms are boxes, ellipsoids, the ground ring and fragments expressed through character cells. The sparse dotted floor continues beneath them in perspective. The interface adds no cards, panels, custom rounded buttons or icons. Checkboxes retain their browser-native shape; links remain text.

## Components

### ASCII stage

The projection has an image role and a Chinese accessible description; individual frame text is hidden from assistive technology. All 64 animated frames occupy the same space and switch visibility through `steps(1,end)` animations. A separate opening-chair frame supplies the reduced-motion still. The sequence loops at 18 seconds; the first detonation is authored at 3.6 seconds. There are no whole-screen flashes, transformed frame strips or scripted playback.

### Native pause and motion controls

Each label is an inline flex target with `44px` minimum height and `7px` internal gap. Checkboxes are `14px` square with no margin and use the body foreground as their accent. Keyboard focus receives a `2px` title-colored outline offset by `5px`.

The pause checkbox holds the current frame through `animation-play-state:paused!important`. Keep that priority: it must override the restored animation after reduced-motion opt-in. Space toggles the focused native checkbox.

For `prefers-reduced-motion:reduce`, animated frames have `animation:none!important` and the opening chair stays visible. Only then is the “播放动画” checkbox displayed. Explicit opt-in restores each named frame animation, its 18-second duration, discrete timing and infinite loop, and hides the still. Pause and resume continue to work in that mode.

### Replay and profile links

Links inherit the controls color and the same `44px` minimum target height. Hover brightens them and adds an underline offset by `4px`; keyboard focus uses the shared outline. “重播” navigates to `./` to reopen the site. The profile link points to `/@lightjunction`. These controls do not perform account actions.

## Do's and Don'ts

### Do:

- **Do** preserve the user-fixed chair-to-nuclear-sculpture subject and the stage-first composition.
- **Do** keep scene geometry in the 96-by-54 ASCII grid and retain perspective, depth and lighting.
- **Do** keep the opening chair static by default for reduced motion and require explicit opt-in to animate.
- **Do** preserve native keyboard controls, focus visibility and the pause rule's `!important` priority.
- **Do** publish only `index.html`; the generator, acceptance script and local screenshots are supporting evidence.

### Don't:

- **Don't** import the separate `/@root/web/` brand into this personal artwork.
- **Don't** add scripts, WebGL, external fonts, image assets, audio or external network requests to the hosted page.
- **Don't** widen the opaque-origin CSP sandbox to support this scene.
- **Don't** replace spatial fragments with a two-dimensional dissolve or add whole-screen flashing.
- **Don't** treat the completed local review as proof of public signed publication.
