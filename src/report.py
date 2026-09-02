"""Diagnostics and the human-readable cut list."""
from __future__ import annotations

from collections import Counter

from .cost import score, sequence_rips
from .model import CutConfig, PartType, Pattern, fmt


def kerf_cliff(demand: list[PartType], cfg: CutConfig, threshold_units: int = 8) -> list:
    """Widths that only just fail to fit another strip per sheet.

    A 48" sheet holds only one 24" strip, because the second would need 48-1/16" once
    the rip kerf is spent. Shaving a sixteenth off such a part doubles its strips per
    sheet, so these are worth surfacing: they are usually free changes in cabinet work.
    """
    cap = cfg.usable_w + cfg.kerf_track_saw
    widths: Counter = Counter()
    for pt in demand:
        for w, l in pt.variants():
            if w <= cfg.usable_w and l <= cfg.usable_l:
                widths[w] += pt.qty

    out = []
    for w, qty in widths.items():
        k = cap // (w + cfg.kerf_track_saw)
        if k < 1:
            continue
        target = cap / (k + 1) - cfg.kerf_track_saw     # width needed for one more strip
        delta = w - target
        if 0 < delta <= threshold_units:
            out.append({
                "width": w, "qty": qty, "strips_now": k, "strips_after": k + 1,
                "shave": delta, "target": target,
            })
    return sorted(out, key=lambda d: (d["shave"], -d["qty"]))


def cliff_report(demand: list[PartType], cfg: CutConfig) -> str:
    rows = kerf_cliff(demand, cfg)
    if not rows:
        return "no near-miss widths found."
    lines = ["Widths that just miss fitting another strip per sheet:",
             "  width      qty   strips/sheet   shave off   would become"]
    for r in rows:
        lines.append(f'  {fmt(r["width"]):>9s}  {r["qty"]:4d}   {r["strips_now"]} -> '
                     f'{r["strips_after"]}          {r["shave"]/32:.4f}"    '
                     f'{r["target"]/32:.4f}"')
    return "\n".join(lines)


def cut_list(patterns: list[Pattern], cfg: CutConfig, limit: int | None = None) -> str:
    """Ordered cut list: sheets in rip-sequence order, strips grouped by width."""
    order, _ = sequence_rips(patterns, cfg)
    lines = []
    current_stop = None
    for n, si in enumerate(order):
        if limit is not None and n >= limit:
            lines.append(f"... {len(order) - limit} more sheets")
            break
        p = patterns[si]
        across, along = p.sheet_dims(cfg)
        c = p.counts(cfg)
        util = c["used_area"] / (across * along)
        lines.append(f'\nSHEET {n+1}  {p.thickness}" ply   '
                     f'{fmt(across)} x {fmt(along)}   util {util*100:.0f}%   '
                     f'{c["n_rips"]} rips, {c["n_cross"]+c["n_wide_cross"]} crosscuts')

        groups: dict[int, list] = {}
        for s in p.strips:
            groups.setdefault(s.width, []).append(s)
        for w in sorted(groups, reverse=True):
            reuse = " (stop already set)" if w == current_stop else ""
            saw = "mitre" if w <= cfg.mitre_max_crosscut_width else "TRACK SAW (too wide for mitre)"
            lines.append(f'  RIP {len(groups[w])} strip(s) @ {fmt(w)}{reuse}'
                         f'   -> crosscut on {saw}')
            current_stop = w
            for s in groups[w]:
                by_len: Counter = Counter()
                for pl in s.placements:
                    by_len[(pl.length, pl.width)] += 1
                bits = []
                for (l, pw), k in sorted(by_len.items(), key=lambda kv: -kv[0][0]):
                    trim = f" +trim to {fmt(pw)}" if pw < s.width else ""
                    bits.append(f"{k}x {fmt(l)}{trim}")
                rem = along - s.used_length(cfg)
                bits.append(f"[offcut {fmt(rem)}]" if rem > 0 else "[exact]")
                lines.append(f"      strip: " + ",  ".join(bits))
        if current_stop is not None and sorted(groups)[0] != current_stop:
            current_stop = sorted(groups)[0]
    return "\n".join(lines)


def full_report(patterns: list[Pattern], demand: list[PartType], cfg: CutConfig) -> str:
    sc = score(patterns, cfg)
    out = [str(sc), "", cliff_report(demand, cfg)]
    return "\n".join(out)
