"""Geometry output -> canonical PartType demand list."""
from __future__ import annotations

from collections import Counter

from .geometry import extract_parts
from .model import GRID, CutConfig, PartType, fmt, to_units


def load_demand(step_path: str, cfg: CutConfig, oversize: float = 0.0) -> list[PartType]:
    """Extract, round up to the grid, canonicalise w<=l, and dedupe into quantities."""
    raw = extract_parts(step_path)
    counter: Counter = Counter()
    for p in raw:
        w = to_units(p.width + oversize)
        l = to_units(p.length + oversize)
        if w > l:
            w, l = l, w
        counter[(w, l, round(p.thickness, 4))] += 1

    demand = [PartType(w=w, l=l, thickness=t, qty=n,
                       label=f"{fmt(w)}x{fmt(l)}")
              for (w, l, t), n in sorted(counter.items(), key=lambda kv: -kv[0][0] * kv[0][1])]

    bad = [pt for pt in demand
           if not (_fits(pt, cfg))]
    if bad:
        lines = [f"  {pt.label} @{pt.thickness}\" x{pt.qty}" for pt in bad]
        raise ValueError(
            f"parts cannot be produced from a {fmt(cfg.usable_w)}x{fmt(cfg.usable_l)} "
            f"usable sheet:\n" + "\n".join(lines))
    return demand


def _fits(pt: PartType, cfg: CutConfig) -> bool:
    for w, l in pt.variants():
        if w <= cfg.usable_w and l <= cfg.usable_l:
            return True
    return False


def area_bound(demand: list[PartType], cfg: CutConfig) -> dict[float, float]:
    """Pure area lower bound on sheets per thickness. Ignores kerf, so optimistic."""
    out: dict[float, float] = {}
    sheet = cfg.usable_w * cfg.usable_l
    for pt in demand:
        out[pt.thickness] = out.get(pt.thickness, 0.0) + pt.area * pt.qty / sheet
    return out


def summarise(demand: list[PartType], cfg: CutConfig) -> str:
    lines = []
    by_t: dict[float, list[PartType]] = {}
    for pt in demand:
        by_t.setdefault(pt.thickness, []).append(pt)
    bounds = area_bound(demand, cfg)
    for t in sorted(by_t, reverse=True):
        pts = by_t[t]
        n = sum(p.qty for p in pts)
        area = sum(p.area * p.qty for p in pts) / (GRID * GRID * 144)
        lines.append(f'{t}" ply: {n} parts, {len(pts)} distinct types, '
                     f'{area:.1f} sqft, area floor {bounds[t]:.2f} sheets')
    return "\n".join(lines)


def apply_shave(demand: list[PartType], cfg: CutConfig, max_shave: int) -> list[PartType]:
    """Shrink part widths that only just miss fitting another strip per sheet.

    Purely a what-if: it changes the parts, so it is never applied silently. Used to
    price how much a tiny dimensional concession is worth.
    """
    cap = cfg.usable_w + cfg.kerf_track_saw

    def shaved(w: int) -> int:
        k = cap // (w + cfg.kerf_track_saw)
        if k < 1:
            return w
        target = cap // (k + 1) - cfg.kerf_track_saw
        if 0 < w - target <= max_shave:
            return target
        return w

    out = []
    for pt in demand:
        w, l = shaved(pt.w), pt.l
        if w > l:
            w, l = l, w
        out.append(PartType(w=w, l=l, thickness=pt.thickness, qty=pt.qty,
                            rotatable=pt.rotatable, label=f"{fmt(w)}x{fmt(l)}"))
    # re-dedupe: shaving can collapse two types into one
    merged: Counter = Counter()
    for pt in out:
        merged[(pt.w, pt.l, pt.thickness)] += pt.qty
    return [PartType(w=w, l=l, thickness=t, qty=n, label=f"{fmt(w)}x{fmt(l)}")
            for (w, l, t), n in sorted(merged.items(), key=lambda kv: -kv[0][0] * kv[0][1])]
