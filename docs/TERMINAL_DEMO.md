# Terminal walkthrough

The README animation is an **illustrated command walkthrough**, not a recording
of production output. It makes no network requests and sends no messages. The
address is `msg.lmm.best`. The displayed `lightjunction` username is an example;
use your own authenticated account. Username matching prevents the address from
selecting someone else's identity.

The presentation takes inspiration from [Termium's concise terminal
quickstart](https://github.com/codr1/termium). No Termium code or artwork is copied.
MSG's `user@domain` selects an HTTPS MSG service; it does not establish an SSH
connection or execute the quoted command in a remote shell.

## Commands shown

```bash
# Empty command: open the read-only TUI.
msg lightjunction@msg.lmm.best ""

# Read a topic. Read a specific post with its returned path or resource ID.
msg lightjunction@msg.lmm.best "read /main"

# Post an update. Save the resource ID/path returned by the service.
msg lightjunction@msg.lmm.best 'post /main --text "Build is ready."'

# Replace <post-id> with that identifier before running this command.
msg lightjunction@msg.lmm.best 'reply <post-id> --text "I will review it."'

# Create a Git repository through the declared operation interface.
cat > repo.json <<'JSON'
{"parent":"/@lightjunction","name":"demo.git"}
JSON
msg lightjunction@msg.lmm.best "call git.create @repo.json"

# Inspect its references. Native Git transports handle Git content.
cat > refs.json <<'JSON'
{"id":"/@lightjunction/demo.git"}
JSON
msg lightjunction@msg.lmm.best "call git.refs @refs.json"
```

Writes need credentials with the relevant operation permissions. Git operations
require an installation that enables the Git extension. These examples create a
repository only when run by the user; the animation itself runs none of them.
The `@file` form avoids nested JSON quoting inside a connection command.

## Regenerate the GIF

Use Pillow and a local monospace TrueType font, for example JetBrains Mono:

```bash
uv run --with pillow python scripts/render_terminal_demo.py \
  --font /usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf
```

The script writes `docs/media/msg-terminal-demo.gif`: 1040 × 570 pixels, about
20 seconds, five scenes, an infinite loop and an explicit illustration label.
It does not bundle a font or add Pillow to the MSG client dependencies.
