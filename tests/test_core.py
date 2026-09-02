"""Cross-checks. Kerf and DP bugs are silent, so these are the safety net."""
import itertools
import random
import sys

sys.path.insert(0, ".")

from src.cost import score
from src.knapsack import bounded_knapsack, np_knapsack, split_groups
from src.model import GRID, CutConfig, PartType, Pattern, Placement, Strip, to_units
from src.validate import LayoutError, check_job, check_pattern

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
        off += to_units(l_in) + cfg.kerf_mitre_saw
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
    # hand-computed under the current weights:
    #   2 rips x 1.0                        =  2.0
    #   1 track stop change x 4.0           =  4.0
    #   6 track-saw crosscuts x 5.0         = 30.0   (23.5in strip > 14in mitre limit)
    #   0 mitre cuts, 0 mitre stops         =  0.0
    #   1 sheet setup x 5.0                 =  5.0
    #   2 strips x 1.0 handling             =  2.0
    #   0 saw changeovers (track saw only)  =  0.0
    #                                        ------
    #                                         43.0 min
    check(abs(sc.material - 60.0) < 1e-6, f"material {sc.material}")
    check(abs(sc.minutes - 43.0) < 1e-6, f"minutes {sc.minutes}, expected 43")
    check(abs(sc.dollars - (60.0 + 43.0 * 1.25)) < 1e-6,
          f"dollars {sc.dollars}, expected 113.75")
    check(sc.n_track_stops == 1, f"stops {sc.n_track_stops}")
    check(sc.n_saw_changeovers == 0,
          f"expected 0 changeovers on a track-saw-only sheet, got {sc.n_saw_changeovers}")


def test_saw_changeovers():
    """A sheet needing both saws costs a changeover; a track-only sheet does not."""
    cfg = CutConfig()
    # narrow strip -> mitre saw; wide strip -> track saw. Both on one sheet.
    narrow = _mk(10, 20, qty=2)
    wide = _mk(30, 20, qty=1)
    p = Pattern(thickness=0.5,
                strips=[_strip(cfg, 30, [(wide, 30, 20)]),
                        _strip(cfg, 10, [(narrow, 10, 20)] * 2)])
    check_pattern(p, cfg)
    check(p.saw_stations(cfg) == ["track", "mitre"],
          f"expected track then mitre, got {p.saw_stations(cfg)}")
    check(score([p], cfg).n_saw_changeovers == 1,
          "expected 1 changeover on a mixed sheet")

    # two such sheets chain: track,mitre,track,mitre -> 3 changeovers
    check(score([p, p], cfg).n_saw_changeovers == 3,
          f"expected 3 changeovers over two mixed sheets, "
          f"got {score([p, p], cfg).n_saw_changeovers}")

    # a trim sends you back to the track saw after the mitre work
    s_trim = Strip(width=to_units(10))
    s_trim.placements.append(Placement(part=narrow, length=to_units(20),
                                       width=to_units(10), offset=0))
    s_trim.placements.append(Placement(part=_mk(8, 20), length=to_units(20),
                                       width=to_units(8),
                                       offset=to_units(20) + cfg.kerf_mitre_saw))
    p2 = Pattern(thickness=0.5, strips=[s_trim])
    check_pattern(p2, cfg)
    check(p2.saw_stations(cfg) == ["track", "mitre", "track"],
          f"expected track,mitre,track with a trim, got {p2.saw_stations(cfg)}")


def test_mitre_stop_sequencing():
    """The stop block only reaches the strip's END, so settings are sequential."""
    cfg = CutConfig()
    pt = _mk(10, 20, qty=4)
    # two narrow strips, each holding two 20in parts: one run each, and the second
    # strip's leading run reuses the setting the first left behind -> 1 setting total
    p = Pattern(thickness=0.5,
                strips=[_strip(cfg, 10, [(pt, 10, 20)] * 2),
                        _strip(cfg, 10, [(pt, 10, 20)] * 2)])
    check_pattern(p, cfg)
    check(score([p], cfg).n_mitre_stops == 1,
          f"expected 1 setting for one repeated length, "
          f"got {score([p], cfg).n_mitre_stops}")

    # strip A = [30, 20], strip B = [20, 30]. A costs 2 settings and leaves the stop
    # at 20; B can be fed 20-end first and so costs 1 more. Total 3 -- not the 2 a
    # per-sheet distinct-length count would claim, nor the 4 of no carryover at all.
    a = _mk(10, 30)
    b = _mk(10, 20)
    p2 = Pattern(thickness=0.5,
                 strips=[_strip(cfg, 10, [(a, 10, 30), (b, 10, 20)]),
                         _strip(cfg, 10, [(b, 10, 20), (a, 10, 30)])])
    check_pattern(p2, cfg)
    got = score([p2], cfg).n_mitre_stops
    check(got == 3, f"expected 3 sequenced mitre settings, got {got}")


def test_mitre_stop_cannot_reach_the_middle():
    """A length repeated either side of a different one cannot share its setting."""
    cfg = CutConfig()
    short = _mk(10, 20, qty=2)
    tall = _mk(10, 30)
    # deliberately laid out 20, 30, 20 -- three runs, so three stop settings, even
    # though only two distinct lengths are present
    s = Strip(width=to_units(10))
    off = 0
    for pt, l in ((short, 20), (tall, 30), (short, 20)):
        s.placements.append(Placement(part=pt, length=to_units(l),
                                      width=to_units(10), offset=off))
        off += to_units(l) + cfg.kerf_mitre_saw
    p = Pattern(thickness=0.5, strips=[s])
    check_pattern(p, cfg)
    runs = p.mitre_run_sequences(cfg)[0]
    check(len(runs) == 3, f"expected 3 runs for 20,30,20 layout, got {runs}")
    check(score([p], cfg).n_mitre_stops == 3,
          f"expected 3 settings, got {score([p], cfg).n_mitre_stops}")

    # the solver's own length-sorted layout collapses those to two runs
    s2 = _strip(cfg, 10, [(tall, 10, 30), (short, 10, 20), (short, 10, 20)])
    p2 = Pattern(thickness=0.5, strips=[s2])
    check_pattern(p2, cfg)
    check(len(p2.mitre_run_sequences(cfg)[0]) == 2,
          "length-sorted layout should collapse equal lengths into one run")


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


def _tiny_demand():
    return [_mk(12, 24, qty=6), _mk(10, 30, qty=4), _mk(6, 18, qty=8),
            _mk(23, 30, qty=3)]


def test_restart_determinism():
    """Same seed, same layout -- the contract parallelism relies on."""
    from src.search import draw_params, restart_seed, run_restart
    import random as _r

    cfg = CutConfig()
    demand = _tiny_demand()
    a = run_restart(demand, cfg, 42, 3, iters=25)
    b = run_restart(demand, cfg, 42, 3, iters=25)
    check(a.patterns is not None, f"restart failed: {a.error}")
    check(abs(a.dollars - b.dollars) < 1e-9,
          f"same seed gave {a.dollars} then {b.dollars}")
    check(a.params == b.params, "same seed gave different parameters")
    # a different index must actually explore somewhere else
    c = run_restart(demand, cfg, 42, 4, iters=25)
    check(restart_seed(42, 3) != restart_seed(42, 4), "seeds collide across indices")


def test_parallel_matches_serial():
    """Ten workers must find exactly what one worker finds."""
    from src.search import search

    cfg = CutConfig()
    demand = _tiny_demand()
    ser, _ = search(demand, cfg, restarts=6, iters=25, base_seed=5, workers=1)
    par, _ = search(demand, cfg, restarts=6, iters=25, base_seed=5, workers=4)
    check(abs(ser.dollars - par.dollars) < 1e-9,
          f"serial ${ser.dollars:,.2f} vs parallel ${par.dollars:,.2f}")
    check_job(par.patterns, demand, cfg)


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