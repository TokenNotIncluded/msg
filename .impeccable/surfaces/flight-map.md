# Multiplayer flight and regional map

Primary target: `src/msg/data/root-web.css`
Related targets: `root-web.html`, `root-web-renderer.js`, regional map module
Mode: Operate.

The user selected geek culture, ASCII art, cryptographic restraint, minimalism and
Agents. Flight is a playable multiplayer layer over the existing geometric token
field. Keep the black ground, the sparse character field and the scene itself
visible. The interface uses compact monospace readouts, thin lines and square
controls; it does not imitate an instrument dashboard or invent telemetry.

## Content boundary

Connection, region, health, fuel, score, respawn and skill cooldowns reflect the
actual game state. Initial unknown values use a dash until state arrives. Game
participants are distinct from MSG account presence. Scene speed is in scene
units, not physical measurements. Status colors indicate game state, never a
certificate, account reputation, encryption guarantee or a fabricated online user.
Do not inject game snapshots, keyboard help or decoration into default machine
Markdown reads. The human browser owns this game interface.

## DOM contract

- `#game-hud`: top-left status with `.game-session` and `.game-vitals`.
- `#game-connection[data-state]`: `connecting`, `connected`, `stale`,
  `disconnected` or `error` from the actual connection lifecycle.
- `#game-region`: region output or button opening the map; `#game-score` and
  `#game-respawn`: server state, not account statistics.
- `.game-vital`: label, `#game-hp` / `#game-fuel` outputs and native
  `progress.game-meter`; optional `data-critical="true"` for a real threshold.
- `#game-actions`: independent combat controls, outside the status readout;
  `.game-action` contains action name, optional `kbd` and `.game-cooldown`.
  Runtime authority decides disabled/active state. `.game-combat-controls`
  supports grouped actions; `data-active="true"` marks an actually active skill.
- Existing `#pilot-hud`, `#pilot-speed`, `#pilot-inspect`, `#pilot-reticle`,
  `.pilot-touch-controls`, `.pilot-pad`, `.pilot-throttle`, `[data-flight-key]`
  remain compatible. `.game-key-legend` appears on desktop. Desktop key help
  reflects actual bindings; touch actions have short Chinese labels.
- `#region-map`: local region-selection sheet with `.region-map-head`,
  `#region-map-close`, `#region-map-canvas`, `#region-map-status` and
  `#region-map-actions`. `.region-map-tools`, `.region-map-regions` and
  `.region-map-note` support the map module. Real region selection uses native
  buttons with `aria-pressed`; the canvas has a keyboard alternative.

## Layout and safety

The top-left HUD stays compact; center aim remains open. Actions sit above the
bottom navigation, separately from movement controls. Touch targets are at least
44 pixels; keyboard focus remains visible. Portrait phones show movement and
combat controls without blocking each other. Short landscape screens put combat
at the upper-right and reduce chrome height. Respect safe-area insets and reduced
motion. The regional map is a restrained flat sheet, with its real status and
selectable regions available below the canvas.

The previous HTML flight styles move into the CSS module to avoid a second late
inline stylesheet overriding this contract. No external fonts/assets or new script
permission is introduced here. Verify in at most two batched desktop/phone passes
after integration. Static fixtures demonstrate layout only; connected gameplay
requires an actual two-client server test and must be reported separately.
