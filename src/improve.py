"""Ruin-and-recreate local search over the real cost function.

The greedy strip builder has a classic end-game problem: the parts that pack well are
consumed first, so low-quantity leftovers each end up alone in a barely-filled strip.
Releasing the worst sheets and rebuilding them together is what fixes that, and because
the acceptance test is the full weighted score, the search trades material against saw
setups on Matt's own numbers rather than on a proxy.
"""
from __future__ import annotations

import random
from collections import Counter

from .cost import Score, score
from .heuristic import solve_thickness
from .model import CutConfig, PartType, Pattern, Placement, Strip
from .validate import LayoutError, check_job


def _p_accept(delta: float, temp0: float, it: int, iters: int) -> float:
    import math
    temp = temp0 * (1.0 - it / max(1, iters))
    if temp <= 1e-9:
        return 0.0
    return math.exp(-delta / temp)


def _utilisation(p: Pattern, cfg: CutConfig) -> float:
    across, along = p.sheet_dims(cfg)
    used = sum(pl.width * pl.length for s in p.strips for pl in s.placements)
    return used / (across * along)


def improve(patterns: list[Pattern], demand: list[PartType], cfg: CutConfig,
            rng: random.Random, iters: int = 60, ruin_frac: float = 0.3,
            temp0: float = 0.0, **solve_kw) -> tuple[list[Pattern], Score]:
    by_t: dict[float, list[PartType]] = {}
    for pt in demand:
        by_t.setdefault(pt.thickness, []).append(pt)

    best = list(patterns)
    best_sc = score(best, cfg)
    cur, cur_sc = best, best_sc

    for it in range(iters):
        # alternate between rebuilding whole sheets and draining an under-filled strip
        if rng.random() < 0.35:
            cand = relocate(cur, cfg, rng)
            if cand is None:
                continue
            try:
                check_job(cand, demand, cfg)
                cand_sc = score(cand, cfg)
            except (LayoutError, ValueError):
                continue
            if cand_sc.dollars < cur_sc.dollars:
                cur, cur_sc = cand, cand_sc
                if cand_sc.dollars < best_sc.dollars:
                    best, best_sc = cand, cand_sc
            continue

        thickness = rng.choice(list(by_t))
        group = [p for p in cur if abs(p.thickness - thickness) < 1e-9]
        others = [p for p in cur if abs(p.thickness - thickness) >= 1e-9]
        if len(group) < 2:
            continue

        # bias the ruin toward the worst-utilised sheets, but keep it stochastic
        k = max(2, int(len(group) * ruin_frac))
        ranked = sorted(group, key=lambda p: _utilisation(p, cfg))
        pool = ranked[:min(len(ranked), k * 2)]
        rng.shuffle(pool)
        ruined = pool[:k]
        kept = [p for p in group if p not in ruined]

        released: Counter = Counter()
        for p in ruined:
            for pt, n in p.part_counts().items():
                released[pt] += n
        if not released:
            continue

        try:
            rebuilt = solve_thickness(by_t[thickness], cfg, rng,
                                      qty=dict(released), **solve_kw)
            cand = others + kept + rebuilt
            check_job(cand, demand, cfg)
            cand_sc = score(cand, cfg)
        except (LayoutError, ValueError):
            continue

        # accept improvements always; accept small regressions early on so the search
        # can climb out of the greedy's local optimum
        delta = cand_sc.dollars - cur_sc.dollars
        if delta < 0 or (temp0 > 0 and rng.random() < _p_accept(delta, temp0, it, iters)):
            cur, cur_sc = cand, cand_sc
            if cand_sc.dollars < best_sc.dollars:
                best, best_sc = cand, cand_sc

    return best, best_sc


def _clone(patterns: list[Pattern]) -> list[Pattern]:
    """Copy the layout structure, keeping PartType references shared."""
    out = []
    for p in patterns:
        q = Pattern(thickness=p.thickness, swapped=p.swapped)
        for s in p.strips:
            t = Strip(width=s.width)
            t.placements = [Placement(part=pl.part, length=pl.length, width=pl.width,
                                      offset=pl.offset) for pl in s.placements]
            q.strips.append(t)
        out.append(q)
    return out


def _renumber(s: Strip, cfg: CutConfig) -> None:
    """Re-lay a strip's parts end to end, longest first, leftover at the far end."""
    s.placements.sort(key=lambda pl: -pl.length)
    off = 0
    for pl in s.placements:
        pl.offset = off
        off += pl.length + cfg.kerf_cross


def _shrink(p: Pattern) -> None:
    """Normal patterns: a strip need be no wider than the widest part it holds."""
    for s in p.strips:
        if s.placements:
            s.width = max(pl.width for pl in s.placements)


def relocate(patterns: list[Pattern], cfg: CutConfig, rng: random.Random
             ) -> list[Pattern] | None:
    """Empty the worst-filled strip into slack elsewhere, then drop what it leaves behind.

    A strip carrying one short part wastes its whole remaining length. If those parts
    fit the leftover length of other strips (widths permitting, at the price of a trim
    rip), moving them can delete a strip outright — and sometimes a whole sheet.
    """
    cand = _clone(patterns)
    strips = [(pi, si, s) for pi, p in enumerate(cand) for si, s in enumerate(p.strips)]
    if len(strips) < 2:
        return None

    def fill(s, p):
        _, along = p.sheet_dims(cfg)
        return s.used_length(cfg) / along

    ranked = sorted(strips, key=lambda t: fill(t[2], cand[t[0]]))
    pool = ranked[:max(1, len(ranked) // 4)]
    src_pi, src_si, src = pool[rng.randrange(len(pool))]

    moved = 0
    for pl in list(src.placements):
        for pi, si, dst in strips:
            if (pi, si) == (src_pi, src_si) or not dst.placements:
                continue
            if abs(cand[pi].thickness - cand[src_pi].thickness) > 1e-9:
                continue
            if dst.width < pl.width:
                continue
            _, along = cand[pi].sheet_dims(cfg)
            need = pl.length + (cfg.kerf_cross if dst.placements else 0)
            if dst.used_length(cfg) + need > along:
                continue
            dst.placements.append(pl)
            _renumber(dst, cfg)
            src.placements.remove(pl)
            moved += 1
            break
    if not moved:
        return None

    for p in cand:
        p.strips = [s for s in p.strips if s.placements]
        for s in p.strips:
            _renumber(s, cfg)
        _shrink(p)
    out = [p for p in cand if p.strips]
    return out or None
