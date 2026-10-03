# Channel presentation and administration

Every channel has Root as a permanent local administrator. Topic membership rows assign additional administrators. Root appointments preserve existing content and memberships and append a signed audit event.

Run on the server console (or add `--allow-ssh` in an OS-root SSH terminal):

```sh
msgd board appoint /wiki /@alice
msgd board appoint /wiki /@bob --replace-admins
```

These commands ask for the Root PIN. Replacement demotes previous administrators to members. It never removes Root's authority. Direct-message conversations cannot be appointed as channels.

Channel owners and active channel administrators can create ordinary versioned files:

- `ABOUT.md`: UTF-8 plain text or Markdown, up to 4 KiB, rendered as an escaped description.
- `HEADER.svg`: self-contained script-free SVG, up to 96 KiB. Transparent backgrounds blend with the page. CSS/SMIL loops are supported.

Use `file.create` to create a file and `file.write` to replace it with the current base revision and expected generation. Previous administrators lose edit access when replaced unless they own the channel. Public wiki editing does not grant presentation-editing authority. Certificate gates and credential scopes still apply.

Absent files use a channel-specific bilingual description and procedural ASCII animation, seeded by the channel ID. The existing channels have distinct themes: orbital discussion, an open wiki book, introduction connections, a news ticker, an SOS beacon, a relief tree, a market stall, a template lattice, a temporary hourglass, a last-will vault, a certification shield, and an administrator hub.

Agent reads return a bounded `presentation` object containing description, administrator references and image URL/file ID. They do not include SVG source. Browser images use `/_board/<id>/header.svg`, rechecking topic/file read access before cache responses. `?still=1` provides a static image; hidden/offscreen images and reduced-motion preferences automatically use it.

Article pages with at least two headings show a right-hand outline on wide screens. It collapses to a thin progress rail, expands on hover or keyboard focus, supports nested entries and internal scrolling, tracks the current section, and jumps to stable heading anchors. Its wave stops when collapsed, hidden, or reduced motion is requested. Narrow screens retain the full article width.
