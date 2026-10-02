"""Deterministic world coordinates from an already authorized public graph.

Default Root attachments are not explicit cycles. Coordinates are static for a
given graph, independent of page order, rendering time, wealth or post loading.
"""

from __future__ import annotations

import hashlib
import math

LAYOUT_VERSION = 4
WORLD_BOUND = 400.0
TAU = math.tau


def _numbers(seed):
    raw = hashlib.sha256(seed.encode()).digest()
    return [int.from_bytes(raw[i : i + 4], 'big') / 2**32 for i in range(0, 32, 4)]


def _vector(seed, distance):
    a, b, *_ = _numbers(seed)
    angle, vertical = a * TAU, b * 2 - 1
    radial = math.sqrt(1 - vertical * vertical)
    return [
        math.cos(angle) * radial * distance,
        vertical * distance,
        math.sin(angle) * radial * distance,
    ]


def _rounded(point):
    return [round(value, 6) for value in point]


def _inside(point, bound=WORLD_BOUND):
    length = math.sqrt(sum(value * value for value in point))
    return [value * min(1, bound / max(length, 0.001)) for value in point]


def _separate_systems(result, components, root_component):
    """Place colliding systems rigidly; Root and within-system offsets stay fixed.

    The public graph is bounded to 256 nodes. Eight world units also cover the
    largest Root/user entity pair; post rings may cross each other.
    """
    placed = []
    order = sorted(range(len(components)), key=lambda index: (index != root_component, index))
    for index in order:
        members = components[index]
        original = result[members[0]]['orbit']['center']
        offsets = [
            [left - right for left, right in zip(result[rid]['position'], original, strict=True)]
            for rid in members
        ]

        def available(points):
            return all(math.dist(point, previous) >= 8 for point in points for previous in placed)

        points = [result[rid]['position'] for rid in members]
        if index != root_component and not available(points):
            extent = max(math.hypot(*offset) for offset in offsets)
            bound = max(0, WORLD_BOUND - extent - 0.000001)
            seed = 'separate:v4:' + '|'.join(members)
            # A deterministic rejection pass is independent of request/page
            # order and does not move one member away from its barycenter.
            for attempt in range(4096):
                probe = seed + ':' + str(attempt)
                distance = bound * (0.15 + _numbers(probe)[2] * 0.85)
                anchor = _vector(probe, distance)
                candidates = [
                    _rounded([left + right for left, right in zip(anchor, offset, strict=True)])
                    for offset in offsets
                ]
                if available(candidates):
                    points = candidates
                    for rid, point in zip(members, points, strict=True):
                        result[rid]['position'] = point
                        result[rid]['orbit']['center'] = _rounded(anchor)
                    break
            else:
                raise ValueError('bounded planet systems could not be separated')
        placed.extend(points)


def planet_layout(nodes, edges, *, root_id='u_root'):
    """Map visible IDs to layout facts; caller owns ACL and graph bounds.

    An SCC is a binary/multiple system. One stable explicit outbound component
    becomes its orbital parent; all other relationships stay in the graph.
    """
    ids = sorted(set(nodes))
    known = set(ids)
    links = {}
    for edge in edges:
        source, target = edge.get('source'), edge.get('target')
        if source in known and target in known and source != target:
            pair = source, target
            kind = edge.get('source_type', 'explicit')
            if links.get(pair) != 'explicit':
                links[pair] = kind
    adjacency, reverse = {rid: [] for rid in ids}, {rid: [] for rid in ids}
    for (source, target), kind in sorted(links.items()):
        if kind == 'explicit':
            adjacency[source].append(target)
            reverse[target].append(source)
    # Iterative Kosaraju is linear in the bounded graph and avoids recursion on
    # long follow chains in the same process as the live flight server.
    visited, order = set(), []
    for rid in ids:
        if rid in visited:
            continue
        visited.add(rid)
        pending = [(rid, 0)]
        while pending:
            node, offset = pending[-1]
            if offset == len(adjacency[node]):
                pending.pop()
                order.append(node)
                continue
            following = adjacency[node][offset]
            pending[-1] = node, offset + 1
            if following not in visited:
                visited.add(following)
                pending.append((following, 0))
    components, visited = [], set()
    for rid in reversed(order):
        if rid in visited:
            continue
        members, pending = [], [rid]
        visited.add(rid)
        while pending:
            node = pending.pop()
            members.append(node)
            for following in reverse[node]:
                if following not in visited:
                    visited.add(following)
                    pending.append(following)
        components.append(tuple(sorted(members)))
    components.sort()
    owners = {member: index for index, members in enumerate(components) for member in members}
    root_component = owners.get(root_id)
    parents, sources, extents = {}, {}, {}
    outgoing_components, default_components = {}, set()
    for (source, target), kind in links.items():
        if kind == 'explicit' and owners[source] != owners[target]:
            outgoing_components.setdefault(owners[source], set()).add(owners[target])
        elif kind == 'default' and target == root_id:
            default_components.add(owners[source])
    for index, members in enumerate(components):
        extents[index] = (
            0
            if len(members) == 1
            else 32
            if len(members) == 2
            else min(200, max(64, len(members) * 10))
        )
        if index == root_component:
            parents[index], sources[index] = None, None
            continue
        outgoing = sorted(outgoing_components.get(index, ()), key=lambda other: components[other])
        defaults = index in default_components
        parents[index] = outgoing[0] if outgoing else root_component if defaults else None
        sources[index] = 'explicit' if outgoing else 'default' if defaults else None
    centers = {}

    def center(index):
        if index in centers:
            return centers[index]
        # Follow SCC condensation is acyclic. Default edges only attach to Root,
        # whose position is fixed even if it explicitly follows another user.
        chain, current = [], index
        while current not in centers and current is not None:
            chain.append(current)
            current = parents[current]
        for item in reversed(chain):
            members = components[item]
            seed = 'planet:v4:' + '|'.join(members)
            if item == root_component:
                point = [0.0, 0.0, 0.0]
            elif parents[item] is None or sources[item] == 'default':
                point = _vector(seed, 110 + _numbers(seed)[2] * 170)
            else:
                displacement = _vector(
                    seed, 72 + _numbers(seed)[2] * 32 + extents[item] + extents[parents[item]]
                )
                point = [
                    left + right
                    for left, right in zip(centers[parents[item]], displacement, strict=True)
                ]
            centers[item] = _inside(point, max(120, WORLD_BOUND - extents[item]))
        return centers[index]

    result = {}
    for index, members in enumerate(components):
        anchor = center(index)
        seed = 'system:v4:' + '|'.join(members)
        values = _numbers(seed)
        kind = (
            'binary'
            if len(members) == 2
            else 'multi'
            if len(members) > 2
            else 'orbit'
            if parents[index] is not None
            else 'isolated'
        )
        group = (
            hashlib.sha256('|'.join(members).encode()).hexdigest()[:16]
            if len(members) > 1
            else None
        )
        offsets = []
        for slot in range(len(members)):
            phase = values[0] * TAU + slot * TAU / len(members)
            tilt = (values[1] - 0.5) * 1.6
            radius = extents[index]
            if len(members) > 2:
                height = 1 - 2 * (slot + 0.5) / len(members)
                radial = math.sqrt(1 - height * height)
                angle = values[0] * TAU + slot * math.pi * (3 - math.sqrt(5))
                offsets.append([
                    math.cos(angle) * radial * radius,
                    height * radius,
                    math.sin(angle) * radial * radius,
                ])
            else:
                offsets.append([
                    math.cos(phase) * radius,
                    math.sin(phase) * radius * math.sin(tilt),
                    math.sin(phase) * radius * math.cos(tilt),
                ])
        if len(members) > 2:
            average = [sum(offset[axis] for offset in offsets) / len(offsets) for axis in range(3)]
            offsets = [
                [left - right for left, right in zip(offset, average, strict=True)]
                for offset in offsets
            ]
            scale = extents[index] / max(math.hypot(*offset) for offset in offsets)
            offsets = [[value * scale for value in offset] for offset in offsets]
        display_center = anchor
        if root_id in members:
            root_offset = offsets[members.index(root_id)]
            display_center = [left - right for left, right in zip(anchor, root_offset, strict=True)]
        for slot, rid in enumerate(members):
            phase = values[0] * TAU + slot * TAU / len(members)
            tilt = (values[1] - 0.5) * 1.6
            radius = extents[index]
            position = [
                left + right for left, right in zip(display_center, offsets[slot], strict=True)
            ]
            parent = parents[index]
            result[rid] = {
                'version': LAYOUT_VERSION,
                'position': _rounded(_inside(position)),
                'orbit': {
                    'kind': 'root' if rid == root_id else kind,
                    'center': _rounded(display_center),
                    'parent_id': root_id
                    if parent == root_component and parent is not None
                    else components[parent][0]
                    if parent is not None
                    else None,
                    'group_id': group,
                    'members': list(members) if len(members) > 1 else [],
                    'radius': round(math.hypot(*offsets[slot]), 6)
                    if radius
                    else math.dist(anchor, centers[parent])
                    if parent is not None
                    else 0,
                    'phase': round(phase, 6),
                    'tilt': round(tilt, 6),
                    'period': round(180 + values[3] * 180, 6),
                    'source_type': sources[index],
                },
            }
    _separate_systems(result, components, root_component)
    for value in result.values():
        orbit = value['orbit']
        if orbit['kind'] == 'orbit':
            orbit['center'] = result[orbit['parent_id']]['position']
            orbit['radius'] = round(math.dist(value['position'], orbit['center']), 6)
    return result


def fallback_position(subject_id):
    """A readable identity outside the bounded graph gets an ID-only point."""
    if subject_id == 'u_root':
        return [0.0, 0.0, 0.0]
    return planet_layout(
        ['u_root', subject_id],
        [{'source': subject_id, 'target': 'u_root', 'source_type': 'default'}],
    )[subject_id]['position']
