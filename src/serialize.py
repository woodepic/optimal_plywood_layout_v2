"""Layout -> JSON with drawable geometry.

Everything is emitted in grid units (1/32") in each sheet's own frame: `across` is the
direction strips stack in, `along` the direction they run. The client converts to inches
for display. Cuts carry their own saw assignment, because which saw makes a cut is a
property of the strip's width, not of the drawing.
"""
from __future__ import annotations

from .bounds import cost_floor
from .cost import score, sequence_rips
from .model import CutConfig, PartType, Pattern, config_to_dict, fmt, to_inches


def _strip_rows(pat: Pattern, cfg: CutConfig):
    """Strips with their absolute offset across the sheet."""
    rows = []
    y = 0
    for i, s in enumerate(pat.strips):
        rows.append((i, s, y))
        y += s.width + cfg.kerf_track_saw
    return rows


def sheet_to_dict(pat: Pattern, cfg: CutConfig, width_colors: dict[int, int],
                  index: int) -> dict:
    across, along = pat.sheet_dims(cfg)
    rows = _strip_rows(pat, cfg)
    used_across = sum(s.width for s in pat.strips) + \
        max(0, len(pat.strips) - 1) * cfg.kerf_track_saw

    strips = []
    cuts = []
    waste = []
    kerf_area = 0
    for i, s, y in rows:
        wide = s.width > cfg.mitre_max_crosscut_width
        parts = []
        for pl in s.placements:
            parts.append({
                "x": pl.offset, "len": pl.length, "w": pl.width, "y": y,
                # Labelled AS PLACED: width across the strip first, then length along
                # it. The canonical part dimensions read the other way round for a
                # rotated part, so a 6-7/8 x 22-3/4 panel sitting 22-3/4 across its
                # strip looked as though it had been given the wrong colour.
                "label": f"{fmt(pl.width)} x {fmt(pl.length)}",
                "part_label": f"{fmt(pl.part.w)} x {fmt(pl.part.l)}",
                # Coloured by the PART's own width, not by the strip's. With non-exact
                # 2-stage a strip holds parts of many widths, so colouring by the rip
                # width painted a whole strip one colour and hid the distinction.
                "color": width_colors[pl.width],
                "trim": pl.width < s.width,
                "trim_to": pl.width if pl.width < s.width else None,
            })
        used_len = s.used_length(cfg)

        # Every square inch that is not a part is waste, and all of it gets hatched.
        # Previously only the strip's end offcut was drawn, which left two kinds of
        # waste invisible: the sliver beside a part narrower than its strip, and the
        # band left over after the last rip.
        for pl in s.placements:
            if pl.width < s.width:
                waste.append({"x": pl.offset, "y": y + pl.width, "w": pl.length,
                              "h": s.width - pl.width, "kind": "trim"})
        if along - used_len > 0:
            waste.append({"x": used_len, "y": y, "w": along - used_len,
                          "h": s.width, "kind": "offcut"})
        kerf_area += max(0, len(s.placements) - 1) * cfg.kerf_mitre_saw * s.width

        strips.append({
            "index": i, "width": s.width, "y": y,
            "saw": "track" if wide else "mitre",
            "wide": wide,
            "color": width_colors[s.width],
            "parts": parts,
            "offcut": along - used_len,
            "fill": used_len / along if along else 0.0,
        })

        # a rip frees this strip at its far edge, unless that edge is the sheet's own
        far = y + s.width
        if far < across:
            cuts.append({"type": "rip", "saw": "track", "orient": "along",
                         "at": far, "from": 0, "to": along,
                         "label": f"rip @ {fmt(far)}"})

        # crosscuts free each part in turn; the last is unnecessary if the parts
        # exactly fill the strip, because the far end is then the sheet's own edge
        for j, pl in enumerate(s.placements):
            end = pl.offset + pl.length
            last = j == len(s.placements) - 1
            if last and end >= along:
                continue
            cuts.append({"type": "cross", "saw": "track" if wide else "mitre",
                         "orient": "across", "at": end, "from": y, "to": y + s.width,
                         "label": f"crosscut @ {fmt(end)}"})

        # a part narrower than its strip needs a third cut, and that cut is a rip
        for pl in s.placements:
            if pl.width < s.width:
                cuts.append({"type": "trim", "saw": "track", "orient": "along",
                             "at": y + pl.width, "from": pl.offset,
                             "to": pl.offset + pl.length,
                             "label": f"trim to {fmt(pl.width)}"})

    if across - used_across > 0:
        waste.append({"x": 0, "y": used_across, "w": along,
                      "h": across - used_across, "kind": "edge"})
    kerf_area += max(0, len(pat.strips) - 1) * cfg.kerf_track_saw * along

    part_area = sum(pl.width * pl.length for s in pat.strips for pl in s.placements)
    return {
        "index": index,
        "thickness": pat.thickness,
        "swapped": pat.swapped,
        "across": across, "along": along,
        "across_in": to_inches(across), "along_in": to_inches(along),
        "utilisation": part_area / (across * along),
        "waste_across": across - used_across,
        "strips": strips,
        "cuts": cuts,
        "waste": waste,
        "kerf_area": kerf_area,
        "part_area": part_area,
        "counts": pat.counts(cfg),
    }


def layout_to_dict(patterns: list[Pattern], demand: list[PartType],
                   cfg: CutConfig, name: str = "") -> dict:
    """Full payload: geometry, cut sequence, cost breakdown and bounds."""
    sc = score(patterns, cfg)
    order, _ = sequence_rips(patterns, cfg)

    # One colour per distinct WIDTH, over every width that appears anywhere in the job
    # -- part widths and rip widths alike -- indexed in sorted order so the numerically
    # closest widths land on adjacent palette entries, which are the furthest apart
    # perceptually. Stable across sheets by construction, since the map is built once
    # for the whole job.
    part_widths = {pl.width for p in patterns for s in p.strips for pl in s.placements}
    strip_widths = {s.width for p in patterns for s in p.strips}
    widths = sorted(part_widths | strip_widths, reverse=True)
    width_colors = {w: i for i, w in enumerate(widths)}

    sheets = [sheet_to_dict(patterns[i], cfg, width_colors, i) for i in order]

    # rip plan in cut order, flagging which groups reuse the standing stop setting
    plan = []
    current = None
    for pos, i in enumerate(order):
        pat = patterns[i]
        groups: dict[int, int] = {}
        for s in pat.strips:
            groups[s.width] = groups.get(s.width, 0) + 1
        seq = []
        for w in sorted(groups, reverse=True):
            seq.append({"width": w, "width_in": to_inches(w), "count": groups[w],
                        "reuses_stop": w == current,
                        "color": width_colors[w],
                        "saw": "track" if w > cfg.mitre_max_crosscut_width else "mitre"})
            current = w
        plan.append({"sheet": i, "position": pos, "thickness": pat.thickness,
                     "groups": seq})

    f = cost_floor(demand, cfg)
    dpm = cfg.dollars_per_min()
    lines = [
        ("track rips", sc.n_rips, cfg.min_per_track_rip),
        ("track stop changes", sc.n_track_stops, cfg.extra_min_per_track_stop_change),
        ("track crosscuts (wide)", sc.n_wide_cross, cfg.min_per_track_crosscut),
        ("mitre crosscuts", sc.n_cross, cfg.min_per_mitre_crosscut),
        ("mitre stop changes", sc.n_mitre_stops, cfg.extra_min_per_mitre_stop_change),
        ("trim rips", sc.n_trims, cfg.min_per_trim_rip),
        ("trim stop changes", sc.n_trim_stops, cfg.extra_min_per_trim_stop_change),
        ("sheet setups", sc.n_sheets, cfg.min_per_sheet_setup),
        ("strip handling", sum(len(p.strips) for p in patterns),
         cfg.min_per_strip_handling),
        ("saw changeovers", sc.n_saw_changeovers, cfg.min_per_saw_changeover),
    ]
    breakdown = [{"label": n, "count": c, "minutes": c * r, "dollars": c * r * dpm}
                 for n, c, r in lines]

    return {
        "name": name,
        "score": {
            "dollars": sc.dollars, "material": sc.material, "labour": sc.labour,
            "minutes": sc.minutes, "hours": sc.minutes / 60.0,
            "n_sheets": sc.n_sheets,
            "sheets_by_thickness": {str(k): v
                                    for k, v in sc.sheets_by_thickness.items()},
            "n_rips": sc.n_rips, "n_track_stops": sc.n_track_stops,
            "n_adjacent_equal_rips": sc.n_adjacent_equal_rips,
            "n_cross": sc.n_cross, "n_wide_cross": sc.n_wide_cross,
            "n_mitre_stops": sc.n_mitre_stops, "n_trims": sc.n_trims,
            "n_trim_stops": sc.n_trim_stops,
            "n_saw_changeovers": sc.n_saw_changeovers,
            "n_parts": sc.n_parts,
            "utilisation": sc.used_area / (sc.n_sheets * cfg.usable_w * cfg.usable_l),
        },
        "breakdown": breakdown,
        "floor": {"hard": f["hard"], "working": f["working"],
                  "min_sheets": f["min_sheets"],
                  "forced_track_parts": f["forced_track_parts"],
                  "mitre_reachable_parts": f["mitre_reachable_parts"]},
        "config": config_to_dict(cfg),
        "widths": [{"width": w, "width_in": to_inches(w), "color": width_colors[w],
                    "parts": sum(1 for p in patterns for s in p.strips
                                 for pl in s.placements if pl.width == w),
                    "strips": sum(1 for p in patterns for s in p.strips
                                  if s.width == w),
                    "saw": "track" if w > cfg.mitre_max_crosscut_width else "mitre"}
                   for w in widths],
        "rip_plan": plan,
        "sheets": sheets,
        "demand_parts": sum(p.qty for p in demand),
        "grid": 32,
    }
