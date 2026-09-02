"""Job-level scoring.

Sheets are cut one at a time, so almost every cost term is a property of a single
pattern. The one cross-sheet term is the track saw stop: if the last width ripped on
one sheet matches a width on the next, that stop setting carries over for free. We
sequence sheets greedily to harvest those carryovers.
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import CutConfig, Pattern


@dataclass
class Score:
    dollars: float
    material: float
    labour: float
    minutes: float
    n_sheets: int
    sheets_by_thickness: dict
    n_rips: int
    n_track_stops: int
    n_adjacent_equal_rips: int
    n_cross: int
    n_wide_cross: int
    n_trims: int
    n_trim_stops: int
    n_mitre_stops: int
    n_parts: int
    used_area: int
    sheet_order: list

    def __str__(self) -> str:
        return (f"${self.dollars:,.2f}  ({self.n_sheets} sheets ${self.material:,.0f} + "
                f"{self.minutes/60:.1f}h ${self.labour:,.0f})  "
                f"rips={self.n_rips} stops={self.n_track_stops} "
                f"adj={self.n_adjacent_equal_rips} cross={self.n_cross} trims={self.n_trims}")


def sequence_rips(patterns: list[Pattern], cfg: CutConfig) -> tuple[list[int], int]:
    """Greedily order sheets to reuse the track saw stop across sheet boundaries.

    Returns (sheet order, total stop settings). Within a sheet the rips are grouped by
    width, so a sheet needs one stop setting per distinct width, less one if the
    incoming setting already matches one of its widths.
    """
    remaining = list(range(len(patterns)))
    width_sets = [set(p.counts(cfg)["widths"]) for p in patterns]
    order: list[int] = []
    stops = 0
    current: int | None = None

    while remaining:
        # prefer a sheet that can reuse the current stop setting
        pick = next((i for i in remaining if current is not None and current in width_sets[i]),
                    None)
        if pick is None:
            pick = max(remaining, key=lambda i: len(width_sets[i]))
        remaining.remove(pick)
        ws = width_sets[pick]
        stops += len(ws) - (1 if current is not None and current in ws else 0)
        # leave the stop on a width the next sheet is most likely to want
        if remaining:
            shared = [w for w in ws if any(w in width_sets[j] for j in remaining)]
            current = shared[0] if shared else next(iter(ws))
        else:
            current = next(iter(ws))
        order.append(pick)

    return order, stops


def score(patterns: list[Pattern], cfg: CutConfig) -> Score:
    if not patterns:
        raise ValueError("no patterns to score")

    order, n_track_stops = sequence_rips(patterns, cfg)

    material = 0.0
    by_thick: dict[float, int] = {}
    agg = dict(n_rips=0, n_cross=0, n_wide_cross=0, n_trims=0, n_trim_widths=0,
               mitre_stops=0, n_parts=0, used_area=0)

    for p in patterns:
        material += cfg.cost_of_sheet(p.thickness)
        by_thick[p.thickness] = by_thick.get(p.thickness, 0) + 1
        c = p.counts(cfg)
        for k in agg:
            agg[k] += c[k]

    minutes = (
        cfg.t_rip * agg["n_rips"]
        + cfg.t_track_stop * n_track_stops
        + cfg.t_cross * agg["n_cross"]
        + cfg.t_wide_cross * agg["n_wide_cross"]
        + cfg.t_mitre_stop * agg["mitre_stops"]
        + cfg.t_trim * agg["n_trims"]
        + cfg.t_trim_stop * agg["n_trim_widths"]
        + cfg.t_sheet_handling * len(patterns)
        + cfg.t_strip_handling * sum(len(p.strips) for p in patterns)
    )
    labour = minutes * cfg.dollars_per_min()

    return Score(
        dollars=material + labour,
        material=material,
        labour=labour,
        minutes=minutes,
        n_sheets=len(patterns),
        sheets_by_thickness=by_thick,
        n_rips=agg["n_rips"],
        n_track_stops=n_track_stops,
        n_adjacent_equal_rips=agg["n_rips"] - n_track_stops,
        n_cross=agg["n_cross"],
        n_wide_cross=agg["n_wide_cross"],
        n_trims=agg["n_trims"],
        n_trim_stops=agg["n_trim_widths"],
        n_mitre_stops=agg["mitre_stops"],
        n_parts=agg["n_parts"],
        used_area=agg["used_area"],
        sheet_order=order,
    )
