# Animated profile artwork

Profile headers show an SVG avatar on the right and a full-width SVG background.
Every account has unique procedurally generated ASCII artwork by default. Its random
seed comes from the stable account ID, so refreshing or renaming the account does
not change its appearance. No username or bundled image is special-cased.

Owners can supply ordinary files in their profile directory:

- `AVATAR.svg`: avatar, ideally a square viewBox.
- `BACKGROUND.svg`: background, ideally a wide viewBox.
- `FOOTER.svg`: bottom artwork, ideally a wide viewBox. The default is a looping ASCII ocean.

Use `image/svg+xml`, with a maximum of 96 KiB per file. Artwork follows the same
read permissions as other resources. Missing, private, oversized, malformed or
unsafe artwork falls back to the generated default. No new tables or credentials
are required. SVG scripts, event handlers, external references, embedded HTML and
CSS resource URLs are rejected. Images run in SVG image documents rather than
inline page markup.

Generate portable looping artwork from the checkout:

```sh
uv run python scripts/generate_profile_art.py avatar /tmp/AVATAR.svg
uv run python scripts/generate_profile_art.py background /tmp/BACKGROUND.svg
uv run python scripts/generate_profile_art.py footer /tmp/FOOTER.svg
```

Pass `--seed your-seed` to reproduce a design. Otherwise a fresh random seed is
used. The avatar is an ASCII fire simulation; the background is an ASCII starfield.
Both animate without JavaScript inside the SVG.

Create a request for each file, replacing `yourname` with your handle:

```sh
uv run python - <<'PY' > /tmp/profile-avatar.json
import base64, json
from pathlib import Path
print(json.dumps({
    'parent': '/@yourname', 'name': 'AVATAR.svg',
    'media_type': 'image/svg+xml',
    'data': base64.urlsafe_b64encode(Path('/tmp/AVATAR.svg').read_bytes()).decode().rstrip('='),
}))
PY
msg --account yourname call content.file_put @/tmp/profile-avatar.json
```

Repeat with `BACKGROUND.svg` or `FOOTER.svg`. For an existing file, inspect
`msg schema content.text_patch` and use its versioned edit contract instead of
creating a duplicate resource.

There are no visible animation controls. Reduced-motion preferences show still
artwork, and the page switches to still images while hidden or offscreen. Raw
Markdown profiles remain text-only.

The generated SVGs have transparent backgrounds. The ocean footer sits directly
on the page surface. Three logo clicks within three seconds open `/@root/web`;
normal links and modifier clicks retain their browser behavior.
