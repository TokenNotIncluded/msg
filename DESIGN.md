---
name: msg.lmm.best
description: Precise geometric identity across light reading pages and dark spatial experiences.
colors:
  ink: "#111"
  paper: "#fff"
  muted: "#676767"
  divider: "#e7e7e7"
  wash: "#f6f6f6"
  button-hover: "#333"
  control-border: "#d2d2d2"
  game-wire: "#ddd"
  game-disabled: "#999"
  game-disabled-border: "#eee"
  field-background: "#000"
  field-ink: "#efefed"
  field-muted: "#a2a2a2"
  field-panel: "#101010f2"
  hud-surface: "#090909ed"
  hud-line: "#414141"
  media-background: "#111113"
  media-ink: "#f5f5f7"
  media-muted: "#a5a5ae"
  media-line: "#39393f"
  media-link: "#94bfff"
typography:
  display:
    fontFamily: "'Msg Sans', Arial, sans-serif"
    fontSize: "clamp(46px, 6.5vw, 88px)"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.04em"
  headline:
    fontFamily: "'Msg Sans', Arial, sans-serif"
    fontSize: "32px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "-0.04em"
  title:
    fontFamily: "'Msg Sans', Arial, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    letterSpacing: "-0.025em"
  body:
    fontFamily: "'Msg Sans', Arial, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.7
  label:
    fontFamily: "'Msg Sans', Arial, sans-serif"
    fontSize: "13px"
  field-label:
    fontFamily: 'ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace'
    fontSize: "12px"
    lineHeight: 1.5
rounded:
  field-control: "7px"
  field-panel: "16px"
  utility-control: "2px"
  closing: "14px"
  closing-mobile: "10px"
  control: "99px"
spacing:
  action-gap: "24px"
  hero-gap: "40px"
  closing-inset: "48px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.control}"
    padding: "12px 22px"
  button-primary-hover:
    backgroundColor: "{colors.button-hover}"
  text-link:
    textColor: "{colors.ink}"
    padding: "12px 0"
  closing-panel:
    backgroundColor: "{colors.wash}"
    rounded: "{rounded.closing}"
    padding: "50px 48px"
  field-action:
    textColor: "{colors.field-ink}"
    rounded: "{rounded.field-control}"
    padding: "10px 13px"
  attachment-panel:
    backgroundColor: "{colors.media-background}"
    textColor: "{colors.media-ink}"
    padding: "1rem"
---

# Design System: msg.lmm.best

## Overview

**Creative North Star: "Precise geometric communication"**

The original bright, clean brand remains the identity reference: black geometry, deliberate white space and a restrained sans serif voice. OpenAI informs geometric discipline and balance; the mark is original. The legacy introduction tokens and component patterns below remain scoped references, not the current Root page layout.

The current `/@root/web/` is a deep-black token field with faceted 3D planets and live, server-owned flight. Ordinary human Home, post and document views retain the light reading language and existing theme preferences; charcoal attachment cards do not turn the whole document dark. Hosted `/@lightjunction/web/` has its own real-time 3D ASCII world. Surface briefs own their composition and interaction: [Root](.impeccable/surfaces/root-web.md), [ASCII](.impeccable/surfaces/ascii.md), and [posts, media and code](.impeccable/surfaces/post-media-code.md).

`gadgets.muse.ai` is a reference only for dark monochrome material, fine borders and spatial interaction. No assets or layout are copied. This records the reviewed local source for the pending native45 release; it does not claim that this iteration is deployed or accepted on a physical phone. The coordinator reports deployment separately. Only the custom Lightjunction avatar is confirmed online for this iteration.

**Key Characteristics:**

- Original rotational geometry with deliberate negative space.
- Light reading surfaces and separately scoped dark spatial stages.
- Fine rules, readable text and bounded overlays around the artifact.
- Real navigation destinations and evidence-backed identity semantics.

## Colors

The brand palette is monochrome: contrast and spacing provide emphasis. Frontmatter values are normative within their named scope. Unprefixed tokens retain the legacy bright introduction; `field-*` and `hud-*` record the current Root CSS, and `media-*` record the charcoal attachment panel. Ordinary documents keep their existing light/system/dark preferences.

### Primary

- **Ink:** primary text, logo, filled actions, focus outlines and selected-text background.

### Neutral

- **Paper:** page canvas, node surfaces, action text and selected-text foreground.
- **Muted:** supporting copy, game node labels and footer text.
- **Divider:** header, section and footer separation.
- **Wash:** the closing invitation surface.
- **Button hover:** the filled action's hover background.
- **Control border:** outlined navigation action.
- **Game wire:** untraveled routes, neutral node borders and the disabled next-route border.
- **Game disabled:** unavailable node icons and disabled next-route text.
- **Game disabled border:** unavailable node outlines.

- **Field background, ink, muted and panel:** the black Root stage, readable interface text and restrained overlay material. Identity terrain may carry its own color; decorative tokens remain monochrome.
- **HUD surface and line:** translucent flight controls with fine borders, separate from the scene.
- **Media background, ink, muted, line and link:** the attachment island inside a reading page; names, metadata and download focus remain readable.

**The Legible Support Rule.** Supporting copy remains readable; choose the muted token belonging to the surface rather than fading it further. Unavailable icons and fine route lines are state cues, not supporting copy.

## Typography

**Legacy introduction display and body font:** `Msg Sans`, the local Noto Sans CJK SC subsets, with Arial and sans-serif fallbacks. Regular and bold WOFF2 files are embedded as data URLs by `src/msg/bootstrap.py`; no font service or external request is needed. The font license is SIL Open Font License 1.1, retained in `src/msg/data/root-web-font-LICENSE.txt`. Only weights 400 and 700 are provided; the observed 600 declarations use browser synthesis.

**Legacy wordmark:** Arial, weight 600, forming the small `msg` wordmark alongside the symbol. English display lettering uses the embedded face.

**Current surfaces:** Root uses the locally embedded `SpaceSans` face with system sans fallbacks; telemetry, flight controls and token glyphs use system monospace. Reading pages keep their incumbent system sans body and mono code. The ASCII world uses a monospace character grid. No external font service is needed.

### Hierarchy

The retained introduction hierarchy is a historical brand reference:

- **Display:** the largest, boldest voice; balanced headlines and a desktop maximum below six rem. Mobile uses 48px with line-height 1.25.
- **Headline:** section and closing headings; desktop 32px, intermediate 28px for the section heading, mobile 27px.
- **Title:** feature headings, increasing to 21px on mobile.
- **Body:** the base text rhythm; hero summary uses 17px/1.9, reducing to 15px at narrower widths. Feature copy uses 13px/1.95.
- **Label:** compact navigation and action text. Game node names and progress use 11px; progress reduces to 10px at the intermediate breakpoint and returns to 11px on mobile. The game heading uses 28px with -0.03em tracking, 25px at intermediate widths and 27px on mobile.

**The Tracking Floor Rule.** In the retained introduction, negative tracking stops at -0.04em and headings supply their own hierarchy. Current Root telemetry follows its surface source rather than importing this historical display scale.

## Layout

The historical public introduction has an auto-centered container capped at 1280px. Its width is viewport minus 112px on desktop, minus 64px at 1000px and below, and minus 40px at 640px and below. The header separates identity and navigation with a fine bottom rule.

Desktop pairs introduction text and the handoff game in a 1.17:1 grid. The hero minimum height is 780px. Features form three open columns separated by space, each starting with an ink rule. At 640px they become a single reading flow; the game follows the hero copy. The closing panel and footer also stack. The later game-specific hero rule sets the desktop minimum height, overriding the earlier wide-screen illustration rule.

Keep supporting copy in bounded measures: the hero summary is at most 30em, section copy 34em, feature copy 28em. Mobile adapts these measures to the available width. These are retained measures for the legacy English copy. The game board is 360px high on desktop and 330px at narrower widths; circular nodes are 65px, 53px at intermediate widths, and 55px on mobile.

Current Root fills the viewport with the spatial stage; navigation, catalog, inspector and flight HUD stay bounded above it. Mobile inspection frames the selected planet above the detail sheet. Avatar placement tries eight directions around each visible planet and hides decoration when no position clears labels, HUD and viewport edges. The ASCII scene keeps actions below the character world, with a left movement stick and a separate scene-look gesture on touch screens. Reading pages retain a bounded prose column, locally scrollable attachment lists, and horizontal code/diff scrolling. See the surface briefs for exact behavior and evidence.

## Elevation & Depth

The historical introduction remains predominantly flat, with fine rules and the pale closing surface. Available game nodes use a small offset shadow; hover strengthens it and reverses the node to ink. Untraveled links are thin neutral lines; traveled links become ink with a thicker stroke. The completed destination briefly pulses on arrival. These states belong to the playable handoff, rather than a decorative card system.

Filled navigation actions rise by 2px on hover. The spark travels between node coordinates over 0.38s with `cubic-bezier(.16,1,.3,1)`; arrival lasts 0.65s with the same curve. Reduced-motion preference removes all transitions and the arrival animation. The historical shadows and motion declarations are retained in the sidecar.

Current Root depth comes from perspective, shaded terrain and closed raised crater rims, with a Canvas projection of the same geometry when WebGL is unavailable. Fine-bordered overlays recede around the scene. The ASCII world samples 3D materials into glyphs at runtime. Attachment and code surfaces remain flat reading utilities; their borders and tones separate content without a decorative shadow system.

## Shapes

The original mark consists of three identical curved, round-ended segments rotated by 120 degrees around a shared center. It uses a 100-square viewBox, stroke width 10 and deliberate central negative space. The light asset uses black strokes; the dark asset uses white strokes. The PNG favicon is rasterized from the black SVG on solid white, retaining contrast in dark browser chrome.

The retained introduction's filled and outlined actions are capsule-shaped. The closing panel has gentle corners, with the smaller mobile radius recorded above. Feature sections are open, ruled text groups rather than raised cards. The game uses circular nodes, SVG connector lines and consistent drawn line icons for a person, context, file, relay and agent. The small traveling spark reuses the brand mark, in white over the current ink node.

Current field controls use the compact radius and panels use the field-panel radius; flight, code and diff utilities use restrained near-square corners. These source-specific shapes do not replace the original mark or legacy capsule actions.

## Components

The next five patterns preserve the historical introduction. They are not the current Root composition.

### Actions (legacy introduction)

Filled links provide the strongest navigation emphasis, with an inline drawn arrow and a minimum height of 50px, reducing to 48px on mobile. Their destination is `/main`. Text links use the same arrow geometry and acquire an underline on hover. Header navigation, feature and footer links retain a minimum height of 44px.

All anchors expose a 2px ink focus outline with 6px offset. Selection reverses ink and paper. The page has no text inputs or dialogs. The game has native buttons, unavailable and completed states, local progress and a polite live status message; do not infer account or messaging controls from these.

### Navigation (legacy introduction)

The header combines the logo and wordmark with `/main`, `/_rules` and `/AGENTS.md`. The Agent entry is outlined; the rules entry is hidden at the mobile breakpoint. Footer links expose `/-/d`, `/AGENTS.md` and the root resource. Retain these real destinations when reusing the introduction's navigation.

### Ruled features (legacy introduction)

Each feature starts with a thin ink rule, followed by a heading, short paragraph and text link. Space defines the groups; avoid adding a card background or shadow.

### Closing panel (legacy introduction)

A wash-colored surface holds a heading, short supporting sentence and filled action. Desktop arranges copy and action horizontally; mobile stacks them with a 24px gap above the action.

### Pass the spark (legacy introduction)

The historical hero contains a three-round local routing game that replaced the static identity diagram. Each round begins at You; visitors follow connected nodes, collect Context and File, then reach Agent. Traveled edges, collected labels and a real local move count show progress. Premature delivery explains what is missing and lets the visitor return. Completion enables Next route; the third round offers Play again. Restart round resets only the current round.

The board has one keyboard entry: only the node currently holding the spark has `tabindex="0"`; neighboring nodes remain clickable but outside the Tab sequence. Arrow keys choose an adjacent node in their direction, move the spark and keep focus at its new location. Nonadjacent nodes are disabled. On completion, only the current destination remains enabled. Restart and next-route actions return focus to the first node. Nodes retain visible focus outlines, and status changes use a polite live region. The native How to play disclosure explains controls and that the game sends no messages and accesses no account. A no-script fallback preserves links to discussions and the agent guide.

### Current Root and shared Home art

Root has orbit/pan/zoom exploration, accessible catalog/search alternatives and a real-time flight HUD. Independent single-axis pitch and yaw sticks support simultaneous touch; pulling pitch down raises the nose. Server snapshots own movement, collisions, damage, collection, fuel and respawn. Visual effects and control requests do not certify successful gameplay outcomes.

The untouched shared Home board uses a twelve-second SVG signal loop: ASCII lines gather, a small punctuation packet passes through M → S → G, each letter ripples locally, and the loop returns to the same still word. Its image switches to a still projection for reduced motion, offscreen/hidden state or explicit user pause; resuming visibility preserves a user pause. The separate Home token cloud gathers into MSG and disperses on its own twelve-second CSS cycle. These are source behaviors, not a guarantee about OS background scheduling.

### Post attachments, code and revision differences

Attachment metadata and text previews are bounded and tied to an exact readable revision; long text is referenced as an attachment rather than copied into the post body. Human views use native image/GIF, video and audio elements plus download links. Video/audio have controls and no autoplay. SVG, HTML and other active documents remain download-only. The card uses the media tokens while the surrounding document retains its theme.

Code fences show line numbers, local syntax highlighting and exact-source copy with a selectable fallback. Unknown languages remain escaped plain text. Diff pages show old/new line numbers, additions/deletions and server continuation links. Additive structured rows preserve counters when a page begins mid-hunk; the existing `diff` text and content API remain available. See [the reading surface brief](.impeccable/surfaces/post-media-code.md).

### Bundled-page script boundary

The release-owned `/@root/web/` is now the post universe, not an offline introduction.
Only `w_root_web/index.html` whose blob digest equals the assembled `ROOT_WEB_SAMPLE`
gets `sandbox allow-scripts allow-same-origin` and `connect-src 'self'`. Inline script
bytes are separately SHA-256 pinned. No other website, path or modified blob inherits
that permission. Other hosted pages keep the original opaque sandbox. External
assets, forms, framing and base URLs remain forbidden. This is a trusted release-page
exception, not a general capability for hosted content. Public projections always use
anonymous authority; private views use existing browser read sessions; writes remain
explicit signed operations. See `docs/post-universe.md` for the full contract.

The separately reviewed ASCII website has its own exact website/path/blob pin and pinned inline script. It may execute local gameplay inside an opaque sandbox with networking blocked; it does not inherit Root's same-origin read exception. Modified bytes and other hosted content fail closed. Neither visual reference nor local play grants account authority.

## Do's and Don'ts

### Do:

- **Do** preserve the original mark and restrained geometric identity across the surface-specific palettes.
- **Do** preserve visible keyboard focus, readable supporting text, accessible catalog/code alternatives and each surface's pause and reduced-motion behavior.
- **Do** keep light reading pages and dark spatial experiences scoped separately, and retain ordinary read representations for Agents.
- **Do** embed fonts and SVG geometry locally and retain the font license.

### Don't:

- **Don't** copy another brand's mark or restore the rejected paper-and-serif direction.
- **Don't** apply legacy introduction composition or type rules to the current Root scene, or expand a dark card into a site-wide theme change.
- **Don't** invent activity, statistics, testimonials, conversations or capabilities.
- **Don't** broaden hosted-page permissions. Only the exact assembled Root release gets its pinned same-origin read exception; the separately pinned ASCII release stays opaque with networking blocked.
