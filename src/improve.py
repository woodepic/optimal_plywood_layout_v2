"""Local search over the real cost function.

Restarts and iteration depth were both measured to saturate around $2,205, which means
the ceiling belongs to the construction heuristic and to the set of moves available
here — not to how long either runs. So this module is a set of moves, weighted, all
accepted or rejected on the true weighted score.

The moves divide into two kinds. *Rebuild* moves throw work back at the construction
heuristic and are expensive (~0.7 s each). *Surgical* moves rearrange an existing
layout directly and cost microseconds, so most iterations should be surgical.

Each move returns a fresh layout or None if it had nothing to do. None of them may
mutate the layout they are given.
"""
from __future__ import annotations

import math
import random
from collections import Counter

from .cost import Score, score
from .heuristic import solve_thickness
from .model import CutConfig, PartType, Pattern, Placement, Strip
from .validate import LayoutError, check_job


# --- shared helpers -------------------------------------------------------------

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
    """Re-lay a strip's parts end to end, longest first.

    Length-sorting is load-bearing, not cosmetic: the mitre stop only reaches a strip's
    end, so only *consecutive* equal lengths share a setting. Sorting is what makes
    equal lengths adjacent.
    """
    s.placements.sort(key=lambda pl: -pl.length)
    off = 0
    for pl in s.placements:
        pl.offset = off
        off += pl.length + cfg.kerf_mitre_saw


def _shrink(p: Pattern) -> None:
    """Normal patterns: a strip need be no wider than the widest part it holds."""
    for s in p.strips:
        if s.placements:
            s.width = max(pl.width for pl in s.placements)


def _compact(patterns: list[Pattern], cfg: CutConfig) -> list[Pattern] | None:
    """Drop empty strips and empty sheets, then re-lay and shrink what is left."""
    out = []
    for p in patterns:
        p.strips = [s for s in p.strips if s.placements]
        if not p.strips:
            continue
        for s in p.strips:
            _renumber(s, cfg)
        _shrink(p)
        out.append(p)
    return out or None


def _length_slack(s: Strip, p: Pattern, cfg: CutConfig) -> int:
    _, along = p.sheet_dims(cfg)
    return along - s.used_length(cfg)


def _width_slack(p: Pattern, cfg: CutConfig) -> int:
    across, _ = p.sheet_dims(cfg)
    used = sum(s.width for s in p.strips) + max(0, len(p.strips) - 1) * cfg.kerf_track_saw
    return across - used


def _utilisation(p: Pattern, cfg: CutConfig) -> float:
    across, along = p.sheet_dims(cfg)
    used = sum(pl.width * pl.length for s in p.strips for pl in s.placements)
    return used / (across * along)


def _all_strips(patterns: list[Pattern]):
    return [(pi, si) for pi, p in enumerate(patterns) for si in range(len(p.strips))]


# --- surgical moves -------------------------------------------------------------

def move_part(patterns, cfg, rng, **_):
    """Move one part into another strip that can take it."""
    cand = _clone(patterns)
    slots = _all_strips(cand)
    if len(slots) < 2:
        return None
    rng.shuffle(slots)
    for spi, ssi in slots:
        src = cand[spi].strips[ssi]
        if not src.placements:
            continue
        pl = src.placements[rng.randrange(len(src.placements))]
        for dpi, dsi in slots:
            if (dpi, dsi) == (spi, ssi):
                continue
            if abs(cand[dpi].thickness - cand[spi].thickness) > 1e-9:
                continue
            dst = cand[dpi].strips[dsi]
            if dst.width < pl.width or not dst.placements:
                continue
            if _length_slack(dst, cand[dpi], cfg) < pl.length + cfg.kerf_mitre_saw:
                continue
            src.placements.remove(pl)
            dst.placements.append(pl)
            return _compact(cand, cfg)
    return None


def swap_parts(patterns, cfg, rng, **_):
    """Exchange two parts between strips.

    Reaches states single moves cannot: when both strips are full, nothing can move
    until something moves the other way at the same time.
    """
    cand = _clone(patterns)
    slots = _all_strips(cand)
    if len(slots) < 2:
        return None
    rng.shuffle(slots)
    for a_pi, a_si in slots[:12]:
        A = cand[a_pi].strips[a_si]
        for b_pi, b_si in slots[:12]:
            if (a_pi, a_si) == (b_pi, b_si):
                continue
            if abs(cand[a_pi].thickness - cand[b_pi].thickness) > 1e-9:
                continue
            B = cand[b_pi].strips[b_si]
            if not A.placements or not B.placements:
                continue
            pa = A.placements[rng.randrange(len(A.placements))]
            pb = B.placements[rng.randrange(len(B.placements))]
            if pb.width > A.width or pa.width > B.width:
                continue
            # length must still fit once the two have traded places
            if (A.used_length(cfg) - pa.length + pb.length >
                    cand[a_pi].sheet_dims(cfg)[1]):
                continue
            if (B.used_length(cfg) - pb.length + pa.length >
                    cand[b_pi].sheet_dims(cfg)[1]):
                continue
            A.placements.remove(pa)
            B.placements.remove(pb)
            A.placements.append(pb)
            B.placements.append(pa)
            return _compact(cand, cfg)
    return None


def merge_strips(patterns, cfg, rng, **_):
    """Fold one strip's parts into another, deleting a rip and a handling charge.

    The merged strip keeps the wider of the two widths and stays on the sheet that
    already had that width, so no extra sheet width is needed — and the donor sheet
    gets width back, which may let a strip migrate in later.
    """
    cand = _clone(patterns)
    slots = _all_strips(cand)
    if len(slots) < 2:
        return None
    # prefer emptying strips that are barely used
    slots.sort(key=lambda t: _length_slack(cand[t[0]].strips[t[1]], cand[t[0]], cfg),
               reverse=True)
    pool = slots[:max(2, len(slots) // 2)]
    rng.shuffle(pool)

    for spi, ssi in pool:
        src = cand[spi].strips[ssi]
        if not src.placements:
            continue
        need = sum(pl.length for pl in src.placements) \
            + len(src.placements) * cfg.kerf_mitre_saw
        for dpi, dsi in slots:
            if (dpi, dsi) == (spi, ssi):
                continue
            if abs(cand[dpi].thickness - cand[spi].thickness) > 1e-9:
                continue
            dst = cand[dpi].strips[dsi]
            if not dst.placements or dst.width < src.width:
                continue
            if _length_slack(dst, cand[dpi], cfg) < need:
                continue
            dst.placements.extend(src.placements)
            src.placements = []
            return _compact(cand, cfg)
    return None


def migrate_strip(patterns, cfg, rng, **_):
    """Move an entire strip to another sheet that has width to spare.

    Pure re-assignment — nothing is re-cut. This is what lets a sheet empty out and
    disappear, taking its material and setup cost with it.
    """
    cand = _clone(patterns)
    if len(cand) < 2:
        return None
    order = sorted(range(len(cand)), key=lambda i: _utilisation(cand[i], cfg))
    for spi in order[:max(1, len(order) // 2)]:
        src_pat = cand[spi]
        for ssi in sorted(range(len(src_pat.strips)),
                          key=lambda i: src_pat.strips[i].width):
            strip = src_pat.strips[ssi]
            for dpi in range(len(cand)):
                if dpi == spi:
                    continue
                if abs(cand[dpi].thickness - src_pat.thickness) > 1e-9:
                    continue
                # The destination's rip axis decides how much run a strip has. A strip
                # built for a 96" run does not fit a swapped sheet's 48". Omitting this
                # check made 19% of attempts produce uncuttable layouts.
                if strip.used_length(cfg) > cand[dpi].sheet_dims(cfg)[1]:
                    continue
                need = strip.width + (cfg.kerf_track_saw if cand[dpi].strips else 0)
                if _width_slack(cand[dpi], cfg) < need:
                    continue
                cand[dpi].strips.append(strip)
                src_pat.strips.pop(ssi)
                return _compact(cand, cfg)
    return None


def drain_strip(patterns, cfg, rng, **_):
    """Empty the worst-filled strip part by part into slack anywhere else.

    A strip carrying one short part wastes its whole remaining length; scattering those
    parts can delete the strip outright.
    """
    cand = _clone(patterns)
    slots = _all_strips(cand)
    if len(slots) < 2:
        return None

    def fill(t):
        s = cand[t[0]].strips[t[1]]
        return s.used_length(cfg) / cand[t[0]].sheet_dims(cfg)[1]

    ranked = sorted(slots, key=fill)
    spi, ssi = ranked[rng.randrange(max(1, len(ranked) // 4))]
    src = cand[spi].strips[ssi]

    moved = 0
    for pl in list(src.placements):
        for dpi, dsi in slots:
            if (dpi, dsi) == (spi, ssi):
                continue
            if abs(cand[dpi].thickness - cand[spi].thickness) > 1e-9:
                continue
            dst = cand[dpi].strips[dsi]
            if dst.width < pl.width or not dst.placements:
                continue
            if _length_slack(dst, cand[dpi], cfg) < pl.length + cfg.kerf_mitre_saw:
                continue
            src.placements.remove(pl)
            dst.placements.append(pl)
            _renumber(dst, cfg)
            moved += 1
            break
    return _compact(cand, cfg) if moved else None


# --- rebuild moves --------------------------------------------------------------

def _release(patterns, cfg, rng, thickness, frac):
    """Split a thickness group into (other thicknesses, kept sheets, released parts).

    Selection is by INDEX, never by value. Pattern is a plain dataclass, so `p in
    ruined` compares field by field — and identical sheets are common when part
    multiplicity is high. Value-based selection therefore dropped every twin of a
    ruined sheet from `kept` while releasing only one sheet's parts, losing the rest.
    """
    group = [i for i, p in enumerate(patterns)
             if abs(p.thickness - thickness) < 1e-9]
    others = [p for i, p in enumerate(patterns) if i not in set(group)]
    if len(group) < 2:
        return None
    k = max(2, int(len(group) * frac))
    ranked = sorted(group, key=lambda i: _utilisation(patterns[i], cfg))
    pool = ranked[:min(len(ranked), k * 2)]
    rng.shuffle(pool)
    ruined = set(pool[:k])
    kept = [patterns[i] for i in group if i not in ruined]
    released: Counter = Counter()
    for i in ruined:
        for pt, n in patterns[i].part_counts().items():
            released[pt] += n
    if not released:
        return None
    return others, kept, released


def _pick_thickness(patterns, by_t, rng, weighted: bool = True):
    """Choose a thickness group to rebuild.

    Weighting by sheet count sounds right -- a 4-sheet group has a fraction of the
    opportunity of a 13-sheet one -- but it also steers every rebuild at the larger
    group, where each rebuild is more expensive. Whether the better targeting pays for
    the slower iterations is measured, not assumed, so it stays switchable.
    """
    counts = {t: sum(1 for p in patterns if abs(p.thickness - t) < 1e-9) for t in by_t}
    live = [t for t, n in counts.items() if n >= 2]
    if not live:
        return None
    if not weighted:
        return live[rng.randrange(len(live))]
    return rng.choices(live, weights=[counts[t] for t in live], k=1)[0]


def ruin_recreate(patterns, cfg, rng, by_t=None, ruin_frac=0.3, solve_kw=None,
                  weight_thickness=True, **_):
    """Release the worst sheets of one thickness and rebuild them together.

    Fixes the construction's end-game, where the parts that pack well are consumed
    first and low-quantity leftovers each land alone in a barely-filled strip.
    """
    thickness = _pick_thickness(patterns, by_t, rng, weight_thickness)
    if thickness is None:
        return None
    got = _release(patterns, cfg, rng, thickness, ruin_frac)
    if got is None:
        return None
    others, kept, released = got
    rebuilt = solve_thickness(by_t[thickness], cfg, rng, qty=dict(released),
                              **(solve_kw or {}))
    return others + kept + rebuilt


def rebuild_narrow(patterns, cfg, rng, by_t=None, solve_kw=None,
                   weight_thickness=True, **_):
    """Rebuild sheets under a strip-width cap, to keep crosscuts on the cheap saw.

    A strip wider than the mitre capacity sends every one of its crosscuts to the track
    saw at many times the cost, and that is the largest single labour item. Capping
    strip width trades material for cheap cuts — a trade the construction heuristic
    will not make on its own, because it prices strips one at a time. Parts whose
    smaller dimension exceeds the cap are exempt; the cap lifts once capped widths run
    out.
    """
    thickness = _pick_thickness(patterns, by_t, rng, weight_thickness)
    if thickness is None:
        return None
    got = _release(patterns, cfg, rng, thickness, rng.choice([0.4, 0.6, 1.0]))
    if got is None:
        return None
    others, kept, released = got
    cap = cfg.mitre_max_crosscut_width
    rebuilt = solve_thickness(by_t[thickness], cfg, rng, qty=dict(released),
                              max_strip_width=cap, **(solve_kw or {}))
    return others + kept + rebuilt


def flip_axis(patterns, cfg, rng, by_t=None, solve_kw=None, **_):
    """Rebuild one sheet's parts on the other rip axis.

    Construction picks an axis per thickness group, so without this every sheet in a
    group shares one axis. This lets a single sheet defect to the other family, which
    is how mixed-axis solutions appear at all.
    """
    if not patterns:
        return None
    idx = rng.randrange(len(patterns))
    victim = patterns[idx]
    thickness = victim.thickness
    released: Counter = Counter()
    for pt, n in victim.part_counts().items():
        released[pt] += n
    if not released:
        return None
    kw = dict(solve_kw or {})
    kw.pop("swapped", None)
    try:
        rebuilt = solve_thickness(by_t[thickness], cfg, rng, qty=dict(released),
                                  swapped=not victim.swapped, **kw)
    except ValueError:
        return None
    return [p for i, p in enumerate(patterns) if i != idx] + rebuilt


# name, weight, function
# Weights are measured, not guessed -- see movestats.py, which reports how often each
# move applies at all and how often that turns into an accepted improvement. The
# striking result is that migrate_strip, which merely re-assigns a finished strip to
# another sheet, gains far more often than any move that re-cuts anything; while
# merge_strips finds nothing to do 83% of the time, because strips coming out of
# construction are already well filled along their length.
# Weights are NOT simply proportional to measured gain rate. Gain rate counts strict
# improvements, which is the wrong metric for a move whose job is diversification:
# ruin_recreate gains on only ~5% of applications, but cutting its weight from 16 to 6
# on that basis cost ~$16 at equal wall time. Its rejected moves are doing work that
# the gain counter cannot see -- carrying the search out of a basin so the cheap
# surgical moves have somewhere new to polish. Restored.
MOVES = [
    ("move_part",      14, move_part),
    ("swap_parts",     20, swap_parts),
    ("merge_strips",   10, merge_strips),   # 4 -> 10: measured 24% once strips
                                            # carry slack for it to work with
    ("migrate_strip",  22, migrate_strip),  # best gain rate in the set, ~39%
    ("drain_strip",    10, drain_strip),
    ("ruin_recreate",  16, ruin_recreate),  # diversifier: do not judge by gain rate
    ("rebuild_narrow", 14, rebuild_narrow),
    ("flip_axis",       0, flip_axis),      # zero: the swapped axis is never chosen on
                                            # this part mix, so there is nowhere to
                                            # flip to. Kept for a mix that suits it.
]


def _p_accept(delta: float, temp0: float, it: int, iters: int) -> float:
    temp = temp0 * (1.0 - it / max(1, iters))
    if temp <= 1e-9:
        return 0.0
    return math.exp(-delta / temp)


def improve(patterns: list[Pattern], demand: list[PartType], cfg: CutConfig,
            rng: random.Random, iters: int = 500, ruin_frac: float = 0.3,
            temp0: float = 0.0, stats: dict | None = None,
            weight_thickness: bool = True,
            **solve_kw) -> tuple[list[Pattern], Score]:
    by_t: dict[float, list[PartType]] = {}
    for pt in demand:
        by_t.setdefault(pt.thickness, []).append(pt)

    names = [m[0] for m in MOVES]
    weights = [m[1] for m in MOVES]
    fns = {m[0]: m[2] for m in MOVES}

    best = list(patterns)
    best_sc = score(best, cfg)
    cur, cur_sc = best, best_sc

    for it in range(iters):
        name = rng.choices(names, weights=weights, k=1)[0]
        try:
            cand = fns[name](cur, cfg, rng, by_t=by_t, ruin_frac=ruin_frac,
                             solve_kw=solve_kw,
                             weight_thickness=weight_thickness)
        except (LayoutError, ValueError):
            cand = None
        if cand is None:
            if stats is not None:
                stats[name + ":none"] = stats.get(name + ":none", 0) + 1
            continue
        try:
            check_job(cand, demand, cfg)
            cand_sc = score(cand, cfg)
        except (LayoutError, ValueError):
            if stats is not None:
                stats[name + ":invalid"] = stats.get(name + ":invalid", 0) + 1
            continue

        delta = cand_sc.dollars - cur_sc.dollars
        if delta < 0 or (temp0 > 0 and rng.random() < _p_accept(delta, temp0, it, iters)):
            cur, cur_sc = cand, cand_sc
            if stats is not None and delta < 0:
                stats[name + ":gain"] = stats.get(name + ":gain", 0) + 1
            if cand_sc.dollars < best_sc.dollars:
                best, best_sc = cand, cand_sc
        elif stats is not None:
            stats[name + ":reject"] = stats.get(name + ":reject", 0) + 1

    return best, best_sc
