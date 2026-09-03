"""Randomized-restart driver: solve many times, validate each, keep the best score."""
from __future__ import annotations

import argparse
import os
import time

from src.bounds import floor_report
from src.cost import score
from src.search import search
from src.model import CutConfig, config_to_dict, load_config, to_units
from src.parts import apply_shave, area_bound, load_demand, summarise
from src.validate import LayoutError, check_job

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("step", nargs="?", default="Master Kitchen Layout V3.step")
    ap.add_argument("-n", "--restarts", type=int, default=24)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--improve", type=int, default=500)
    ap.add_argument("--out", default="out/best.pkl")
    ap.add_argument("--shave", type=float, default=0.0, help="what-if: shave up to X inches off near-miss widths")
    ap.add_argument("--config", default="config.json", help="cost model JSON; omit to use built-in defaults")
    ap.add_argument("--no-weight-thickness", action="store_true",
                    help="ablation: pick a thickness group uniformly when rebuilding")
    ap.add_argument("--no-exact-fill", action="store_true",
                    help="ablation: skip the exactly-filled-strip probe")
    ap.add_argument("--no-width-reuse", action="store_true",
                    help="ablation: do not charge a stop change for a new strip width")
    ap.add_argument("-w", "--workers", type=int, default=None,
                    help="parallel restarts; default all cores, 1 for in-process")
    ap.add_argument("--no-trim", action="store_true", help="exact 2-stage only")
    args = ap.parse_args()

    import os
    if args.config and os.path.exists(args.config):
        cfg = load_config(args.config)
        print(f"cost model: {args.config}")
    else:
        cfg = CutConfig()
        print("cost model: built-in defaults")
    if args.no_trim:
        import dataclasses
        cfg = dataclasses.replace(cfg, allow_trim=False)
    demand = load_demand(args.step, cfg)
    if args.shave > 0:
        demand = apply_shave(demand, cfg, to_units(args.shave))
        print(f"WHAT-IF: shaved up to {args.shave}\" off near-miss widths")
    print(summarise(demand, cfg))
    bounds = area_bound(demand, cfg)
    from math import ceil as _ceil
    int_bound = sum(_ceil(v) for v in bounds.values())
    print("area floor: " + ", ".join(f'{t}"={v:.2f}->{_ceil(v)}'
                                     for t, v in sorted(bounds.items(), reverse=True)))
    print(f"integer lower bound (per-thickness, kerf ignored): {int_bound} sheets\n")

    best_r, all_r = None, []
    t0 = time.time()

    def note(r):
        nonlocal best_r
        if r.patterns is not None and (best_r is None or r.dollars < best_r.dollars):
            best_r = r
            print(f"  [{r.index:4d}] {score(r.patterns, cfg)}")

    best_r, all_r = search(demand, cfg, args.restarts, args.improve,
                           base_seed=args.seed, workers=args.workers,
                           on_result=note,
                           extra={"exact_fill_probe": not args.no_exact_fill,
                                  "width_reuse": not args.no_width_reuse,
                                  "weight_thickness":
                                      not args.no_weight_thickness})
    best, best_score, best_kw = best_r.patterns, score(best_r.patterns, cfg), best_r.params
    check_job(best, demand, cfg)

    dt = time.time() - t0
    fails = sum(1 for r in all_r if r.patterns is None)
    for r in all_r[:3]:
        if r.error:
            print(f"  restart {r.index} invalid: {r.error}")
    nw = args.workers or min(os.cpu_count() or 1, args.restarts)
    print(f"\n{args.restarts} restarts x {args.improve} iters in {dt:.1f}s "
          f"on {nw} worker(s) ({fails} invalid)")
    hist = sorted(r.dollars for r in all_r if r.patterns is not None)
    if hist:
        print(f"score spread: best ${hist[0]:,.0f}  median ${hist[len(hist)//2]:,.0f}  "
              f"worst ${hist[-1]:,.0f}")

    sc = best_score
    print(f"\n{'='*70}\nBEST  params={best_kw}\n{'='*70}")
    print(f"  total cost               ${sc.dollars:,.2f}")
    print(f"    material               ${sc.material:,.2f}   ({sc.n_sheets} sheets)")
    print(f"    labour                 ${sc.labour:,.2f}   ({sc.minutes:.0f} min = "
          f"{sc.minutes/60:.2f} h @ ${cfg.labour_dollars_per_hour:.0f}/h)")
    print(f"  sheets                   {sc.n_sheets}   " +
          "  ".join(f'{t}"x{n}' for t, n in sorted(sc.sheets_by_thickness.items(),
                                                   reverse=True)))
    print()
    print("  operation                 count      min      cost")
    rows = [
        ("track rips", sc.n_rips, cfg.min_per_track_rip),
        ("  track stop changes", sc.n_track_stops, cfg.extra_min_per_track_stop_change),
        ("  rips reusing that stop", sc.n_adjacent_equal_rips, 0.0),
        ("track crosscuts (wide)", sc.n_wide_cross, cfg.min_per_track_crosscut),
        ("mitre crosscuts", sc.n_cross, cfg.min_per_mitre_crosscut),
        ("  mitre stop changes", sc.n_mitre_stops, cfg.extra_min_per_mitre_stop_change),
        ("trim rips", sc.n_trims, cfg.min_per_trim_rip),
        ("  trim stop changes", sc.n_trim_stops, cfg.extra_min_per_trim_stop_change),
        ("sheet setups", sc.n_sheets, cfg.min_per_sheet_setup),
        ("strip handling", sum(len(p.strips) for p in best), cfg.min_per_strip_handling),
        ("saw changeovers", sc.n_saw_changeovers, cfg.min_per_saw_changeover),
    ]
    dpm = cfg.dollars_per_min()
    for label, n, rate in rows:
        mins = n * rate
        print(f"  {label:24s} {n:5d} {mins:8.1f}  ${mins*dpm:8.2f}")
    print(f"  {'TOTAL LABOUR':24s} {'':5s} {sc.minutes:8.1f}  ${sc.labour:8.2f}")
    print()
    util = sc.used_area / (sc.n_sheets * cfg.usable_w * cfg.usable_l)
    print(f"  parts produced           {sc.n_parts} / {sum(p.qty for p in demand)}")
    print(f"  sheet utilisation        {util*100:.1f}%")
    print(f"  area floor               {sum(bounds.values()):.2f} sheets fractional; "
          f"integer bound {int_bound}")
    print(f"  gap to lower bound       {sc.n_sheets - int_bound} sheet(s)")
    for t in sorted(bounds, reverse=True):
        got = sc.sheets_by_thickness.get(t, 0)
        print(f'    {t}" ply                {got} sheets  (floor {bounds[t]:.2f} -> '
              f'{_ceil(bounds[t])}, gap {got - _ceil(bounds[t])})')

    print()
    print(floor_report(demand, cfg, sc))

    save_if_better(best, best_score, demand, cfg, args.out)


def save_if_better(patterns, sc, demand, cfg, path):
    """Keep exactly one champion layout on disk, replaced only when genuinely beaten.

    The stored layout is re-scored under the *current* cost model before comparing, so
    changing a weight re-ranks the champion honestly rather than grandfathering a score
    computed under old weights.
    """
    import os
    import pickle

    from src.report import cut_list

    key = lambda d: sorted((p.w, p.l, p.thickness, p.qty) for p in d)
    if os.path.exists(path):
        try:
            prev = pickle.load(open(path, "rb"))
        except Exception as e:
            print(f"\nexisting {path} unreadable ({e}); replacing")
            prev = None
        if prev is not None:
            if key(prev["demand"]) != key(demand):
                print(f"\n{path} holds a layout for a different part list; replacing")
            else:
                try:
                    check_job(prev["patterns"], demand, cfg)
                    prev_sc = score(prev["patterns"], cfg)
                except LayoutError as e:
                    print(f"\nstored champion is invalid under the current cut model "
                          f"({e}); leaving it untouched. Re-run without --no-trim to "
                          f"replace it.")
                    return
                stored = prev.get("cfg_fields")
                if stored is not None and stored != config_to_dict(cfg):
                    changed = [k for k in set(stored) | set(config_to_dict(cfg))
                               if stored.get(k) != config_to_dict(cfg).get(k)]
                    print(f"\nnote: stored champion was found under different "
                          f"weights ({', '.join(sorted(changed))}); it is re-scored "
                          f"under the current ones below")
                print(f"\nstored champion re-scored under current weights: "
                      f"${prev_sc.dollars:,.2f}  (this run: ${sc.dollars:,.2f})")
                if prev_sc.dollars <= sc.dollars:
                    print(f"keeping existing {path} — not beaten")
                    return

    with open(path, "wb") as f:
        # Store the config as a plain dict as well. A pickled frozen dataclass whose
        # fields have since been renamed silently falls back to class defaults on
        # load, which reads as agreement when it is really amnesia.
        pickle.dump({"patterns": patterns, "cfg": cfg,
                     "cfg_fields": config_to_dict(cfg), "demand": demand}, f)
    txt = path.rsplit(".", 1)[0] + "_cutlist.txt"
    with open(txt, "w") as f:
        f.write(f"{sc}\n\n{cut_list(patterns, cfg)}\n")
    print(f"NEW CHAMPION: saved {path} and {txt}")


if __name__ == "__main__":
    main()
