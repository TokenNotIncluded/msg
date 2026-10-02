# Planet surfaces and profile avatars

`root-web-planets.js` adds decorative geology seeded by the stable account ID.
Continents, ridges, crater basins, rocky bands and ice caps are visual features,
not certificate, presence or balance claims. Existing public facts still control
the root anchor, certificate seal, presence glow and reserve ring in the renderer.

Solid surface vertices stay between 0.952 and 1 times the existing authoritative
planet radius. Collision and gravity continue to use that spherical radius;
small depressions are visual, so a ship may stop just above a valley. Transparent
clouds sit at 1.008 times the radius and are intangible. Rotation and sunlight
change the surface shading without changing positions or physics.

The renderer starts with 128 solid triangles on a cold cache. It builds nearby
surfaces during idle time, with at most four pending jobs and up to 64 faces or
3 ms of work per callback. Desktop near views reach 2,048 solid triangles; mobile
and software views reach 512. Cloud shells add at most 512 sparse triangles.
The cache keeps at most 32 identities, with one detail level each. The renderer
also caps the number of detailed visible bodies; distant identities remain small
points. These are geometry budgets, not a promise of a particular frame rate.

Profile avatars come only from the current anonymous `artwork.avatar.url`
projection. The client accepts the exact same-origin `/@handle/art/avatar.svg`
path belonging to that projected user, with no query, fragment or alternate file
path. Fetches omit credentials and reject redirects. Response media type, streamed
size (98,304 bytes maximum), SVG elements and active/external content are checked
before making a `data:` image. SVG animation is preserved inside the isolated
image document; no hosted-page CSP is expanded.

Only nearby on-screen public user planets get markers: at most eight on desktop
or four on mobile/software, with two concurrent fetches and sixteen cached images.
Visible artwork is rechecked after 60 seconds, including errors. Markers sit beside
the planet and ignore pointer events. Removing a marker cancels its pending request;
hidden pages, page exit, detached containers and explicit disposal remove markers,
abort requests and clear avatar data. Invalid or unavailable artwork falls back to
the first handle character.

The JS contracts are covered by `tests/js/planet-details.test.cjs`. The shared local
browser input is `tests/fixtures/planet-details-v1.json`; its accounts are fixtures,
not production identities. Public projection and hidden-art permissions are covered
separately through the actual anonymous executor and HTTP tests.
