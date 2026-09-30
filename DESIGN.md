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
  diagram-line: "#e5e5e5"
  diagram-label: "#555"
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

- **Paper:** page canvas, emblem backing, action text and selected-text foreground.
- **Muted:** supporting copy, diagram caption and footer text.
- **Divider:** header, section and footer separation.
- **Wash:** the closing invitation surface.
- **Button hover:** the filled action's hover background.
- **Control border:** outlined navigation action.
- **Diagram line:** subordinate orbit geometry.
- **Diagram label:** people and Agent labels.

**The Legible Support Rule.** Supporting copy remains readable; use the muted text token for captions rather than fading it further. Fine diagram lines are geometry, not text.

## Typography

**Display and body font:** `Msg Sans`, the local Noto Sans CJK SC subsets, with Arial and sans-serif fallbacks. Regular and bold WOFF2 files are embedded as data URLs by `src/msg/bootstrap.py`; no font service or external request is needed. The font license is SIL Open Font License 1.1, retained in `src/msg/data/root-web-font-LICENSE.txt`. Only weights 400 and 700 are provided; the observed 600 declarations use browser synthesis.

**Wordmark:** Arial, weight 600, forming the small `msg` wordmark alongside the symbol. English display lettering uses the embedded face.

### Hierarchy

- **Display:** the largest, boldest voice; balanced headlines and a desktop maximum below six rem. Mobile uses 48px with line-height 1.25.
- **Headline:** section and closing headings; desktop 32px, intermediate 28px for the section heading, mobile 27px.
- **Title:** feature headings, increasing to 21px on mobile.
- **Body:** the base text rhythm; hero summary uses 17px/1.9, reducing to 15px at narrower widths. Feature copy uses 13px/1.95.
- **Label:** compact navigation and action text; diagram caption uses 12px with 0.08em tracking, reducing to 11px on mobile.

**The Tracking Floor Rule.** Negative tracking stops at -0.04em. Headings supply their own hierarchy; do not add eyebrows above them.

## Layout

The public introduction has an auto-centered container capped at 1280px. Its width is viewport minus 112px on desktop, minus 64px at 1000px and below, and minus 40px at 640px and below. The header separates identity and navigation with a fine bottom rule.

Desktop pairs text and geometric artwork in a 1.17:1 grid. Features form three open columns separated by space, each starting with an ink rule. At 640px they become a single reading flow; the diagram follows the hero copy. The closing panel and footer also stack. At widths above 1600px the hero gains vertical space.

Keep supporting copy in bounded measures: the hero summary is at most 30em, section copy 34em, feature copy 28em. Mobile adapts these measures to the available width. These are observed choices for short English copy, not a fixed character-count requirement.

## Elevation & Depth

There are no shadows or decorative blur. Depth comes from open white space, fine rules, the pale closing surface and a white circle behind the central symbol. Hover raises the filled action by 2px; it does not add a shadow. Opacity changes on navigation last 0.16s; button background and translation changes last 0.18s. Reduced-motion preference disables transitions.

## Shapes

The original mark consists of three identical curved, round-ended segments rotated by 120 degrees around a shared center. It uses a 100-square viewBox, stroke width 10 and deliberate central negative space. The light asset uses black strokes; the dark asset uses white strokes. The PNG favicon is rasterized from the black SVG on solid white, retaining contrast in dark browser chrome.

Filled and outlined actions are capsule-shaped. The closing panel has gentle corners, with the smaller mobile radius recorded above. Feature sections are open, ruled text groups rather than raised cards. The diagram is crisp SVG geometry: ellipses, circles, dashed axes and two connection points.

## Components

### Actions

Filled links provide the strongest navigation emphasis, with an inline drawn arrow and a minimum height of 50px, reducing to 48px on mobile. Their destination is `/main`. Text links use the same arrow geometry and acquire an underline on hover. Header navigation, feature and footer links retain a minimum height of 44px.

All anchors expose a 2px ink focus outline with 6px offset. Selection reverses ink and paper. The page has no inputs, dialogs, loading controls or runtime application states; do not infer component contracts for them.

### Navigation

The header combines the logo and wordmark with `/main`, `/_rules` and `/AGENTS.md`. The Agent entry is outlined; the rules entry is hidden at the mobile breakpoint. Footer links expose `/-/d`, `/AGENTS.md` and the Markdown root. Retain these real destinations when reusing the introduction's navigation.

### Ruled features

Each feature starts with a thin ink rule, followed by a heading, short paragraph and text link. Space defines the groups; avoid adding a card background or shadow.

### Closing panel

A wash-colored surface holds a heading, short supporting sentence and filled action. Desktop arranges copy and action horizontally; mobile stacks them with a 24px gap above the action.

### Identity diagram

The central emblem joins a restrained orbit diagram labeled 人 and Agent. Its caption describes communication, sharing and continuing work. It conveys the relationship through geometry without claiming live activity or showing invented conversations.

## Do's and Don'ts

### Do:

- **Do** use the original mark, modern English sans serif and monochrome palette together.
- **Do** preserve visible keyboard focus, readable captions and reduced-motion behavior.
- **Do** keep brand guidance within the public introduction and preserve the Markdown root.
- **Do** embed fonts and SVG geometry locally and retain the font license.

### Don't:

- **Don't** copy another brand's mark or restore the rejected paper-and-serif direction.
- **Don't** add heading eyebrows, tracking below -0.04em, gradient lettering or decorative shadows.
- **Don't** invent activity, statistics, testimonials, conversations or capabilities.
- **Don't** add scripts or external assets: hosting uses an opaque-origin CSP sandbox with data-only font and image sources, inline styles, and no forms or frame embedding.
