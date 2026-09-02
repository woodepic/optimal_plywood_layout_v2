"""Cross-checks. Kerf and DP bugs are silent, so these are the safety net."""
import itertools
import random
import sys

sys.path.insert(0, ".")

from src.cost import score
from src.knapsack import bounded_knapsack, np_knapsack, split_groups
from src.model import GRID, CutConfig, PartType, Pattern, Placement, Strip, to_units
from src.validate import LayoutError, check_pattern

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def brute_knapsack(items, cap):
    """Exhaustive best value for tiny bounded-knapsack instances."""
    best = 0.0
    ranges = [range(c + 1) for _, _, c in items]
    for combo in itertools.product(*ranges):
        size = sum(n * items[i][0] for i, n in enumerate(combo))
        if size <= cap:
            best = max(best, sum(n * items[i][1] for i, n in enumerate(combo)))
    return best


def test_knapsack_vs_brute():
    rng = random.Random(1)
    for trial in range(300):
        n = rng.randint(1, 4)
        items = [(rng.randint(1, 40), float(rng.randint(1, 40)), rng.randint(1, 3))
                 for _ in range(n)]
        cap = rng.randint(1, 90)
        want = brute_knapsack(items, cap)
        got_b, _ = bounded_knapsack(items, cap)
        sizes, values, _ = split_groups(items)
        got_n, _, _ = np_knapsack(sizes, values, cap) if sizes else (0.0, 0, [])
        check(abs(got_b - want) < 1e-6,
              f"bounded_knapsack trial {trial}: got {got_b}, brute {want}, items={items}, cap={cap}")
        check(abs(got_n - want) < 1e-6,
              f"np_knapsack trial {trial}: got {got_n}, brute {want}, items={items}, cap={cap}")


def _mk(w_in, l_in, qty=1, t=0.5):
    return PartType(w=to_units(w_in), l=to_units(l_in), thickness=t, qty=qty)


def _strip(cfg, width_in, parts):
    """Build a strip laying parts end to end with a crosscut kerf between them."""
    s = Strip(width=to_units(width_in))
    off = 0
    for pt, w_in, l_in in parts:
        s.placements.append(Placement(part=pt, length=to_units(l_in),
                                      width=to_units(w_in), offset=off))
        off += to_units(l_in) + cfg.kerf_cross
    return s


def test_kerf_boundary():
    """n parts plus (n-1) kerfs must exactly fill the strip, and one unit more must not."""
    cfg = CutConfig()
    # three parts of 31-31/32" + 2 kerfs of 1/16" = 95.90625 + 0.125 = 96.03" > 96"
    # use 31-15/16": 3*31.9375 + 2*0.0625 = 95.8125 + 0.125 = 95.9375 <= 96  OK
    pt = _mk(24, 31.9375, qty=3)
    p = Pattern(thickness=0.5, strips=[_strip(cfg, 24, [(pt, 24, 31.9375)] * 3)])
    try:
        check_pattern(p, cfg)
    except LayoutError as e:
        check(False, f"valid kerf-tight strip rejected: {e}")

    # 32" parts: 3*32 + 2*0.0625 = 96.125 > 96  -> must be rejected
    pt2 = _mk(24, 32, qty=3)
    p2 = Pattern(thickness=0.5, strips=[_strip(cfg, 24, [(pt2, 24, 32)] * 3)])
    try:
        check_pattern(p2, cfg)
        check(False, "strip overflowing by one kerf was accepted")
    except LayoutError:
        pass


def test_rip_kerf_boundary():
    """Two 24in strips need 48-1/16in once the rip kerf is spent, so must be rejected."""
    cfg = CutConfig()
    pt = _mk(24, 30)
    two = Pattern(thickness=0.5, strips=[_strip(cfg, 24, [(pt, 24, 30)]),
                                         _strip(cfg, 24, [(pt, 24, 30)])])
    try:
        check_pattern(two, cfg)
        check(False, "two 24in strips accepted on a 48in sheet despite the rip kerf")
    except LayoutError:
        pass
    # 23-31/32 + 23-31/32 + kerf = 48.0 exactly -> must be accepted
    pt3 = _mk(23.96875, 30)
    ok = Pattern(thickness=0.5,
                 strips=[_strip(cfg, 23.96875, [(pt3, 23.96875, 30)]),
                         _strip(cfg, 23.96875, [(pt3, 23.96875, 30)])])
    try:
        check_pattern(ok, cfg)
    except LayoutError as e:
        check(False, f"exactly-fitting strip pair rejected: {e}")


def test_validator_catches_bad_layouts():
    cfg = CutConfig()
    pt = _mk(24, 30)

    # part wider than its strip
    bad = Pattern(thickness=0.5, strips=[_strip(cfg, 12, [(pt, 24, 30)])])
    try:
        check_pattern(bad, cfg)
        check(False, "part wider than its strip was accepted")
    except LayoutError:
        pass

    # placement that is not a real orientation of the part
    s = Strip(width=to_units(24))
    s.placements.append(Placement(part=pt, length=to_units(29), width=to_units(24), offset=0))
    try:
        check_pattern(Pattern(thickness=0.5, strips=[s]), cfg)
        check(False, "bogus orientation was accepted")
    except LayoutError:
        pass

    # overlapping placements
    s2 = Strip(width=to_units(24))
    s2.placements.append(Placement(part=pt, length=to_units(30), width=to_units(24), offset=0))
    s2.placements.append(Placement(part=pt, length=to_units(30), width=to_units(24), offset=10))
    try:
        check_pattern(Pattern(thickness=0.5, strips=[s2]), cfg)
        check(False, "overlapping placements accepted")
    except LayoutError:
        pass

    # wrong thickness on the sheet
    s3 = _strip(cfg, 24, [(_mk(24, 30, t=0.75), 24, 30)])
    try:
        check_pattern(Pattern(thickness=0.5, strips=[s3]), cfg)
        check(False, "3/4in part accepted on a 1/2in sheet")
    except LayoutError:
        pass


def test_cost_arithmetic():
    """Hand-computed sheet: 2 strips x 3 parts, checked term by term."""
    cfg = CutConfig()
    pt = _mk(23.5, 30, qty=6)
    p = Pattern(thickness=0.5,
                strips=[_strip(cfg, 23.5, [(pt, 23.5, 30)] * 3),
                        _strip(cfg, 23.5, [(pt, 23.5, 30)] * 3)])
    check_pattern(p, cfg)
    c = p.counts(cfg)
    # width: 2*23.5 + 1 kerf = 47.0625 < 48, so a remainder exists -> 2 rips
    check(c["n_rips"] == 2, f"expected 2 rips, got {c['n_rips']}")
    # length: 3*30 + 2*(1/16) = 90.125 < 96, remainder exists -> 3 crosscuts per strip
    check(c["n_cross"] + c["n_wide_cross"] == 6,
          f"expected 6 crosscuts, got {c['n_cross'] + c['n_wide_cross']}")
    # 23.5in strip exceeds the 14in mitre capacity, so all crosscuts go to the track saw
    check(c["n_wide_cross"] == 6 and c["n_cross"] == 0,
          f"expected all 6 crosscuts on the track saw, got {c}")
    check(c["distinct_widths"] == 1, "expected one distinct width")
    check(c["n_trims"] == 0, "expected no trims")

    sc = score([p], cfg)
    # material 60; labour = 2 rips*1 + 1 stop*4 + 6 wide crosscuts*1 = 12 min = $15
    check(abs(sc.material - 60.0) < 1e-6, f"material {sc.material}")
    check(abs(sc.minutes - 12.0) < 1e-6, f"minutes {sc.minutes}, expected 12")
    check(abs(sc.dollars - 75.0) < 1e-6, f"dollars {sc.dollars}, expected 75")
    check(sc.n_track_stops == 1, f"stops {sc.n_track_stops}")


def test_stop_carryover():
    """A width shared between consecutive sheets must not be paid for twice."""
    cfg = CutConfig()
    pt = _mk(23.5, 30, qty=12)
    mk = lambda: Pattern(thickness=0.5,
                         strips=[_strip(cfg, 23.5, [(pt, 23.5, 30)] * 3),
                                 _strip(cfg, 23.5, [(pt, 23.5, 30)] * 3)])
    sc = score([mk(), mk()], cfg)
    # both sheets use only 23.5in, so the stop is set once for the whole job
    check(sc.n_track_stops == 1,
          f"expected 1 stop setting across two identical sheets, got {sc.n_track_stops}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            n0 = len(FAILS)
            fn()
            print(f"{'PASS' if len(FAILS) == n0 else 'FAIL'}  {name}")
    if FAILS:
        print(f"\n{len(FAILS)} failure(s):")
        for f in FAILS[:20]:
            print("  -", f)
        sys.exit(1)
    print("\nall checks passed")
