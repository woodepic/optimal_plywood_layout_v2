"""The drawn geometry must agree with the cost model.

The UI is only trustworthy if the rectangles it draws are the parts the solver costed.
These check that every rect lies inside its sheet and inside its strip, and that the
cut lines the client will draw are exactly the cuts the scorer charged for -- if the
picture shows fewer cuts than you are billed for, one of the two is wrong.
"""
import pickle
import sys

sys.path.insert(0, ".")

from src.model import CutConfig, PartType, Pattern, Placement, Strip, load_config, to_units
from src.serialize import layout_to_dict

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def _tiny():
    cfg = CutConfig()
    a = PartType(w=to_units(10), l=to_units(30), thickness=0.5, qty=3, label="10x30")
    b = PartType(w=to_units(20), l=to_units(40), thickness=0.5, qty=1, label="20x40")

    def strip(width_in, parts):
        s = Strip(width=to_units(width_in))
        off = 0
        for pt, w_in, l_in in parts:
            s.placements.append(Placement(part=pt, length=to_units(l_in),
                                          width=to_units(w_in), offset=off))
            off += to_units(l_in) + cfg.kerf_mitre_saw
        return s

    pat = Pattern(thickness=0.5, strips=[
        strip(10, [(a, 10, 30), (a, 10, 30)]),
        strip(20, [(b, 20, 40), (a, 10, 30)]),      # second part needs a trim
    ])
    return cfg, [a, b], [pat]


def test_every_rect_is_inside_its_sheet_and_strip():
    cfg, demand, pats = _tiny()
    j = layout_to_dict(pats, demand, cfg)
    for sh in j["sheets"]:
        for st in sh["strips"]:
            check(st["y"] + st["width"] <= sh["across"],
                  f'strip at y={st["y"]} w={st["width"]} exceeds across={sh["across"]}')
            for p in st["parts"]:
                check(p["x"] >= 0 and p["x"] + p["len"] <= sh["along"],
                      f'part x={p["x"]} len={p["len"]} outside along={sh["along"]}')
                check(p["y"] == st["y"],
                      f'part y={p["y"]} does not sit at its strip y={st["y"]}')
                check(p["w"] <= st["width"],
                      f'part w={p["w"]} wider than its strip {st["width"]}')


def test_drawn_cuts_match_the_costed_cuts():
    cfg, demand, pats = _tiny()
    j = layout_to_dict(pats, demand, cfg)
    for sh in j["sheets"]:
        c = sh["counts"]
        drawn = {"rip": 0, "cross": 0, "trim": 0}
        for cut in sh["cuts"]:
            drawn[cut["type"]] += 1
        check(drawn["rip"] == c["n_rips"],
              f'drew {drawn["rip"]} rips but scored {c["n_rips"]}')
        check(drawn["cross"] == c["n_cross"] + c["n_wide_cross"],
              f'drew {drawn["cross"]} crosscuts but scored '
              f'{c["n_cross"] + c["n_wide_cross"]}')
        check(drawn["trim"] == c["n_trims"],
              f'drew {drawn["trim"]} trims but scored {c["n_trims"]}')


def test_saw_assignment_follows_strip_width():
    cfg, demand, pats = _tiny()
    j = layout_to_dict(pats, demand, cfg)
    for sh in j["sheets"]:
        for st in sh["strips"]:
            want = "track" if st["width"] > cfg.mitre_max_crosscut_width else "mitre"
            check(st["saw"] == want,
                  f'strip {st["width"]} assigned {st["saw"]}, want {want}')
        for cut in sh["cuts"]:
            if cut["type"] in ("rip", "trim"):
                check(cut["saw"] == "track",
                      f'{cut["type"]} assigned to {cut["saw"]}: rips are track saw only')


def test_equal_widths_share_a_colour_across_sheets():
    cfg, demand, pats = _tiny()
    pats = pats + [pats[0]]           # two identical sheets
    j = layout_to_dict(pats, demand, cfg)
    by_width = {}
    for sh in j["sheets"]:
        for st in sh["strips"]:
            by_width.setdefault(st["width"], set()).add(st["color"])
    for w, colors in by_width.items():
        check(len(colors) == 1,
              f"width {w} drawn in {len(colors)} different colours across sheets")
    check(len({w["color"] for w in j["widths"]}) == len(j["widths"]),
          "two distinct widths share a colour")


def test_real_champion_serialises_consistently():
    import os
    if not os.path.exists("out/best.pkl"):
        return
    cfg = load_config("config.json") if os.path.exists("config.json") else CutConfig()
    d = pickle.load(open("out/best.pkl", "rb"))
    j = layout_to_dict(d["patterns"], d["demand"], cfg, "champion")
    check(j["score"]["n_parts"] == j["demand_parts"],
          f'serialised {j["score"]["n_parts"]} parts but demand is {j["demand_parts"]}')
    total_drawn = sum(len(st["parts"]) for sh in j["sheets"] for st in sh["strips"])
    check(total_drawn == j["demand_parts"],
          f"drew {total_drawn} parts, demand is {j['demand_parts']}")
    for sh in j["sheets"]:
        for st in sh["strips"]:
            check(st["y"] + st["width"] <= sh["across"], f'sheet {sh["index"]} overflows')


def test_every_square_inch_is_accounted_for():
    """part area + waste + kerf must equal the sheet exactly.

    This is what guarantees the hatching shows ALL the waste. Only the strip-end
    offcut used to be drawn, which left the sliver beside a trimmed part and the band
    after the last rip invisible -- waste you were paying for but could not see.
    """
    cfg, demand, pats = _tiny()
    j = layout_to_dict(pats, demand, cfg)
    for sh in j["sheets"]:
        total = sh["across"] * sh["along"]
        waste = sum(r["w"] * r["h"] for r in sh["waste"])
        acc = sh["part_area"] + waste + sh["kerf_area"]
        check(acc == total,
              f'sheet {sh["index"]}: parts {sh["part_area"]} + waste {waste} + kerf '
              f'{sh["kerf_area"]} = {acc}, but the sheet is {total} '
              f'({total - acc} unaccounted for)')
        # and no waste rect may stray outside the sheet or overlap a part
        for r in sh["waste"]:
            check(r["x"] >= 0 and r["x"] + r["w"] <= sh["along"]
                  and r["y"] >= 0 and r["y"] + r["h"] <= sh["across"],
                  f'waste rect {r} lies outside the sheet')
            check(r["w"] > 0 and r["h"] > 0, f"zero-area waste rect emitted: {r}")


def test_waste_kinds_cover_all_three_causes():
    cfg, demand, pats = _tiny()
    j = layout_to_dict(pats, demand, cfg)
    kinds = {r["kind"] for sh in j["sheets"] for r in sh["waste"]}
    # the tiny fixture has a trimmed part, partly filled strips and a leftover band
    for want in ("offcut", "trim", "edge"):
        check(want in kinds, f'no "{want}" waste emitted; got {kinds}')


def test_real_champion_waste_is_exact():
    import os
    if not os.path.exists("out/best.pkl"):
        return
    cfg = load_config("config.json") if os.path.exists("config.json") else CutConfig()
    d = pickle.load(open("out/best.pkl", "rb"))
    j = layout_to_dict(d["patterns"], d["demand"], cfg)
    for sh in j["sheets"]:
        total = sh["across"] * sh["along"]
        acc = sh["part_area"] + sum(r["w"] * r["h"] for r in sh["waste"]) \
            + sh["kerf_area"]
        check(acc == total, f'sheet {sh["index"]}: {total - acc} units unaccounted for')


def test_one_colour_index_per_distinct_width():
    cfg, demand, pats = _tiny()
    pats = pats + [pats[0]]
    j = layout_to_dict(pats, demand, cfg)
    w2c, c2w = {}, {}
    for sh in j["sheets"]:
        for st in sh["strips"]:
            w2c.setdefault(st["width"], set()).add(st["color"])
            c2w.setdefault(st["color"], set()).add(st["width"])
    for w, cs in w2c.items():
        check(len(cs) == 1, f"width {w} got {len(cs)} colour indices")
    for c, ws in c2w.items():
        check(len(ws) == 1, f"colour index {c} shared by widths {ws}")
    # indices must be contiguous from 0 so the palette is used from its start
    check(sorted(c2w) == list(range(len(c2w))),
          f"colour indices are not contiguous: {sorted(c2w)}")


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
    print("\nall serialize checks passed")
