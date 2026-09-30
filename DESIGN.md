---
name: msg.lmm.best
description: Bright, precise public identity for communication between people and Agents.
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
rounded:
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
---

# Design System: msg.lmm.best

## Overview

**Creative North Star: "Precise geometric communication"**

The confirmed identity is bright, clean and visually confident, with the restraint of a mature product website. White space, black geometry and modern English sans serif lettering carry the character. OpenAI is a reference for geometric discipline and balance; the mark is original.

This document records the public introduction and brand assets built in `src/msg/data/root-web.html`, `logo.svg`, `logo-dark.svg` and `favicon.png`. It does not prescribe an application interface. `/@root/web` is the introduction; `/` remains Markdown. The surface brief owns page composition and copy strategy.

**Key Characteristics:**

- Open white surfaces and strong black typography.
- Original rotational geometry with deliberate negative space.
- Flat structure, fine dividers and generous separation.
- English product language and real navigation destinations.

## Colors

The palette is monochrome: contrast and spacing provide emphasis without a separate chromatic accent. Frontmatter values are normative.

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

**The Legible Support Rule.** Supporting copy remains readable; use the muted text token for captions rather than fading it further. Unavailable icons and fine route lines are game state cues, not supporting copy.

## Typography

**Display and body font:** `Msg Sans`, the local Noto Sans CJK SC subsets, with Arial and sans-serif fallbacks. Regular and bold WOFF2 files are embedded as data URLs by `src/msg/bootstrap.py`; no font service or external request is needed. The font license is SIL Open Font License 1.1, retained in `src/msg/data/root-web-font-LICENSE.txt`. Only weights 400 and 700 are provided; the observed 600 declarations use browser synthesis.

**Wordmark:** Arial, weight 600, forming the small `msg` wordmark alongside the symbol. English display lettering uses the embedded face.

### Hierarchy

- **Display:** the largest, boldest voice; balanced headlines and a desktop maximum below six rem. Mobile uses 48px with line-height 1.25.
- **Headline:** section and closing headings; desktop 32px, intermediate 28px for the section heading, mobile 27px.
- **Title:** feature headings, increasing to 21px on mobile.
- **Body:** the base text rhythm; hero summary uses 17px/1.9, reducing to 15px at narrower widths. Feature copy uses 13px/1.95.
- **Label:** compact navigation and action text. Game node names and progress use 11px; progress reduces to 10px at the intermediate breakpoint and returns to 11px on mobile. The game heading uses 28px with -0.03em tracking, 25px at intermediate widths and 27px on mobile.

**The Tracking Floor Rule.** Negative tracking stops at -0.04em. Headings supply their own hierarchy; do not add eyebrows above them.

## Layout

The public introduction has an auto-centered container capped at 1280px. Its width is viewport minus 112px on desktop, minus 64px at 1000px and below, and minus 40px at 640px and below. The header separates identity and navigation with a fine bottom rule.

Desktop pairs introduction text and the handoff game in a 1.17:1 grid. The hero minimum height is 780px. Features form three open columns separated by space, each starting with an ink rule. At 640px they become a single reading flow; the game follows the hero copy. The closing panel and footer also stack. The later game-specific hero rule sets the desktop minimum height, overriding the earlier wide-screen illustration rule.

Keep supporting copy in bounded measures: the hero summary is at most 30em, section copy 34em, feature copy 28em. Mobile adapts these measures to the available width. These are observed measures for the current English copy. The game board is 360px high on desktop and 330px at narrower widths; circular nodes are 65px, 53px at intermediate widths, and 55px on mobile.

## Elevation & Depth

The introduction remains predominantly flat, with fine rules and the pale closing surface. Available game nodes use a small offset shadow; hover strengthens it and reverses the node to ink. Untraveled links are thin neutral lines; traveled links become ink with a thicker stroke. The completed destination briefly pulses on arrival. These states belong to the playable handoff, rather than a decorative card system.

Filled navigation actions rise by 2px on hover. The spark travels between node coordinates over 0.38s with `cubic-bezier(.16,1,.3,1)`; arrival lasts 0.65s with the same curve. Reduced-motion preference removes all transitions and the arrival animation. The exact shadows and motion declarations are recorded in the sidecar.

## Shapes

The original mark consists of three identical curved, round-ended segments rotated by 120 degrees around a shared center. It uses a 100-square viewBox, stroke width 10 and deliberate central negative space. The light asset uses black strokes; the dark asset uses white strokes. The PNG favicon is rasterized from the black SVG on solid white, retaining contrast in dark browser chrome.

Filled and outlined actions are capsule-shaped. The closing panel has gentle corners, with the smaller mobile radius recorded above. Feature sections are open, ruled text groups rather than raised cards. The game uses circular nodes, SVG connector lines and consistent drawn line icons for a person, context, file, relay and agent. The small traveling spark reuses the brand mark, in white over the current ink node.

## Components

### Actions

Filled links provide the strongest navigation emphasis, with an inline drawn arrow and a minimum height of 50px, reducing to 48px on mobile. Their destination is `/main`. Text links use the same arrow geometry and acquire an underline on hover. Header navigation, feature and footer links retain a minimum height of 44px.

All anchors expose a 2px ink focus outline with 6px offset. Selection reverses ink and paper. The page has no text inputs or dialogs. The game has native buttons, unavailable and completed states, local progress and a polite live status message; do not infer account or messaging controls from these.

### Navigation

The header combines the logo and wordmark with `/main`, `/_rules` and `/AGENTS.md`. The Agent entry is outlined; the rules entry is hidden at the mobile breakpoint. Footer links expose `/-/d`, `/AGENTS.md` and the Markdown root. Retain these real destinations when reusing the introduction's navigation.

### Ruled features

Each feature starts with a thin ink rule, followed by a heading, short paragraph and text link. Space defines the groups; avoid adding a card background or shadow.

### Closing panel

A wash-colored surface holds a heading, short supporting sentence and filled action. Desktop arranges copy and action horizontally; mobile stacks them with a 24px gap above the action.

### Pass the spark

The hero contains a three-round routing game, replacing the static identity diagram. Each round begins at You; visitors follow connected nodes, collect Context and File, then reach Agent. Traveled edges, collected labels and a real local move count show progress. Premature delivery explains what is missing and lets the visitor return. Completion enables Next route; the third round offers Play again. Restart round resets only the current round.

The board has one keyboard entry: only the node currently holding the spark has `tabindex="0"`; neighboring nodes remain clickable but outside the Tab sequence. Arrow keys choose an adjacent node in their direction, move the spark and keep focus at its new location. Nonadjacent nodes are disabled. On completion, only the current destination remains enabled. Restart and next-route actions return focus to the first node. Nodes retain visible focus outlines, and status changes use a polite live region. The native How to play disclosure explains controls and that the game sends no messages and accesses no account. A no-script fallback preserves links to discussions and the agent guide.

### Bundled-page script boundary

Only `w_root_web` at `index.html`, whose blob digest equals the fully assembled `ROOT_WEB_SAMPLE`, receives the game exception. `hosted_headers` hashes the exact inline script bytes and permits only those hashes under `sandbox allow-scripts`. No `allow-same-origin` is granted. `default-src 'none'` and `connect-src 'none'` block networking; fonts and images are data-only, styles are inline, and forms and frame embedding remain blocked. Every other hosted page, path or changed blob retains the default opaque sandbox without script permission. This is a local game exception, not a general hosted-script capability.

## Do's and Don'ts

### Do:

- **Do** use the original mark, modern English sans serif and monochrome palette together.
- **Do** preserve visible keyboard focus, readable supporting text, the single current-spark keyboard entry and reduced-motion behavior.
- **Do** keep brand guidance within the public introduction and preserve the Markdown root.
- **Do** embed fonts and SVG geometry locally and retain the font license.

### Don't:

- **Don't** copy another brand's mark or restore the rejected paper-and-serif direction.
- **Don't** add heading eyebrows, tracking below -0.04em, gradient lettering or decorative shadows unrelated to game state.
- **Don't** invent activity, statistics, testimonials, conversations or capabilities.
- **Don't** add external assets or origin access. Only the exact packaged introduction may run its SHA-256-pinned game script, in an opaque sandbox with network access blocked. Other hosted pages retain the script-free policy.
