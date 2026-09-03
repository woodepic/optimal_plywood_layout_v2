"""Geometry tests on synthetic solids.

geometry.py is the highest-risk module in the project and had no coverage at all,
because the only STEP file is gitignored so the suite could never reach it. These build
panels in OCP directly, so they run anywhere.

The important one is the rotated panel. The trap the plan called out from the start is
that the world-frame axis-aligned bounding box of a panel rotated inside an assembly is
NOT its footprint. Get that wrong and every part silently inflates, the sheet count
silently rises, and it presents as an optimizer bug.
"""
import math
import sys

sys.path.insert(0, ".")

from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.TopoDS import TopoDS_Compound

from src.geometry import UnmeasurableSolid, parts_from_shape

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def _panel(t, w, l, rotate_deg=0.0, axis=(1.0, 1.0, 1.0), move=(0.0, 0.0, 0.0)):
    shape = BRepPrimAPI_MakeBox(t, w, l).Shape()
    trsf = gp_Trsf()
    if rotate_deg:
        ax = gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(*axis))
        trsf.SetRotation(ax, math.radians(rotate_deg))
    if any(move):
        t2 = gp_Trsf()
        t2.SetTranslation(gp_Vec(*move))
        trsf = t2.Multiplied(trsf)
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def _compound(shapes):
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    for s in shapes:
        b.Add(comp, s)
    return comp


def _aabb(shape):
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, True)
    x1, y1, z1, x2, y2, z2 = box.Get()
    return sorted([x2 - x1, y2 - y1, z2 - z1])


def test_axis_aligned_panel():
    parts = parts_from_shape(_panel(0.75, 24, 30), unit_scale=1.0)
    check(len(parts) == 1, f"expected 1 part, got {len(parts)}")
    p = parts[0]
    check(abs(p.thickness - 0.75) < 1e-6, f"thickness {p.thickness}, want 0.75")
    check(abs(p.width - 24) < 1e-4, f"width {p.width}, want 24")
    check(abs(p.length - 30) < 1e-4, f"length {p.length}, want 30")
    check(abs(p.fill - 1.0) < 1e-3, f"fill {p.fill}, want 1.0 for a rectangle")


def test_rotated_panel_is_not_measured_by_its_world_bounding_box():
    """The whole reason geometry.py builds a local frame."""
    shape = _panel(0.75, 24, 30, rotate_deg=37.0, axis=(0.3, 1.0, 0.7),
                   move=(5.0, -3.0, 11.0))

    # first establish that the naive measurement really would be wrong
    naive = _aabb(shape)
    check(naive[0] > 2.0,
          f"world AABB thickness is {naive[0]:.3f}: rotation did not skew it, so this "
          f"test proves nothing -- pick a different axis")

    parts = parts_from_shape(shape, unit_scale=1.0)
    check(len(parts) == 1, f"expected 1 part, got {len(parts)}")
    p = parts[0]
    check(abs(p.thickness - 0.75) < 1e-3,
          f"thickness {p.thickness:.4f}, want 0.75 (world AABB would say "
          f"{naive[0]:.3f})")
    check(abs(p.width - 24) < 1e-3,
          f"width {p.width:.4f}, want 24 (world AABB would say {naive[1]:.3f})")
    check(abs(p.length - 30) < 1e-3,
          f"length {p.length:.4f}, want 30 (world AABB would say {naive[2]:.3f})")


def test_thickness_axis_found_regardless_of_which_dimension_is_thin():
    """The thin direction must be identified, not assumed to be X."""
    for dims in ((0.5, 20, 36), (20, 0.5, 36), (20, 36, 0.5)):
        parts = parts_from_shape(_panel(*dims), unit_scale=1.0)
        p = parts[0]
        check(abs(p.thickness - 0.5) < 1e-6,
              f"box {dims}: thickness {p.thickness}, want 0.5")
        check(abs(p.width - 20) < 1e-4 and abs(p.length - 36) < 1e-4,
              f"box {dims}: got {p.width}x{p.length}, want 20x36")


def test_many_panels_and_none_dropped():
    shapes = [_panel(0.75, 12 + i, 20 + 2 * i, rotate_deg=13.0 * i,
                     axis=(1.0, 0.5 + i, 0.25))
              for i in range(6)]
    parts = parts_from_shape(_compound(shapes), unit_scale=1.0)
    check(len(parts) == 6, f"expected 6 parts, got {len(parts)} -- a solid was dropped")
    for i, p in enumerate(sorted(parts, key=lambda q: q.width)):
        check(abs(p.width - (12 + i)) < 1e-3,
              f"panel {i}: width {p.width:.4f}, want {12 + i}")


def test_millimetre_model_is_detected():
    """96in = 2438mm, so a metric model must be recognised and converted."""
    parts = parts_from_shape(_panel(19.05, 600, 2400))     # mm
    p = parts[0]
    check(abs(p.thickness - 0.75) < 1e-3, f"thickness {p.thickness:.4f}, want ~0.75")
    check(abs(p.length - 2400 / 25.4) < 1e-2,
          f"length {p.length:.3f}, want {2400 / 25.4:.3f}")


def test_unmeasurable_solid_is_loud_not_silent():
    """A sphere is not a flat panel. Dropping it quietly is the dangerous outcome."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere
    sphere = BRepPrimAPI_MakeSphere(10.0).Shape()
    mixed = _compound([_panel(0.75, 24, 30), sphere])
    try:
        parts_from_shape(mixed, unit_scale=1.0)
        check(False, "a sphere was accepted as a flat panel, or silently dropped")
    except UnmeasurableSolid as e:
        check("could not be measured" in str(e),
              f"unexpected message: {e}")
    # and it must be skippable on purpose
    parts = parts_from_shape(mixed, unit_scale=1.0, strict=False)
    check(len(parts) == 1, f"strict=False should return the 1 real panel, got {len(parts)}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            n0 = len(FAILS)
            try:
                fn()
            except Exception as e:
                FAILS.append(f"{name} raised {type(e).__name__}: {e}")
            print(f"{'PASS' if len(FAILS) == n0 else 'FAIL'}  {name}")
    if FAILS:
        print(f"\n{len(FAILS)} failure(s):")
        for f in FAILS[:20]:
            print("  -", f)
        sys.exit(1)
    print("\nall geometry checks passed")
