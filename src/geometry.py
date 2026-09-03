"""STEP -> 2D plywood rectangles.

The trap this module exists to avoid: the world-frame axis-aligned bounding box of a
panel that is rotated inside the assembly is NOT its 2D footprint. For each solid we
must find the sheet plane, build a local frame from its normal, and measure the
minimum-area bounding rectangle of the outline projected into that frame.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from OCP.BRep import BRep_Tool
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GProp import GProp_GProps
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS


@dataclass(frozen=True)
class RawPart:
    """One solid, measured in its own sheet plane. Units: inches."""
    index: int
    thickness: float
    width: float          # short side of the min-area rect
    length: float         # long side
    face_area: float      # true area of the sheet face (< w*l for non-rectangles)

    @property
    def fill(self) -> float:
        """Fraction of the bounding rect the part actually occupies."""
        return self.face_area / (self.width * self.length)


def _solids(shape):
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        yield TopoDS.Solid_s(exp.Current())
        exp.Next()


def _faces(shape):
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        yield TopoDS.Face_s(exp.Current())
        exp.Next()


def _face_area(face) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return props.Mass()


def _mesh_points(solid, deflection: float):
    """Triangulation nodes in world coordinates.

    Uses the mesh rather than vertices so that curved edges (rounded corners, arcs)
    contribute their true extremes to the bounding rectangle.
    """
    BRepMesh_IncrementalMesh(solid, deflection, False, 0.5, True)
    pts = []
    for face in _faces(solid):
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            continue
        trsf = loc.Transformation()
        for i in range(1, tri.NbNodes() + 1):
            p = tri.Node(i).Transformed(trsf)
            pts.append((p.X(), p.Y(), p.Z()))
    return pts


def _sheet_normal(solid):
    """Normal of the dominant planar face pair = the thickness direction.

    Planar faces are bucketed by direction (sign-insensitive, since the two big faces
    of a panel have opposite normals) and the bucket with the most area wins.
    """
    buckets: dict[tuple, list] = {}
    for face in _faces(solid):
        surf = BRep_Tool.Surface_s(face)
        if surf.DynamicType().Name() != "Geom_Plane":
            continue
        n = surf.Pln().Axis().Direction()
        v = (n.X(), n.Y(), n.Z())
        if v[0] < 0 or (v[0] == 0 and (v[1] < 0 or (v[1] == 0 and v[2] < 0))):
            v = (-v[0], -v[1], -v[2])
        key = tuple(round(c, 4) for c in v)
        buckets.setdefault(key, []).append(_face_area(face))
    if not buckets:
        return None
    best = max(buckets.items(), key=lambda kv: sum(kv[1]))
    return best[0], sum(best[1])


def _frame(n):
    """Orthonormal frame with n as the third axis."""
    nx, ny, nz = n
    mag = math.sqrt(nx * nx + ny * ny + nz * nz)
    n = (nx / mag, ny / mag, nz / mag)
    helper = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = (
        helper[1] * n[2] - helper[2] * n[1],
        helper[2] * n[0] - helper[0] * n[2],
        helper[0] * n[1] - helper[1] * n[0],
    )
    m = math.sqrt(sum(c * c for c in u))
    u = tuple(c / m for c in u)
    v = (
        n[1] * u[2] - n[2] * u[1],
        n[2] * u[0] - n[0] * u[2],
        n[0] * u[1] - n[1] * u[0],
    )
    return u, v, n


def _hull(pts):
    """Andrew's monotone chain convex hull."""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2:
                (x1, y1), (x2, y2) = out[-2], out[-1]
                if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) > 0:
                    break
                out.pop()
            out.append(p)
        return out

    return half(pts)[:-1] + half(reversed(pts))[:-1]


def _min_area_rect(pts):
    """Rotating calipers. Returns (short_side, long_side)."""
    hull = _hull(pts)
    if len(hull) < 3:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        a, b = max(xs) - min(xs), max(ys) - min(ys)
        return (min(a, b), max(a, b))
    best = None
    for i in range(len(hull)):
        x1, y1 = hull[i]
        x2, y2 = hull[(i + 1) % len(hull)]
        ex, ey = x2 - x1, y2 - y1
        m = math.hypot(ex, ey)
        if m < 1e-12:
            continue
        ex, ey = ex / m, ey / m
        # project onto edge direction and its perpendicular
        along = [(p[0] - x1) * ex + (p[1] - y1) * ey for p in hull]
        perp = [-(p[0] - x1) * ey + (p[1] - y1) * ex for p in hull]
        w = max(along) - min(along)
        h = max(perp) - min(perp)
        if best is None or w * h < best[0] * best[1]:
            best = (w, h)
    return (min(best), max(best))


class UnmeasurableSolid(RuntimeError):
    """A solid could not be measured as a flat panel."""


def parts_from_shape(shape, unit_scale: float | None = None,
                     strict: bool = True) -> list[RawPart]:
    """Measure every solid in a shape in its own sheet plane.

    unit_scale converts model units to inches. If None it is inferred by assuming the
    largest part must fit a 4x8 sheet.

    strict=True refuses to return a short list. Dropping a solid quietly is the worst
    failure mode available here: the layout still validates and still scores, and it
    scores BETTER for having one fewer part to cut. You would cut 200 of 201 panels and
    find out at assembly.
    """
    raw = []
    skipped = []
    for idx, solid in enumerate(_solids(shape)):
        sn = _sheet_normal(solid)
        if sn is None:
            skipped.append((idx, "no planar faces: not a flat panel"))
            continue
        normal, _ = sn
        u, v, n = _frame(normal)
        pts3 = _mesh_points(solid, 1.0)
        if not pts3:
            skipped.append((idx, "could not be triangulated"))
            continue
        pts2 = [(p[0] * u[0] + p[1] * u[1] + p[2] * u[2],
                 p[0] * v[0] + p[1] * v[1] + p[2] * v[2]) for p in pts3]
        along_n = [p[0] * n[0] + p[1] * n[1] + p[2] * n[2] for p in pts3]
        thickness = max(along_n) - min(along_n)
        w, l = _min_area_rect(pts2)
        # sheet face area = half the total surface minus edges; use the dominant bucket
        _, dom_area = sn
        raw.append((idx, thickness, w, l, dom_area / 2.0))

    if skipped:
        lines = [f"  solid {i}: {why}" for i, why in skipped]
        msg = (f"{len(skipped)} of {len(raw) + len(skipped)} solids could not be "
               f"measured as flat panels:\n" + "\n".join(lines))
        if strict:
            raise UnmeasurableSolid(
                msg + "\n\nPass strict=False to skip them deliberately.")
        print("WARNING: " + msg)

    if not raw:
        raise UnmeasurableSolid("no measurable solids found")

    if unit_scale is None:
        biggest = max(max(r[2], r[3]) for r in raw)
        # candidate: model is in mm (96in = 2438mm) or inches
        unit_scale = 1.0 / 25.4 if biggest > 200 else 1.0

    return [
        RawPart(idx, t * unit_scale, w * unit_scale, l * unit_scale,
                a * unit_scale * unit_scale)
        for idx, t, w, l, a in raw
    ]


def extract_parts(step_path: str, unit_scale: float | None = None,
                  strict: bool = True) -> list[RawPart]:
    """Read a STEP assembly and measure every solid in its own sheet plane."""
    reader = STEPControl_Reader()
    if reader.ReadFile(step_path) != 1:
        raise RuntimeError(f"could not read {step_path}")
    reader.TransferRoots()
    return parts_from_shape(reader.OneShape(), unit_scale, strict)
