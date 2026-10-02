"""Deterministic perspective-rendered ASCII sculpture; generated HTML runs no JS."""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path

COLS, ROWS, FRAMES, DURATION = 96, 54, 64, 18
GLYPHS = ' .,:;i!tfx*#%@'
Point = tuple[float, float, float, float, float, float, float]


def box(x, y, z, sx, sy, sz):
    """Sample six real surfaces, retaining outward normals for lighting."""
    points = []
    dimensions = (sx, sy, sz)
    center = (x, y, z)
    for axis in range(3):
        a, b = [i for i in range(3) if i != axis]
        na, nb = max(2, math.ceil(dimensions[a] / 0.034)), max(2, math.ceil(dimensions[b] / 0.034))
        for side in (-1, 1):
            for i in range(na + 1):
                for j in range(nb + 1):
                    p, n = list(center), [0.0] * 3
                    p[axis] += dimensions[axis] * side / 2
                    p[a] += dimensions[a] * (i / na - 0.5)
                    p[b] += dimensions[b] * (j / nb - 0.5)
                    n[axis] = side
                    points.append((*p, *n, 1.0))
    return points


def chair():
    parts = [box(0, 1.42, 0, 1.8, 0.17, 1.7)]
    for x in (-0.78, 0.78):
        parts += [box(x, 0.71, -0.67, 0.16, 1.42, 0.16), box(x, 1.65, 0.67, 0.17, 3.3, 0.17)]
    parts += [box(0, 3.12, 0.67, 1.75, 0.32, 0.19)]
    for x in (-0.42, 0.0, 0.42):
        parts.append(box(x, 2.32, 0.67, 0.13, 1.4, 0.14))
    parts += [box(0, 0.54, -0.67, 1.6, 0.1, 0.1), box(0, 0.54, 0.67, 1.6, 0.1, 0.1)]
    return parts


def ellipsoid(cx, cy, cz, rx, ry, rz, brightness, phase, steps=72):
    points = []
    for a in range(1, steps // 2):
        v = math.pi * a / (steps // 2)
        for b in range(steps):
            u = 2 * math.pi * b / steps
            noise = 1 + 0.055 * math.sin(u * 7 + phase) * math.sin(v * 9 + phase * 0.4)
            nx, ny, nz = math.sin(v) * math.cos(u), math.cos(v), math.sin(v) * math.sin(u)
            points.append((
                cx + rx * nx * noise,
                cy + ry * ny * noise,
                cz + rz * nz * noise,
                nx / rx,
                ny / ry,
                nz / rz,
                brightness,
            ))
    return points


def cloud(t):
    phase = t * 1.7
    if t < 1.1:
        r = 0.1 + t * 1.55
        return ellipsoid(0, 0.95 + t * 0.4, 0, r, r, r, 1.28, phase)
    growth = min(1, (t - 1.1) / 5)
    rise = 1.5 + 2.1 * growth + max(0, t - 6.1) * 0.05
    cap = 1.35 + 1.5 * growth
    points = ellipsoid(0, rise, 0, cap, 0.5 + 0.58 * growth, cap * 0.86, 1.0, phase)
    # Separate lobes wrap the cap rim and give the mushroom an unmistakable volume.
    for i in range(9):
        angle = i * math.tau / 9
        points += ellipsoid(
            math.cos(angle) * cap * 0.66,
            rise - 0.14,
            math.sin(angle) * cap * 0.58,
            cap * 0.36,
            0.43 + growth * 0.2,
            cap * 0.34,
            0.85,
            phase + i,
            32,
        )
    for i in range(9):
        y = 0.25 + i * (rise - 0.65) / 8
        radius = 0.25 + 0.18 * growth + 0.10 * math.sin(i * 1.9 + phase)
        points += ellipsoid(
            0.13 * math.sin(i * 0.7),
            y,
            0.06 * math.cos(i),
            radius,
            (rise - 0.65) / 8 + 0.11,
            radius,
            0.71,
            phase + i,
            24,
        )
    points += ellipsoid(0, 0.22, 0, 1.4 + growth * 1.2, 0.24, 1.4 + growth * 1.2, 0.47, phase, 48)
    return points


def render(index, parts):
    time = index * DURATION / FRAMES
    detonation = max(0, time - 3.6)
    angle = -0.67 + 0.17 * math.sin(time * 0.16)
    camera = (math.sin(angle) * 9.1, 4.4, -math.cos(angle) * 9.1)
    target = (0, 1.65 if time < 4.7 else 2.0, 0)
    forward = tuple(target[i] - camera[i] for i in range(3))
    length = math.sqrt(sum(v * v for v in forward))
    forward = tuple(v / length for v in forward)
    right = (forward[2], 0, -forward[0])
    length = math.sqrt(sum(v * v for v in right))
    right = tuple(v / length for v in right)
    up = (
        forward[1] * right[2] - forward[2] * right[1],
        forward[2] * right[0] - forward[0] * right[2],
        forward[0] * right[1] - forward[1] * right[0],
    )
    zoom = 1.9 - 0.75 * min(1, max(0, (time - 3.6) / 2.2))
    depth = [math.inf] * (COLS * ROWS)
    frame = [' '] * (COLS * ROWS)

    def draw(p, custom=None):
        x, y, z, nx, ny, nz, brightness = p
        relative = (x - camera[0], y - camera[1], z - camera[2])
        distance = sum(relative[i] * forward[i] for i in range(3))
        if distance <= 0.1:
            return
        px = sum(relative[i] * right[i] for i in range(3))
        py = sum(relative[i] * up[i] for i in range(3))
        col = round(COLS / 2 + 83 * zoom * px / distance)
        row = round(ROWS / 2 - 44 * zoom * py / distance)
        if not (0 <= col < COLS and 0 <= row < ROWS):
            return
        at = row * COLS + col
        if distance >= depth[at]:
            return
        depth[at] = distance
        if custom:
            frame[at] = custom
            return
        normal_length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
        light = max(0, (-nx * 0.4 + ny * 0.8 - nz * 0.6) / normal_length)
        shade = max(0.12, min(1, brightness * (0.19 + light * 0.81)))
        frame[at] = GLYPHS[max(1, min(len(GLYPHS) - 1, round(shade * (len(GLYPHS) - 1))))]

    # Deliberately sparse floor: perspective points carry the ground into depth.
    for a in range(-23, 24):
        for b in range(-18, 25):
            if (a + b) % 2 == 0:
                draw((a * 0.33, -0.03, b * 0.33, 0, 1, 0, 0.1), '.')
    if time <= 4.1:
        for part in parts:
            for p in part:
                draw(p)
    elif detonation < 4.8:
        for part_index, part in enumerate(parts):
            # The same 3D chair surfaces break apart; this is not a 2D dissolve.
            direction = part_index * 2.399
            displacement = (detonation - 0.5) ** 1.15 * 0.63
            for p in part[::4]:
                x, y, z, nx, ny, nz, brightness = p
                draw((
                    x + math.cos(direction) * displacement,
                    y + displacement * 0.64 - displacement * displacement * 0.16,
                    z + math.sin(direction) * displacement,
                    nx,
                    ny,
                    nz,
                    brightness * max(0.2, 1 - detonation / 6),
                ))
    if time >= 3.6:
        for p in cloud(detonation):
            draw(p)
        # Expanding toroidal ground shock front, sampled in real space.
        radius = 0.45 + detonation * 1.7
        if radius < 10.5:
            for i in range(960):
                u = math.tau * i / 960
                draw(
                    (math.cos(u) * radius, 0.08, math.sin(u) * radius, 0, 1, 0, 0.65),
                    ':' if detonation > 2 else '*',
                )
        rng = random.Random(781)
        for _ in range(190):
            u, speed = rng.uniform(0, math.tau), rng.uniform(0.3, 1.1)
            r = detonation * speed
            y = rng.uniform(0.5, 1.8) * detonation - detonation * detonation * 0.22
            if y > 0:
                draw((math.cos(u) * r, y, math.sin(u) * r, 0, 1, 0, 0.5), '+')
    return '\n'.join(''.join(frame[r * COLS : (r + 1) * COLS]).rstrip() for r in range(ROWS))


def build():
    parts = chair()
    frames = [render(i, parts) for i in range(FRAMES)]
    rules = [f'.f{i}{{--index:{i}}}' for i in range(FRAMES)]
    html = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>椅子之后 · lightjunction</title>
<style>
@property --frame{syntax:"<integer>";inherits:true;initial-value:0}
@keyframes scene-clock{from{--frame:0}to{--frame:64}}
:root{color-scheme:dark;background:#080808;color:#d4d4d4;font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}body{margin:0;min-height:100svh;display:grid;grid-template-rows:1fr auto;overflow-x:hidden}::selection{background:#d4d4d4;color:#080808}
.screen{position:relative;display:flex;align-items:center;justify-content:center;min-height:0;padding:24px 12px 0}.projection{position:relative;width:96ch;height:54em;line-height:1;font-size:clamp(5px,min(1.55vw,1.58vh),15px);letter-spacing:0;flex-shrink:0}
pre{font:inherit;line-height:1;margin:0;white-space:pre;position:absolute;inset:0;color:#d0d0d0;user-select:none}.frame{visibility:visible;opacity:calc(1 - max(var(--frame) - var(--index),var(--index) - var(--frame)))}.still{visibility:hidden}
.projection{animation:scene-clock 18s steps(64,end) infinite;animation-play-state:paused}body:has(#scene-ready) .projection{animation-play-state:running}
.accessibility{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
footer{display:flex;align-items:center;justify-content:space-between;gap:20px;margin:0 28px 22px;font-size:12px;line-height:1.6;color:#b5b5b5}h1{font:inherit;margin:0;color:#dedede}p{margin:2px 0 0;font-size:11px;color:#aaa}.controls{display:flex;align-items:center;gap:16px;flex-shrink:0}label,a{color:inherit;text-decoration:none;cursor:pointer;min-height:44px;display:inline-flex;align-items:center;gap:7px}a:hover{color:#fff;text-decoration:underline;text-underline-offset:4px}input{accent-color:#d4d4d4;width:14px;height:14px;margin:0}a:focus-visible,input:focus-visible{outline:2px solid #dedede;outline-offset:5px}
body:has(#pause:checked) .projection{animation-play-state:paused!important}
.motion-choice{display:none}
@media(max-width:600px){.screen{padding:12px 4px 0}.projection{font-size:min(1.67vw,1.58vh)}footer{margin:0 18px 16px;gap:12px;align-items:flex-start;flex-direction:column}.controls{gap:24px}p{max-width:32ch}}
@media(prefers-reduced-motion:reduce){.projection{animation:none!important}.frame{visibility:hidden}.still{visibility:visible}.motion-choice{display:inline-flex}body:has(#motion:checked) .projection{animation-name:scene-clock!important;animation-duration:18s!important;animation-timing-function:steps(64,end)!important;animation-iteration-count:infinite!important}body:has(#motion:checked) .frame{visibility:visible}body:has(#motion:checked) .still{visibility:hidden}}
__RULES__
</style></head><body>
<!-- THESIS: A recognisable chair becomes a spatial ASCII nuclear sculpture; the scene, not interface chrome, owns the viewport.
OWN-WORLD: Black stage, gray-white 7-bit glyphs, perspective floor and surface-lit volume, low-key native controls.
STORY: Look at a quiet chair for 3.6 seconds, then watch a fireball, ground shock ring, fragments and rising mushroom cloud over an 18-second loop.
FIRST VIEWPORT: One large three-quarter-view chair centered across a 96 by 54 character projection; title and pause/replay sit below the stage.
FORM: Code-led full-screen sculpture. Fixed user direction, no concept tournament; seed ascii-chair-cloud-781.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance -->
<main class="screen"><div class="projection" role="img" aria-label="立体字符动画：一把木椅，随后火球、冲击波与升起的蘑菇云。18秒循环。">
<pre class="still" aria-hidden="true">__STILL__</pre>
__FRAMES__
</div></main>
<footer id="scene-ready"><div><h1>椅子之后</h1><p>一把椅子。然后，世界换了一种形状。</p></div><div class="controls"><label class="motion-choice"><input type="checkbox" id="motion">播放动画</label><label><input type="checkbox" id="pause">暂停</label><a href="./">重播</a><a href="/&#64;lightjunction">&#64;lightjunction</a></div></footer>
</body></html>"""
    html = (
        html
        .replace('__RULES__', '\n'.join(rules))
        .replace('__STILL__', frames[0])
        .replace(
            '__FRAMES__',
            '\n'.join(
                f'<pre class="frame f{i}" aria-hidden="true">{frame}</pre>'
                for i, frame in enumerate(frames)
            ),
        )
    )
    target = Path(__file__).with_name('index.html')
    data = html.encode()
    assert len(data) < 400 * 1024
    assert all(ord(c) < 128 for f in frames for c in f)
    assert '<script' not in html and '<form' not in html
    target.write_bytes(data)
    print(f'{target}: {len(data)} bytes sha256={hashlib.sha256(data).hexdigest()}')


if __name__ == '__main__':
    build()
