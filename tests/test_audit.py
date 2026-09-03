"""Prove the independent audit actually fails when it should.

An audit nobody has watched fail proves nothing. Each test here corrupts one thing --
a shrunk dimension, a dropped part, a substituted size -- and asserts the audit
catches it. These are the failures that would otherwise show up as a BETTER score,
because a smaller or missing part is cheaper to cut.
"""
import sys

sys.path.insert(0, ".")

from dataclasses import replace

from src.geometry import RawPart
from src.model import CutConfig, PartType, Pattern, Placement, Strip, to_units
from verify import audit

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def _fixture():
    """Three consistent panels: measurements, volumes, demand and a valid layout."""
    cfg = CutConfig()
    specs = [(0.75, 10.0, 30.0), (0.75, 10.0, 30.0), (0.75, 12.0, 20.0)]
    raw = [RawPart(index=i, thickness=t, width=w, length=l, face_area=w * l)
           for i, (t, w, l) in enumerate(specs)]
    vols = [t * w * l for t, w, l in specs]

    a = PartType(w=to_units(10), l=to_units(30), thickness=0.75, qty=2, label='10x30')
    b = PartType(w=to_units(12), l=to_units(20), thickness=0.75, qty=1, label='12x20')
    demand = [a, b]

    def strip(width_in, parts):
        s = Strip(width=to_units(width_in))
        off = 0
        for pt, w_in, l_in in parts:
            s.placements.append(Placement(part=pt, length=to_units(l_in),
                                          width=to_units(w_in), offset=off))
            off += to_units(l_in) + cfg.kerf_mitre_saw
        return s

    pat = Pattern(thickness=0.75, strips=[
        strip(10, [(a, 10, 30), (a, 10, 30)]),
        strip(12, [(b, 12, 20)]),
    ])
    return cfg, raw, vols, demand, [pat]


def test_audit_passes_on_consistent_data():
    cfg, raw, vols, demand, pats = _fixture()
    problems, notes, st = audit(raw, vols, pats, demand, cfg)
    check(not problems, f"clean fixture reported problems: {problems}")
    check(st["solids"] == 3 and st["in_layout"] == 3,
          f"bad stats: {st}")


def test_audit_catches_a_silently_shrunk_part():
    """The headline case: geometry measures a part smaller than it is."""
    cfg, raw, vols, demand, pats = _fixture()
    # pretend the calipers returned 9" instead of 10" for solid 0. Volume is measured
    # by a different code path, so it still reflects the real part.
    raw[0] = replace(raw[0], width=9.0, face_area=9.0 * 30.0)
    problems, notes, st = audit(raw, vols, pats, demand, cfg)
    check(any("exceeds its own" in p and "bounding box" in p for p in problems),
          f"a 10% shrink was NOT caught. problems={problems}")
    check(st["worst_vol"] > 1.001,
          f"volume ratio {st['worst_vol']:.4f} should exceed 1 when a dim shrinks")


def test_audit_catches_a_shrunk_thickness():
    cfg, raw, vols, demand, pats = _fixture()
    raw[2] = replace(raw[2], thickness=0.5)     # really 0.75
    problems, _, _ = audit(raw, vols, pats, demand, cfg)
    check(any("exceeds its own" in p for p in problems),
          f"a thickness error was NOT caught. problems={problems}")


def test_audit_catches_a_dropped_part():
    cfg, raw, vols, demand, pats = _fixture()
    pats[0].strips[1].placements.clear()        # lose the 12x20
    problems, _, _ = audit(raw, vols, pats, demand, cfg)
    check(any("layout has 2 parts but the file has 3" in p for p in problems),
          f"a dropped part was NOT counted. problems={problems}")
    check(any("need 1, layout has 0" in p for p in problems),
          f"a dropped part was NOT caught by type. problems={problems}")


def test_audit_catches_a_size_not_in_the_file():
    cfg, raw, vols, demand, pats = _fixture()
    bogus = PartType(w=to_units(5), l=to_units(7), thickness=0.75, qty=0, label='5x7')
    pats[0].strips[1].placements[0] = Placement(
        part=bogus, length=to_units(7), width=to_units(5), offset=0)
    problems, _, _ = audit(raw, vols, pats, demand, cfg)
    check(any("not in the STEP file at all" in p for p in problems),
          f"a substituted size was NOT caught. problems={problems}")


def test_audit_reports_surplus_as_a_note_not_a_failure():
    """Overproduction is legal but costs material, so it must be visible."""
    cfg, raw, vols, demand, pats = _fixture()
    a = demand[0]
    pats[0].strips[0].placements.append(
        Placement(part=a, length=to_units(30), width=to_units(10),
                  offset=to_units(62)))
    problems, notes, _ = audit(raw, vols, pats, demand, cfg)
    check(any("surplus" in n for n in notes),
          f"surplus was not reported. notes={notes}")


def test_audit_notes_a_solid_that_is_not_a_panel():
    cfg, raw, vols, demand, pats = _fixture()
    vols[0] = vols[0] * 0.3        # a bracket, not a flat panel
    problems, notes, _ = audit(raw, vols, pats, demand, cfg)
    check(any("not a plain panel" in n for n in notes),
          f"a 30%-solid part was not flagged. notes={notes}")
    check(not any("exceeds" in p for p in problems),
          "a low-fill solid must be a note, not a failure")


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
    print("\nall audit checks passed")
