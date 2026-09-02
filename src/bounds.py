"""Lower bounds on total cost.

Sheet count has a clean lower bound: area forbids fewer. Total *cost* has no such
clean bound, because the labour terms depend on how parts are grouped. What follows is
therefore two numbers, labelled honestly:

  hard    — provable. Every term is forced by the parts alone, whatever the layout.
  working — the hard floor plus terms that are unavoidable in practice but rest on a
            stated assumption. Still an underestimate: several real costs (rips, stop
            changes, handling, changeovers) have no useful lower bound and are simply
            left out, so the true optimum sits somewhere above this.
"""
from __future__ import annotations

from math import ceil

from .model import GRID, CutConfig, PartType


def sheet_floor(demand: list[PartType], cfg: CutConfig) -> dict[float, int]:
    """Minimum sheets per thickness. Provable: area alone forbids fewer."""
    sheet = cfg.usable_w * cfg.usable_l
    area: dict[float, int] = {}
    for pt in demand:
        area[pt.thickness] = area.get(pt.thickness, 0) + pt.area * pt.qty
    return {t: ceil(a / sheet) for t, a in area.items()}


def crosscut_split(demand: list[PartType], cfg: CutConfig) -> tuple[int, int]:
    """(parts forced onto the track saw, parts that can reach the mitre saw).

    A part's strip is at least as wide as its smaller dimension, so that dimension
    decides which saw can crosscut it. No layout changes this.
    """
    forced = reach = 0
    for pt in demand:
        legal = [min(w, l) for w, l in pt.variants()
                 if w <= cfg.usable_w and l <= cfg.usable_l]
        if not legal:
            continue
        if min(legal) > cfg.mitre_max_crosscut_width:
            forced += pt.qty
        else:
            reach += pt.qty
    return forced, reach


def max_wide_strips(demand: list[PartType], cfg: CutConfig, sheets: int) -> int:
    """Upper bound on strips wider than the mitre capacity, across `sheets` sheets.

    Used only to bound how many forced crosscuts could be dodged by a part ending
    flush with its strip, which is the one way a part avoids its own crosscut.
    """
    per_sheet = (cfg.usable_w + cfg.kerf_track_saw) // \
        (cfg.mitre_max_crosscut_width + 1 + cfg.kerf_track_saw)
    return max(0, per_sheet * sheets)


def cost_floor(demand: list[PartType], cfg: CutConfig) -> dict:
    floors = sheet_floor(demand, cfg)
    min_sheets = sum(floors.values())
    material = sum(cfg.cost_of_sheet(t) * n for t, n in floors.items())

    dpm = cfg.dollars_per_min()
    setup_min = min_sheets * cfg.min_per_sheet_setup

    forced, reach = crosscut_split(demand, cfg)
    # A part avoids its own crosscut only by ending flush with its strip, and at most
    # one part per strip can do that. Bounding the number of wide strips therefore
    # bounds how many of the forced crosscuts could be free.
    dodgeable = min(forced, max_wide_strips(demand, cfg, min_sheets))
    hard_track_min = (forced - dodgeable) * cfg.min_per_track_crosscut
    work_track_min = forced * cfg.min_per_track_crosscut
    work_mitre_min = reach * cfg.min_per_mitre_crosscut

    hard = material + (setup_min + hard_track_min) * dpm
    working = material + (setup_min + work_track_min + work_mitre_min) * dpm

    return {
        "sheet_floor": floors, "min_sheets": min_sheets,
        "material": material,
        "setup_min": setup_min,
        "forced_track_parts": forced, "mitre_reachable_parts": reach,
        "dodgeable": dodgeable,
        "hard_track_min": hard_track_min,
        "work_track_min": work_track_min, "work_mitre_min": work_mitre_min,
        "hard": hard, "working": working,
    }


def floor_report(demand: list[PartType], cfg: CutConfig, actual=None) -> str:
    f = cost_floor(demand, cfg)
    dpm = cfg.dollars_per_min()
    W = 58
    L = ["COST FLOOR", "=" * (W + 12)]

    def row(label, dollars, indent=2):
        L.append(f'{" " * indent}{label:<{W - indent}s}${dollars:>10,.2f}')

    sheets = " ".join(f'{t}"x{n}' for t, n in sorted(f["sheet_floor"].items(),
                                                     reverse=True))
    L.append("provable - forced by the parts alone, whatever the layout:")
    row(f'material, {f["min_sheets"]} sheets minimum ({sheets})', f["material"])
    row(f'sheet setup, {f["min_sheets"]} x {cfg.min_per_sheet_setup:g} min',
        f["setup_min"] * dpm)
    row(f'track crosscuts, >={f["forced_track_parts"] - f["dodgeable"]} of '
        f'{f["forced_track_parts"]} forced parts', f["hard_track_min"] * dpm)
    row("HARD FLOOR", f["hard"], indent=0)
    L.append("")
    L.append("working floor - adds terms unavoidable in practice, assuming each part")
    L.append("needs its own crosscut (the champion does exactly one per part):")
    row(f'track crosscuts, {f["forced_track_parts"]} parts x '
        f'{cfg.min_per_track_crosscut:g} min', f["work_track_min"] * dpm)
    row(f'mitre crosscuts, {f["mitre_reachable_parts"]} parts x '
        f'{cfg.min_per_mitre_crosscut:g} min', f["work_mitre_min"] * dpm)
    row("WORKING FLOOR", f["working"], indent=0)
    L.append("")
    L.append("excluded for lack of any useful bound (so the floor is conservative):")
    L.append("  rips, track/mitre/trim stop changes, strip handling, saw changeovers")
    if actual is not None:
        gap = actual.dollars - f["working"]
        L.append("")
        row("champion", actual.dollars, indent=0)
        L.append(f'{"gap above working floor":<{W}s}${gap:>10,.2f}   '
                 f'({gap / f["working"] * 100:.0f}% above)')
    return "\n".join(L)
