"""Heuristic solver: greedy strip formation by area density, then bin-packing of strips.

Two levels, as in the plan, but the levels are no longer solved in isolation — that
independence was what cost 25% of the sheet.

Strip formation: at each step every candidate strip width is costed (a knapsack fills a
strip of that width from the eligible remaining parts) and the strip with the best *area
density* is emitted. Density is part area over the sheet area the strip consumes, so a
half-empty strip and an over-wide strip are both penalised, and the choice between them
is made on the same scale.

Non-exact 2-stage means a strip of width W may hold any part of width <= W, with a third
trim rip. Once filled, a strip is shrunk to the widest part it actually holds — the
normal-patterns argument — which recovers the unused width and cancels the trim cuts for
parts already at that width.

Sheet packing: seed each sheet with the widest remaining strip, then fill the rest by
knapsack over blocks of equal-width strips, so a track saw stop change is charged once
per distinct width rather than once per rip.
"""
from __future__ import annotations

import random
from collections import Counter

import numpy as np

from .knapsack import np_knapsack, split_groups
from .model import CutConfig, PartType, Pattern, Placement, Strip

NEG = -1e18


def _variants(types: list[PartType], cfg: CutConfig) -> list[tuple]:
    """Every legal (part, width, length) orientation with its remaining quantity."""
    out = []
    for pt in types:
        for w, l in pt.variants():
            if w <= cfg.usable_w and l <= cfg.usable_l:
                out.append((pt, w, l))
    return out


def _fill_strip(width: int, cands: list[tuple], remaining: Counter, cfg: CutConfig,
                rng: random.Random, jitter: float, trim_units: float = 0.0,
                fill_mode: bool = False):
    """Knapsack-fill one strip of the given width. Returns (placements, used_length)."""
    elig = [(i, pt, w, l) for i, (pt, w, l) in enumerate(cands)
            if w <= width and remaining[pt] > 0]
    if not elig:
        return None
    # one orientation per part per strip decision: keep the longest that still fits
    by_part: dict[PartType, tuple] = {}
    for i, pt, w, l in elig:
        prev = by_part.get(pt)
        if prev is None or (w, l) > (prev[2], prev[3]):
            by_part[pt] = (i, pt, w, l)
    elig = list(by_part.values())

    # Value is *useful area* expressed in strip-length units, not length filled:
    # a part of width w in a strip of width W wastes (W-w) along its whole length, which
    # is l*(W-w)/W of equivalent strip. So val = l*w/W, plus the kerf it saves by
    # sharing a cut, less the cost of the trim rip it needs when w < W.
    items = []
    for _, pt, w, l in elig:
        size = l + cfg.kerf_mitre_saw
        if fill_mode:
            # Maximise raw length consumed instead of useful area. This is the only
            # way to land on an EXACTLY filled strip, and an exactly filled strip
            # needs one fewer crosscut: the last part's far end is the sheet's own
            # factory edge, already square. Worth a whole track-saw cut on a wide
            # strip, so it is tried alongside the area-optimal fill and the better of
            # the two wins on true density.
            val = float(size)
        else:
            val = l * (w / width) + cfg.kerf_mitre_saw
            if w < width:
                val -= trim_units
        if jitter:
            val *= 1.0 + rng.uniform(-jitter, jitter)
        items.append((size, max(val, 1e-6), remaining[pt]))

    sizes, values, owner = split_groups(items)
    if not sizes:
        return None
    _, _, chosen = np_knapsack(sizes, values, cfg.usable_l + cfg.kerf_mitre_saw)

    counts: Counter = Counter()
    for g in chosen:
        idx, take = owner[g]
        counts[idx] += take
    if not counts:
        return None

    # longest parts first so the leftover ends up at the far end of the strip
    order = sorted(counts, key=lambda i: -elig[i][3])
    placements, offset = [], 0
    for i in order:
        _, pt, w, l = elig[i]
        for _ in range(counts[i]):
            placements.append(Placement(part=pt, length=l, width=w, offset=offset))
            offset += l + cfg.kerf_mitre_saw
    return placements, offset - cfg.kerf_mitre_saw


def _strip_value(placements, width: int, cfg: CutConfig, thickness: float,
                 dollars_per_area: float, trim_cost: float, stop_cost: float,
                 used_widths: set[int] | None) -> tuple[float, int]:
    """Score one candidate strip in dollars per unit of sheet area it consumes.

    A strip occupies `shrunk x usable_l` of sheet whether or not it is well filled, so
    density is the right basis for comparing a narrow well-packed strip against a wide
    half-empty one.
    """
    shrunk = max(p.width for p in placements)          # normal patterns
    part_area = sum(p.width * p.length for p in placements)
    trims = sum(1 for p in placements if p.width < shrunk)

    # Every part needs a crosscut wherever it sits, so the full cut cost is not a
    # differential: only the *excess* from being forced onto the track saw
    # distinguishes one strip choice from another.
    wide = shrunk > cfg.mitre_max_crosscut_width
    per_cut = cfg.min_per_track_crosscut if wide else cfg.min_per_mitre_crosscut
    excess = max(0.0, cfg.min_per_track_crosscut - cfg.min_per_mitre_crosscut) \
        if wide else 0.0
    cut_cost = len(placements) * excess * cfg.dollars_per_min()

    # An exactly filled strip needs one crosscut fewer, because the last part's far
    # end is the sheet's own factory edge. On a wide strip that is a whole track-saw
    # cut -- the single largest per-operation cost in the model.
    used = sum(p.length for p in placements) + \
        (len(placements) - 1) * cfg.kerf_mitre_saw
    if used == cfg.usable_l:
        cut_cost -= per_cut * cfg.dollars_per_min()

    rip_cost = (cfg.min_per_track_rip + cfg.min_per_strip_handling) \
        * cfg.dollars_per_min()
    # Opening a width not yet used in this job costs a track saw stop setting.
    # Charging it here is what makes construction prefer widths it already has, which
    # cuts stop changes and, because reused widths absorb more parts, strip count too.
    if used_widths is not None and shrunk not in used_widths:
        rip_cost += stop_cost

    value = (part_area * dollars_per_area - trims * trim_cost - cut_cost - rip_cost)
    return value / (shrunk * cfg.usable_l), shrunk


def _build_strips(types: list[PartType], cfg: CutConfig, rng: random.Random,
                  jitter: float, trim_weight: float,
                  qty: dict[PartType, int] | None = None,
                  max_strip_width: int | None = None,
                  exact_fill_probe: bool = True,
                  width_reuse: bool = True) -> list[Strip]:
    cands = _variants(types, cfg)
    remaining = Counter(qty if qty is not None else {pt: pt.qty for pt in types})
    thickness = types[0].thickness
    sheet_area = cfg.usable_w * cfg.usable_l
    dollars_per_area = cfg.cost_of_sheet(thickness) / sheet_area
    trim_cost = (cfg.min_per_trim_rip + cfg.extra_min_per_trim_stop_change * 0.25) \
        * cfg.dollars_per_min() * trim_weight
    stop_cost = (cfg.min_per_track_rip + cfg.extra_min_per_track_stop_change) \
        * cfg.dollars_per_min()

    strips: list[Strip] = []
    used_widths: set[int] = set()
    while sum(remaining.values()) > 0:
        widths = sorted({w for pt, w, l in cands if remaining[pt] > 0}, reverse=True)
        if max_strip_width is not None:
            # Cap strip width so crosscuts stay on the cheap saw. Parts whose smaller
            # dimension exceeds the cap cannot obey it, so once the capped widths are
            # exhausted the cap is lifted for whatever is left.
            capped = [w for w in widths if w <= max_strip_width]
            widths = capped or widths

        reuse = used_widths if width_reuse else None

        def evaluate(width, fill_mode):
            tu = trim_cost / (cfg.cost_of_sheet(thickness) * width / sheet_area)
            got = _fill_strip(width, cands, remaining, cfg, rng, jitter, tu, fill_mode)
            if got is None:
                return None
            placements, _ = got
            density, shrunk = _strip_value(
                placements, width, cfg, thickness, dollars_per_area, trim_cost,
                stop_cost, reuse)
            return density, shrunk, placements

        best = None
        for width in widths:
            cand = evaluate(width, False)
            if cand is not None and (best is None or cand[0] > best[0]):
                best = cand
                best_width = width

        # Probe for an exactly filled strip on the winning width ONLY. Doing it for
        # every candidate width made each iteration 2.5x more expensive, and the extra
        # quality did not come close to paying for the iterations it cost.
        if exact_fill_probe and best is not None:
            alt = evaluate(best_width, True)
            if alt is not None and alt[0] > best[0]:
                best = alt

        if best is None:
            # Nothing could be placed while parts remain. Fail loudly: silently
            # returning a short layout produces something that scores *better* for
            # having less to cut, and only the validator would catch it.
            left = {pt.label: n for pt, n in remaining.items() if n > 0}
            raise ValueError(f"could not place remaining parts: {left}")

        _, shrunk, placements = best
        strips.append(Strip(width=shrunk, placements=placements))
        used_widths.add(shrunk)
        for p in placements:
            remaining[p.part] -= 1
    return strips


def _pack_sheets(strips: list[Strip], thickness: float, cfg: CutConfig,
                 rng: random.Random) -> list[Pattern]:
    """Bin-pack strips across the sheet width, widest first so fillers stay available."""
    cap = cfg.usable_w + cfg.kerf_track_saw
    stop_penalty = (cfg.extra_min_per_track_stop_change * cfg.dollars_per_min()
                    / (cfg.cost_of_sheet(thickness) / cfg.usable_w))

    pool: dict[int, list[Strip]] = {}
    for s in strips:
        pool.setdefault(s.width, []).append(s)

    patterns: list[Pattern] = []
    while any(pool.values()):
        # seed with the widest strip: large items first, small ones kept as fillers
        seed_w = max(w for w, v in pool.items() if v)
        pat = Pattern(thickness=thickness)
        pat.strips.append(pool[seed_w].pop())
        if not pool[seed_w]:
            del pool[seed_w]
        room = cap - (seed_w + cfg.kerf_track_saw)

        while room > 0:
            widths = [w for w, v in pool.items() if v and w + cfg.kerf_track_saw <= room]
            if not widths:
                break
            items, owner_w = [], []
            for w in widths:
                maxm = min(len(pool[w]), room // (w + cfg.kerf_track_saw))
                if maxm < 1:
                    continue
                for m in range(1, maxm + 1):
                    size = m * (w + cfg.kerf_track_saw)
                    bonus = stop_penalty if w == seed_w else 0.0
                    items.append((size, float(size) - stop_penalty + bonus, 1))
                    owner_w.append((w, m))
            if not items:
                break
            sizes, values, owner = split_groups(items)
            _, _, chosen = np_knapsack(sizes, values, room)
            # keep at most one block per width
            take: dict[int, int] = {}
            for g in chosen:
                idx, _ = owner[g]
                w, m = owner_w[idx]
                take[w] = max(take.get(w, 0), m)
            if not take:
                break
            progressed = False
            for w, m in sorted(take.items(), reverse=True):
                for _ in range(min(m, len(pool.get(w, [])))):
                    if room < w + cfg.kerf_track_saw:
                        break
                    pat.strips.append(pool[w].pop())
                    room -= w + cfg.kerf_track_saw
                    progressed = True
                if w in pool and not pool[w]:
                    del pool[w]
            if not progressed:
                break

        patterns.append(pat)
    return patterns


def solve_thickness(types: list[PartType], cfg: CutConfig, rng: random.Random,
                    jitter: float = 0.0, trim_weight: float = 1.0,
                    qty: dict[PartType, int] | None = None,
                    max_strip_width: int | None = None,
                    exact_fill_probe: bool = True,
                    width_reuse: bool = True) -> list[Pattern]:
    strips = _build_strips(types, cfg, rng, jitter, trim_weight, qty, max_strip_width,
                           exact_fill_probe, width_reuse)
    return _pack_sheets(strips, types[0].thickness, cfg, rng)


def solve(demand: list[PartType], cfg: CutConfig, rng: random.Random, **kw
          ) -> list[Pattern]:
    by_t: dict[float, list[PartType]] = {}
    for pt in demand:
        by_t.setdefault(pt.thickness, []).append(pt)
    out: list[Pattern] = []
    for t in sorted(by_t, reverse=True):
        out.extend(solve_thickness(by_t[t], cfg, rng, **kw))
    return out
