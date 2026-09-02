"""Design-side advice: small dimensional changes that pay for themselves.

The optimizer takes the parts as given. This module asks the other question — which
parts are awkward, and how little would have to change for them to stop being awkward.
Everything here is a suggestion about the CAD model, never applied automatically.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .model import GRID, CutConfig, PartType, fmt


def _strip_widths(demand: list[PartType], cfg: CutConfig) -> Counter:
    """The narrowest strip each part could live in, by quantity."""
    out: Counter = Counter()
    for pt in demand:
        legal = [min(w, l) for w, l in pt.variants()
                 if w <= cfg.usable_w and l <= cfg.usable_l]
        if legal:
            out[min(legal)] += pt.qty
    return out


def mitre_capacity(demand: list[PartType], cfg: CutConfig) -> dict:
    """How many parts could reach the mitre saw at various crosscut capacities.

    A part can only be crosscut on the mitre saw if its strip is within capacity, and
    the narrowest strip it can live in is its smaller dimension. So the smaller
    dimension decides which saw does its crosscut — and at 5 min versus 0.25 min, that
    is the most expensive single consequence of a part's dimensions.
    """
    widths = _strip_widths(demand, cfg)
    total = sum(widths.values())
    cap_now = cfg.mitre_max_crosscut_width
    excess_min = cfg.min_per_track_crosscut - cfg.min_per_mitre_crosscut
    per_cut = excess_min * cfg.dollars_per_min()

    rows = []
    for cap_in in (10, 12, 14, 16, 18, 20, 24):
        cap = cap_in * GRID
        reachable = sum(q for w, q in widths.items() if w <= cap)
        rows.append({"capacity": cap_in, "parts_on_mitre": reachable,
                     "parts_on_track": total - reachable,
                     "saving_vs_now": max(0, reachable - sum(
                         q for w, q in widths.items() if w <= cap_now)) * per_cut})
    return {"rows": rows, "total": total, "per_cut_saving": per_cut,
            "forced": sum(q for w, q in widths.items() if w > cap_now)}


def mitre_near_misses(demand: list[PartType], cfg: CutConfig,
                      tol_in: float = 3.0) -> list[dict]:
    """Parts whose smaller dimension only just exceeds the mitre capacity.

    Shaving these moves their crosscuts from the track saw to the mitre saw.
    """
    widths = _strip_widths(demand, cfg)
    cap = cfg.mitre_max_crosscut_width
    excess = (cfg.min_per_track_crosscut - cfg.min_per_mitre_crosscut) \
        * cfg.dollars_per_min()
    out = []
    for w, qty in widths.items():
        if cap < w <= cap + int(tol_in * GRID):
            out.append({"width": w, "qty": qty, "shave": w - cap,
                        "saving": qty * excess})
    return sorted(out, key=lambda d: -d["saving"])


def length_cliffs(demand: list[PartType], cfg: CutConfig,
                  threshold_units: int = 8) -> list[dict]:
    """Parts whose length only just misses fitting another copy per strip.

    The width-direction version of this is `report.kerf_cliff`; this is the same
    argument along the strip.
    """
    cap = cfg.usable_l + cfg.kerf_mitre_saw
    lengths: Counter = Counter()
    for pt in demand:
        legal = [max(w, l) for w, l in pt.variants()
                 if w <= cfg.usable_w and l <= cfg.usable_l]
        if legal:
            lengths[min(legal)] += pt.qty

    out = []
    for l, qty in lengths.items():
        k = cap // (l + cfg.kerf_mitre_saw)
        if k < 1:
            continue
        target = cap / (k + 1) - cfg.kerf_mitre_saw
        shave = l - target
        if 0 < shave <= threshold_units and qty >= k + 1:
            out.append({"length": l, "qty": qty, "per_strip_now": k,
                        "per_strip_after": k + 1, "shave": shave, "target": target})
    return sorted(out, key=lambda d: (d["shave"], -d["qty"]))


def dimension_clusters(demand: list[PartType], cfg: CutConfig,
                       tol_in: float = 0.375, min_members: int = 3) -> dict:
    """Near-duplicate dimensions that could be unified.

    Distinct strip widths cost a track saw stop change each; distinct crosscut lengths
    cost a mitre stop change each. Dimensions that differ by less than a saw blade's
    worth of consequence are pure overhead in a build like this.
    """
    def cluster(counter: Counter, tol: int):
        groups = []
        for v in sorted(counter):
            if groups and v - groups[-1][-1] <= tol:
                groups[-1].append(v)
            else:
                groups.append([v])
        return [g for g in groups if len(g) >= min_members]

    tol = int(tol_in * GRID)
    widths = _strip_widths(demand, cfg)
    lengths: Counter = Counter()
    for pt in demand:
        legal = [max(w, l) for w, l in pt.variants()
                 if w <= cfg.usable_w and l <= cfg.usable_l]
        if legal:
            lengths[min(legal)] += pt.qty

    stop_cost = (cfg.min_per_track_rip + cfg.extra_min_per_track_stop_change) \
        * cfg.dollars_per_min()
    mitre_cost = cfg.extra_min_per_mitre_stop_change * cfg.dollars_per_min()

    return {
        "widths": [{"values": g, "qty": sum(widths[v] for v in g),
                    "unify_to": max(g), "saves": (len(g) - 1) * stop_cost}
                   for g in cluster(widths, tol)],
        "lengths": [{"values": g, "qty": sum(lengths[v] for v in g),
                     "unify_to": max(g), "saves": (len(g) - 1) * mitre_cost}
                    for g in cluster(lengths, tol)],
    }


def advise(demand: list[PartType], cfg: CutConfig) -> str:
    from .report import kerf_cliff

    L = []
    mc = mitre_capacity(demand, cfg)
    L.append("=" * 72)
    L.append("MITRE SAW CAPACITY — the most expensive dimensional consequence")
    L.append("=" * 72)
    L.append(f'A part\'s SMALLER dimension decides which saw crosscuts it. Under 14" it')
    L.append(f'goes to the mitre saw at {cfg.min_per_mitre_crosscut} min; over, to the '
             f'track saw at {cfg.min_per_track_crosscut} min')
    L.append(f'— a ${mc["per_cut_saving"]:.2f} difference per cut.')
    L.append(f'\n{mc["forced"]} of {mc["total"]} parts are currently forced onto the '
             f'track saw.\n')
    L.append("  mitre capacity   parts on mitre   parts on track   saving vs now")
    for r in mc["rows"]:
        mark = " <- you" if r["capacity"] == cfg.mitre_max_crosscut_width // GRID else ""
        L.append(f'  {r["capacity"]:>10d}"   {r["parts_on_mitre"]:>14d}   '
                 f'{r["parts_on_track"]:>14d}   ${r["saving_vs_now"]:>10,.0f}{mark}')

    nm = mitre_near_misses(demand, cfg)
    if nm:
        L.append(f'\nParts that only just miss the mitre saw — shaving the smaller')
        L.append(f'dimension moves every one of their crosscuts to the cheap saw:')
        L.append("  smaller dim    qty   shave off   saving")
        for r in nm:
            L.append(f'  {fmt(r["width"]):>11s}  {r["qty"]:5d}   '
                     f'{r["shave"]/GRID:8.4f}"   ${r["saving"]:8,.0f}')

    L.append("\n" + "=" * 72)
    L.append("STRIPS PER SHEET — widths that just miss fitting one more")
    L.append("=" * 72)
    wc = kerf_cliff(demand, cfg)
    if wc:
        L.append("  width      qty   strips/sheet   shave off   would become")
        for r in wc:
            L.append(f'  {fmt(r["width"]):>9s}  {r["qty"]:4d}   {r["strips_now"]} -> '
                     f'{r["strips_after"]}          {r["shave"]/GRID:.4f}"    '
                     f'{r["target"]/GRID:.4f}"')
    else:
        L.append("  none")

    L.append("\n" + "=" * 72)
    L.append("PARTS PER STRIP — lengths that just miss fitting one more")
    L.append("=" * 72)
    lc = length_cliffs(demand, cfg)
    if lc:
        L.append("  length     qty   per strip     shave off   would become")
        for r in lc:
            L.append(f'  {fmt(r["length"]):>9s}  {r["qty"]:4d}   '
                     f'{r["per_strip_now"]} -> {r["per_strip_after"]}'
                     f'           {r["shave"]/GRID:.4f}"    {r["target"]/GRID:.4f}"')
    else:
        L.append("  none")

    dc = dimension_clusters(demand, cfg)
    L.append("\n" + "=" * 72)
    L.append("NEAR-DUPLICATE DIMENSIONS — unify these and stop paying for the difference")
    L.append("=" * 72)
    stop = (cfg.min_per_track_rip + cfg.extra_min_per_track_stop_change) \
        * cfg.dollars_per_min()
    if dc["widths"]:
        L.append(f'Strip widths (each distinct width costs a track stop setting, '
                 f'${stop:.2f}):')
        for g in dc["widths"]:
            vals = ", ".join(fmt(v) for v in g["values"])
            L.append(f'  {vals}')
            L.append(f'     -> unify to {fmt(g["unify_to"])}: {g["qty"]} parts, '
                     f'saves up to ${g["saves"]:,.0f}')
    if dc["lengths"]:
        mc2 = cfg.extra_min_per_mitre_stop_change * cfg.dollars_per_min()
        L.append(f'\nCrosscut lengths (each distinct length costs a mitre stop setting, '
                 f'${mc2:.2f}):')
        for g in dc["lengths"][:8]:
            vals = ", ".join(fmt(v) for v in g["values"])
            L.append(f'  {vals}')
            L.append(f'     -> unify to {fmt(g["unify_to"])}: {g["qty"]} parts, '
                     f'saves up to ${g["saves"]:,.0f}')
    if not dc["widths"] and not dc["lengths"]:
        L.append("  none")
    return "\n".join(L)
