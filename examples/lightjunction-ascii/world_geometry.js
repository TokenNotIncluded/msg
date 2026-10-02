// Static meshes only. The caller owns terrain, movement, doors and survival state.
function createShelterWorld({ box, sphere, limb }) {
  const entrance = [], stairs = [], chamber = [], door = [], ruins = [];
  const halfWidth = 0.85, mouthZ = -2, doorZ = -5.05;
  const floorY = -2.6, ceilingY = -0.25, stepCount = 12;
  const tread = (mouthZ - doorZ) / stepCount, rise = -floorY / stepCount;

  // A rectangular surface, with samples no farther apart than 0.12 world units.
  // a is the corner; b and c end its two perpendicular edges.
  function plane(out, a, b, c, normal, shade, spacing = 0.12) {
    const u = b.map((n, i) => n - a[i]);
    const v = c.map((n, i) => n - a[i]);
    const nu = Math.max(1, Math.ceil(Math.hypot(...u) / spacing));
    const nv = Math.max(1, Math.ceil(Math.hypot(...v) / spacing));
    for (let i = 0; i <= nu; i++) {
      for (let j = 0; j <= nv; j++) {
        out.push([
          ...a.map((n, axis) => n + u[axis] * i / nu + v[axis] * j / nv),
          ...normal, shade,
        ]);
      }
    }
  }

  // Small solid rails use the same coarse sampler, not the dense box helper.
  function slab(out, x0, x1, y0, y1, z0, z1, shade) {
    plane(out, [x0, y0, z0], [x0, y1, z0], [x0, y0, z1], [-1, 0, 0], shade);
    plane(out, [x1, y0, z0], [x1, y1, z0], [x1, y0, z1], [1, 0, 0], shade);
    plane(out, [x0, y0, z0], [x1, y0, z0], [x0, y0, z1], [0, -1, 0], shade);
    plane(out, [x0, y1, z0], [x1, y1, z0], [x0, y1, z1], [0, 1, 0], shade);
    plane(out, [x0, y0, z0], [x1, y0, z0], [x0, y1, z0], [0, 0, -1], shade);
    plane(out, [x0, y0, z1], [x1, y0, z1], [x0, y1, z1], [0, 0, 1], shade);
  }

  // A three-sided ground collar leaves the mouth facing the chair unobstructed.
  slab(entrance, -1.02, -halfWidth, 0, 0.22, doorZ - 0.12, mouthZ, 0.74);
  slab(entrance, halfWidth, 1.02, 0, 0.22, doorZ - 0.12, mouthZ, 0.74);
  slab(entrance, -1.02, 1.02, 0, 0.22, doorZ - 0.12, doorZ, 0.68);

  // Thin upright jambs and a lintel identify the open stair entrance.
  slab(entrance, -1.02, -halfWidth, 0, 2.35, mouthZ - 0.08, mouthZ + 0.08, 0.89);
  slab(entrance, halfWidth, 1.02, 0, 2.35, mouthZ - 0.08, mouthZ + 0.08, 0.89);
  slab(entrance, -1.02, 1.02, 2.22, 2.35, mouthZ - 0.08, mouthZ + 0.08, 0.89);

  for (let i = 0; i < stepCount; i++) {
    const front = mouthZ - i * tread, back = mouthZ - (i + 1) * tread;
    const upper = -i * rise, lower = -(i + 1) * rise;
    // Each leading riser drops onto one tread; the last tread meets room floor.
    plane(stairs, [-halfWidth, lower, front], [halfWidth, lower, front],
      [-halfWidth, lower, back], [0, 1, 0], 0.67);
    plane(stairs, [-halfWidth, lower, front], [halfWidth, lower, front],
      [-halfWidth, upper, front], [0, 0, 1], 0.77);
    // Inward-facing stepped retaining walls stay within the stair corridor.
    plane(stairs, [-halfWidth, lower, front], [-halfWidth, 0.22, front],
      [-halfWidth, lower, back], [1, 0, 0], 0.62);
    plane(stairs, [halfWidth, lower, front], [halfWidth, 0.22, front],
      [halfWidth, lower, back], [-1, 0, 0], 0.62);
  }

  const left = -2.6, right = 2.6, farZ = -9.6;
  // Room shell uses one inward-facing surface per wall, floor and ceiling.
  plane(chamber, [left, floorY, doorZ], [right, floorY, doorZ],
    [left, floorY, farZ], [0, 1, 0], 0.58);
  plane(chamber, [left, ceilingY, doorZ], [right, ceilingY, doorZ],
    [left, ceilingY, farZ], [0, -1, 0], 0.54);
  plane(chamber, [left, floorY, doorZ], [left, ceilingY, doorZ],
    [left, floorY, farZ], [1, 0, 0], 0.72);
  plane(chamber, [right, floorY, doorZ], [right, ceilingY, doorZ],
    [right, floorY, farZ], [-1, 0, 0], 0.72);
  plane(chamber, [left, floorY, farZ], [right, floorY, farZ],
    [left, ceilingY, farZ], [0, 0, 1], 0.79);
  // The near wall keeps a 1.70-wide opening, down to the chamber floor.
  plane(chamber, [left, floorY, doorZ], [-halfWidth, floorY, doorZ],
    [left, ceilingY, doorZ], [0, 0, -1], 0.74);
  plane(chamber, [halfWidth, floorY, doorZ], [right, floorY, doorZ],
    [halfWidth, ceilingY, doorZ], [0, 0, -1], 0.74);
  plane(chamber, [-halfWidth, -0.35, doorZ], [halfWidth, -0.35, doorZ],
    [-halfWidth, ceilingY, doorZ], [0, 0, -1], 0.82);

  // Closed door: rotate points AND normals around (-0.85, any y, -5.05) outside.
  door.push(...box(0, (floorY - 0.35) / 2, doorZ, 1.7, 2.25, 0.10, 0.96));
  // A small handle remains part of the door mesh and follows the same hinge.
  door.push(...box(0.55, -1.4, doorZ + 0.085, 0.16, 0.04, 0.06, 1));

  function tilted(points, x, z, yaw, roll, height = 0.19) {
    const cy = Math.cos(yaw), sy = Math.sin(yaw);
    const cr = Math.cos(roll), sr = Math.sin(roll);
    for (const p of points) {
      const rotate = (a, b, c) => {
        const rx = a * cr - b * sr, ry = a * sr + b * cr;
        return [rx * cy + c * sy, ry, -rx * sy + c * cy];
      };
      const position = rotate(p[0], p[1], p[2]);
      const normal = rotate(p[3], p[4], p[5]);
      ruins.push([position[0] + x, position[1] + height, position[2] + z, ...normal, p[6]]);
    }
  }

  function coarseBox(width, height, thickness, shade) {
    const points = [];
    slab(points, -width / 2, width / 2, -height / 2, height / 2,
      -thickness / 2, thickness / 2, shade);
    return points;
  }

  // A thick wall with a broken top, rather than a complete upright rectangle.
  // Both faces and the exposed end/top surfaces retain real world depth.
  function brokenWall(x, z, width, heights, yaw) {
    const points = [], thickness = 0.18, spacing = 0.12;
    const columns = Math.ceil(width / spacing);
    for (let i = 0; i <= columns; i++) {
      const u = i / columns, edge = u * (heights.length - 1);
      const segment = Math.min(heights.length - 2, Math.floor(edge));
      const top = heights[segment] + (heights[segment + 1] - heights[segment]) * (edge - segment);
      const rows = Math.ceil(top / spacing), px = width * (u - 0.5);
      for (let j = 0; j <= rows; j++) {
        const y = top * j / rows;
        points.push([px, y, -thickness / 2, 0, 0, -1, 1.08]);
        points.push([px, y, thickness / 2, 0, 0, 1, 0.88]);
      }
    }
    for (let i = 0; i < heights.length - 1; i++) {
      const a = width * (i / (heights.length - 1) - 0.5);
      const b = width * ((i + 1) / (heights.length - 1) - 0.5);
      const dy = heights[i + 1] - heights[i], dx = b - a, n = Math.hypot(dx, dy);
      plane(points, [a, heights[i], -thickness / 2], [b, heights[i + 1], -thickness / 2],
        [a, heights[i], thickness / 2], [-dy / n, dx / n, 0], 1.02);
    }
    for (const side of [-1, 1]) {
      const px = side * width / 2, top = side < 0 ? heights[0] : heights.at(-1);
      plane(points, [px, 0, -thickness / 2], [px, top, -thickness / 2],
        [px, 0, thickness / 2], [side, 0, 0], 0.94);
    }
    tilted(points, x, z, yaw, 0, 0);
  }

  // Near-left, middle-right and far-centre silhouettes share the crater field.
  // Their bases sit outside crater depressions; the exit lane remains empty.
  brokenWall(-2.05, 4.9, 1.45, [1.7, 2.55, 1.95, 1.2], 0.26);
  brokenWall(3.15, 11.7, 1.60, [2.1, 3.1, 2.7, 1.8], -0.30);
  brokenWall(-0.1, 24.8, 2.30, [3.0, 2.6, 3.6, 2.2], 0.18);
  tilted(coarseBox(2.2, 0.16, 0.20, 1.08), -1.9, 5.0, -0.16, 0.65, 0.76);
  tilted(coarseBox(2.6, 0.16, 0.20, 0.96), 3.7, 12.4, 0.22, -0.72, 0.95);
  tilted(coarseBox(3.5, 0.16, 0.20, 1.02), -0.1, 24.4, -0.18, 0.66, 1.18);

  // Small broken chair pieces remain nearby, also using the coarse sampler.
  tilted(coarseBox(0.76, 0.10, 0.58, 0.84), 3.1, 1.6, 0.65, 0.12);
  tilted(coarseBox(0.20, 0.09, 0.82, 0.79), 3.4, 2.2, -0.52, -0.12);
  tilted(coarseBox(0.53, 0.08, 0.17, 0.91), -3.0, 0.7, 0.35, 0.13);
  ruins.push(...limb([-3.40, 0.1, 0.42], [-2.78, 0.13, 1.06], 0.06, 0.82));
  const rubble = [
    [-2.7, 4.3], [-1.1, 5.9], [-3.3, 7.0], [0.3, 8.8],
    [0.8, 10.0], [2.6, 12.7], [4.5, 13.3], [0.7, 13.4],
    [-1.0, 14.4], [5.0, 15.6], [-5.6, 16.2], [-3.2, 20.6],
    [-1.2, 23.8], [1.0, 25.8], [3.1, 27.8], [-4.0, 29.5],
  ];
  for (let i = 0; i < rubble.length; i++) {
    const [x, z] = rubble[i], ry = 0.22 + (i % 3) * 0.07;
    ruins.push(...sphere(x, ry, z, 0.26 + (i % 4) * 0.055, ry,
      0.28 + (i % 3) * 0.06, 0.88 + (i % 4) * 0.035, 10, 6));
  }

  return { entrance, stairs, chamber, door, ruins };
}
