"""Independent audit of a saved layout against the STEP file it came from.

Everything in validate.py is self-referential. It confirms a layout is internally
consistent with the demand list -- but that demand list was produced by geometry.py in
the same process. If geometry shrank every part (a unit-scale slip, a wrong local
frame, a broken calipers pass), demand and layout would agree perfectly, every test
would pass, the validator would be satisfied, and the score would come out BETTER
because the parts are smaller.

So this goes back to the STEP file and cross-checks the measurements against
quantities computed by a different code path: OpenCASCADE's own volume and surface
integration, which shares nothing with the rotating-calipers bounding rectangle.

Two checks carry the weight:

  fill = face_area / (width x length) must be <= 1
      face_area is integrated by OCC over the panel's faces; width and length come
      from my calipers. A part cannot occupy more area than its own bounding
      rectangle, so fill > 1 is proof the rectangle came out too small. This is the
      direct catch for silent shrinking.

  volume <= thickness x width x length
      volume is integrated by OCC; all three dimensions come from my calipers and
      frame. A solid cannot contain more material than its own bounding box, so
      exceeding it proves at least one dimension came out too small. This catches
      shrinkage in any of the three axes at once.

Both are one-sided inequalities that hold for ANY shape, not equalities that assume a
perfect prism -- cabinet parts have dados and grooves, and up to 4.3% of the material
in this assembly is legitimately machined away. An earlier version of this file
asserted volume == thickness x face_area and produced 60 false alarms for exactly that
reason.

Both are ratios, so neither depends on the unit-scale inference being right.
"""
from __future__ import annotations

import argparse
import pickle
import sys
from collections import Counter

from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.STEPControl import STEPControl_Reader

from src.geometry import _solids, extract_parts
from src.model import CutConfig, fmt, load_config, to_inches, to_units
from src.parts import load_demand
from src.validate import check_job

TOL = 1.001               # 0.1%: tight enough to catch real shrinkage, loose enough
                          # for floating-point noise in OCC's integrators
LOW_FILL = 0.60           # below this a solid probably is not a plain panel


def solid_volumes(step_path: str) -> list[float]:
    """True volume per solid, integrated by OCC. Independent of geometry.py."""
    reader = STEPControl_Reader()
    if reader.ReadFile(step_path) != 1:
        raise RuntimeError(f"could not read {step_path}")
    reader.TransferRoots()
    out = []
    for solid in _solids(reader.OneShape()):
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(solid, props)
        out.append(props.Mass())
    return out


def audit(raw, vols, patterns, demand, cfg, raw_in=None):
    """Cross-check measurements and layout. Returns (problems, notes, stats).

    `raw` is measured in MODEL units, which keeps the area and volume checks scale-free
    -- they are ratios. `raw_in` is the same measurement converted to INCHES, needed by
    the rounding and thickness checks because the grid and the stock list are both in
    inches. Defaults to `raw` for callers whose model units already are inches.

    Pure function over already-gathered inputs so it can be tested with deliberately
    corrupted data -- an audit that has never been seen to fail proves nothing.
    """
    raw_in = raw if raw_in is None else raw_in
    problems: list[str] = []
    notes: list[str] = []

    produced_total = sum(len(s.placements) for p in patterns for s in p.strips)
    if len(raw) != len(vols):
        problems.append(f"{len(vols) - len(raw)} solids were not measured as panels")
    if produced_total != len(vols):
        problems.append(
            f"layout has {produced_total} parts but the file has {len(vols)} solids "
            f"({len(vols) - produced_total:+d})")

    worst_area = worst_vol = 0.0
    lowest_vol = 1.0
    for p in raw:
        bbox = p.width * p.length
        area_ratio = p.face_area / bbox if bbox else float("inf")
        worst_area = max(worst_area, area_ratio)
        if area_ratio > TOL:
            problems.append(
                f"solid {p.index}: face area {p.face_area:.1f} exceeds its own "
                f"{p.width:.3f}x{p.length:.3f} bounding rectangle "
                f"(ratio {area_ratio:.4f}) -- the rectangle came out SMALLER than "
                f"the part")
        if p.index < len(vols) and vols[p.index] > 0:
            prism = p.thickness * p.width * p.length
            vol_ratio = vols[p.index] / prism if prism else float("inf")
            worst_vol = max(worst_vol, vol_ratio)
            lowest_vol = min(lowest_vol, vol_ratio)
            if vol_ratio > TOL:
                problems.append(
                    f"solid {p.index}: volume {vols[p.index]:.1f} exceeds its own "
                    f"{p.thickness:.3f}x{p.width:.3f}x{p.length:.3f} bounding box "
                    f"(ratio {vol_ratio:.4f}) -- at least one dimension is too small")
            elif vol_ratio < LOW_FILL:
                notes.append(
                    f"solid {p.index} fills only {vol_ratio:.1%} of its bounding "
                    f"box -- heavily machined, or not a plain panel")

    want: Counter = Counter()
    for pt in demand:
        want[(pt.w, pt.l, pt.thickness)] += pt.qty

    # Grid rounding must never shrink a part, and the demand list must be exactly the
    # rounded measurements. Without this the audit compares rounded demand against a
    # rounded layout and a rounding-direction bug would pass unnoticed: 112 of the 201
    # parts in this assembly are off the 1/32" grid, so the rounding step is doing real
    # work on more than half of them.
    from_raw: Counter = Counter()
    for p in raw_in:
        w, l = to_units(p.width), to_units(p.length)
        if to_inches(w) < p.width - 1e-9 or to_inches(l) < p.length - 1e-9:
            problems.append(
                f'solid {p.index}: measured {p.width:.5f}x{p.length:.5f}" but rounds '
                f'to {to_inches(w):.5f}x{to_inches(l):.5f}" -- rounding SHRANK the '
                f'part; it must only ever round up')
        if w > l:
            w, l = l, w
        try:
            from_raw[(w, l, cfg.snap_thickness(p.thickness))] += 1
        except ValueError as e:
            problems.append(f"solid {p.index}: {e}")
    if from_raw and from_raw != want:
        only_raw = {k: v for k, v in from_raw.items() if want.get(k, 0) != v}
        problems.append(
            f"the demand list does not match the rounded measurements: "
            f"{len(only_raw)} type(s) differ, e.g. " + ", ".join(
                f'{fmt(k[0])}x{fmt(k[1])} @{k[2]}" measured {v} vs demand '
                f'{want.get(k, 0)}' for k, v in list(sorted(only_raw.items()))[:3]))
    got: Counter = Counter()
    for pat in patterns:
        for s in pat.strips:
            for pl in s.placements:
                w, l = sorted((pl.width, pl.length))
                got[(w, l, pat.thickness)] += 1

    for k in sorted(want):
        if got.get(k, 0) < want[k]:
            problems.append(f'{fmt(k[0])}x{fmt(k[1])} @{k[2]}": need {want[k]}, '
                            f'layout has {got.get(k, 0)}')
    for k in sorted(got):
        nw = want.get(k, 0)
        if got[k] > nw:
            if nw == 0:
                problems.append(
                    f'{fmt(k[0])}x{fmt(k[1])} @{k[2]}": layout has {got[k]} of a size '
                    f'that is not in the STEP file at all')
            else:
                notes.append(f'{fmt(k[0])}x{fmt(k[1])} @{k[2]}": {got[k] - nw} surplus '
                             f'(overproduction is legal but costs material)')

    try:
        info = check_job(patterns, demand, cfg)
        for pt, n in info["surplus"].items():
            notes.append(f'{pt.label} @{pt.thickness}": {n} surplus')
    except Exception as e:
        problems.append(f"internal validator failed: {e}")

    stats = {"solids": len(vols), "measured": len(raw), "in_layout": produced_total,
             "worst_area": worst_area, "worst_vol": worst_vol,
             "lowest_vol": lowest_vol, "types_wanted": len(want),
             "types_present": len(got)}
    return problems, notes, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pkl", nargs="?", default="out/best.pkl")
    ap.add_argument("step", nargs="?", default="Master Kitchen Layout V3.step")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    import os
    cfg = load_config(args.config) if os.path.exists(args.config) else CutConfig()
    d = pickle.load(open(args.pkl, "rb"))

    # measured in MODEL units so every comparison is scale-free
    raw = extract_parts(args.step, unit_scale=1.0)      # model units: ratios only
    raw_in = extract_parts(args.step)                   # inches: grid and stock checks
    vols = solid_volumes(args.step)
    demand = load_demand(args.step, cfg)

    print(f"auditing {args.pkl} against {args.step}")
    print("=" * 74)
    problems, notes, st = audit(raw, vols, d["patterns"], demand, cfg, raw_in)
    print(f'solids in STEP file      {st["solids"]}')
    print(f'measured as panels       {st["measured"]}')
    print(f'parts in layout          {st["in_layout"]}')
    print(f'worst face-area ratio    {st["worst_area"]:.4f}   (must be <= {TOL})')
    print(f'worst volume ratio       {st["worst_vol"]:.4f}   (must be <= {TOL})')
    print(f'least solid part         {st["lowest_vol"]:.4f}   (joinery removes '
          f'material; flagged below {LOW_FILL})')
    print(f'distinct part types      {st["types_wanted"]} wanted, '
          f'{st["types_present"]} present in layout')
    print("=" * 74)
    if notes:
        print("notes:")
        for n in sorted(set(notes)):
            print(f"  - {n}")
    if problems:
        print(f"\nFAILED with {len(problems)} problem(s):")
        for p in problems:
            print(f"  ! {p}")
        return 1
    print("\nPASS: every solid in the file is present in the layout at full size.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
