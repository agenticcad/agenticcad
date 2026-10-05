"""Pattern and mirror helpers for design scripts (the ribbon's Pattern / Mirror write calls to these).

    pattern_linear(shape, (1, 0, 0), count=4, spacing=15)                      # a row of 4, 15 mm apart
    pattern_linear(shape, (1, 0, 0), 4, 15, direction2=(0, 1, 0), count2=3, spacing2=10)   # a 4 x 3 grid
    pattern_circular(shape, count=6)                                           # 6 around Z through the origin
    pattern_circular(shape, count=5, angle=90, axis=((10, 0, 0), (0, 0, 1)))   # 5 over 90° about an offset axis
    mirror_about(shape, origin=(0, 0, 0), normal=(1, 0, 0))                    # the mirror image only
    pipe(path_wire([edge, ...]), diameter=6, wall=1)                           # a tube along model edges

Counts include the original. The result is all copies fused into one solid (`separate=True` keeps them as a list of
shapes, e.g. to make each a body, or to subtract them all from a plate)."""
from __future__ import annotations

import math
from typing import Any

import build123d as b3d


def _axis(axis) -> b3d.Axis:
    if isinstance(axis, b3d.Axis):
        return axis
    if axis is None:
        return b3d.Axis.Z
    if isinstance(axis, str):
        return {"X": b3d.Axis.X, "Y": b3d.Axis.Y, "Z": b3d.Axis.Z}[axis.upper()]
    origin, direction = axis
    return b3d.Axis(tuple(origin), tuple(direction))


def _fuse(shapes: list) -> Any:
    out = shapes[0]
    for s in shapes[1:]:
        out = out + s
    return out


def pattern_linear(shape, direction=(1, 0, 0), count: int = 2, spacing: float = 10.0, direction2=None,
                   count2: int = 1, spacing2: float = 0.0, separate: bool = False, include_original: bool = True):
    """`count` copies `spacing` mm apart along `direction` (and optionally `count2` rows along `direction2`).
    include_original=False leaves out the first copy (the shape itself), e.g. for a new body of the copies."""
    if count < 1 or count2 < 1:
        raise ValueError("pattern_linear: counts must be >= 1")
    d1 = b3d.Vector(*direction).normalized()
    d2 = b3d.Vector(*direction2).normalized() if direction2 is not None else b3d.Vector(0, 0, 0)
    copies = [b3d.Pos(*(d1 * (spacing * i) + d2 * (spacing2 * j))) * shape
              for j in range(count2) for i in range(count)]
    if not include_original:
        copies = copies[1:]
    if not copies:
        raise ValueError("pattern_linear: nothing to pattern (count 1 without the original)")
    return copies if separate else _fuse(copies)


def pattern_circular(shape, count: int = 6, angle: float = 360.0, axis=None, separate: bool = False,
                     include_original: bool = True):
    """`count` copies spread over `angle` degrees about `axis` (Axis, 'X'/'Y'/'Z', or ((origin), (direction));
    default Z through the origin). A full 360° spaces them 360/count apart, a partial arc puts the last copy at
    `angle`."""
    if count < 1:
        raise ValueError("pattern_circular: count must be >= 1")
    ax = _axis(axis)
    step = angle / count if abs(angle - 360.0) < 1e-9 else (angle / (count - 1) if count > 1 else 0.0)
    copies = [shape.rotate(ax, step * i) if i else shape for i in range(count)]
    if not include_original:
        copies = copies[1:]
    if not copies:
        raise ValueError("pattern_circular: nothing to pattern (count 1 without the original)")
    return copies if separate else _fuse(copies)


def mirror_about(shape, origin=(0, 0, 0), normal=(1, 0, 0)):
    """The mirror image of `shape` in the plane through `origin` with `normal` (fuse it with the original to
    make a symmetric part)."""
    return b3d.mirror(shape, about=b3d.Plane(origin=tuple(origin), z_dir=tuple(normal)))


def path_wire(edges) -> b3d.Wire:
    """A sweep path from edges (any order; they must connect end to end)."""
    edges = list(edges)
    wires = b3d.Wire.combine(edges)
    if len(wires) != 1:
        raise ValueError(f"path_wire: the {len(edges)} edges form {len(wires)} separate chains; pick connected edges")
    return wires[0]


def pipe(path, diameter: float, wall: float | None = None):
    """A round tube along `path` (a Wire/Edge, e.g. from path_wire): solid rod, or hollow with `wall` thickness.
    Sharp corners are mitred."""
    w = path if isinstance(path, b3d.Wire) else b3d.Wire([path])
    plane = b3d.Plane(origin=w.position_at(0), z_dir=w.tangent_at(0))
    prof = plane * b3d.Circle(diameter / 2)
    if wall:
        if not 0 < wall < diameter / 2:
            raise ValueError("pipe: wall must be between 0 and the radius")
        prof = prof - plane * b3d.Circle(diameter / 2 - wall)
    return b3d.sweep(prof, path=w, transition=b3d.Transition.RIGHT)


def namespace() -> dict[str, Any]:
    return {"pattern_linear": pattern_linear, "pattern_circular": pattern_circular, "mirror_about": mirror_about,
            "path_wire": path_wire, "pipe": pipe}
