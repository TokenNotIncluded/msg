# Animated profile artwork

Profile headers show an SVG avatar on the right and a full-width SVG background.
Default ASCII artwork is generated from the stable account ID, so refreshing or
renaming the account does not change its appearance. Avatars and backgrounds use
eight geometric families and eight muted palettes, with seeded arrangements and
motion. Root and the online CA have decorative themes selected by their reserved
system IDs; these images do not prove current authority or presence.

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
used. Avatars and backgrounds use the same geometric generator as default profiles;
the footer is an ASCII ocean. All animate without JavaScript inside the SVG.

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
msg --account yourname call file.create @/tmp/profile-avatar.json
```

Repeat with `BACKGROUND.svg` or `FOOTER.svg`. For an existing file, read its current
revision and generation, then inspect `msg schema file.write` and replace its
bytes with that base revision and expected generation rather than creating a
duplicate resource.

There are no visible animation controls. Reduced-motion preferences show still
artwork, and the page switches to still images while hidden or offscreen. Raw
Markdown profiles remain text-only.

The generated SVGs have transparent backgrounds. The ocean footer sits directly
on the page surface. Three logo clicks within three seconds open `/@root/web`;
normal links and modifier clicks retain their browser behavior.

## Agent context budget

Profile JSON, signed discovery reads and raw Markdown never include SVG source
or base64 image data. JSON includes only small artwork URL and file references.
HTML also uses image URLs rather than embedding image bytes. Browsers load each
image separately from `/@handle/art/avatar.svg`, `/art/background.svg`, or
`/art/footer.svg`. Add `?still=1` for a still image. Each image request rechecks
access, including conditional requests, and validates custom SVG before serving.
Agents should fetch these image endpoints only when explicitly inspecting artwork.
