"""Independent geometry checker.

DP off-by-one-kerf bugs are silent: they produce layouts that score well and cannot be
cut. Every layout the optimizer emits is checked here, and a failure is an error, never
a warning. This module deliberately shares no code with the solvers.
"""
from __future__ import annotations

from .model import CutConfig, Pattern, PartType, fmt


class LayoutError(AssertionError):
    pass


def check_pattern(p: Pattern, cfg: CutConfig, where: str = "") -> None:
    across, along = p.sheet_dims(cfg)
    prefix = f"{where}: " if where else ""

    if not p.strips:
        raise LayoutError(f"{prefix}pattern has no strips")

    # strips must fit across the sheet, with a rip kerf between each adjacent pair
    total = sum(s.width for s in p.strips) + max(0, len(p.strips) - 1) * cfg.kerf_rip
    if total > across:
        raise LayoutError(
            f"{prefix}strips span {fmt(total)} across a {fmt(across)} sheet "
            f"(widths {[fmt(s.width) for s in p.strips]})")

    for i, s in enumerate(p.strips):
        if s.width <= 0:
            raise LayoutError(f"{prefix}strip {i} has non-positive width")
        if not s.placements:
            raise LayoutError(f"{prefix}strip {i} is empty")

        # parts must fit along the strip, with a crosscut kerf between each adjacent pair
        used = sum(pl.length for pl in s.placements) + \
            max(0, len(s.placements) - 1) * cfg.kerf_cross
        if used > along:
            raise LayoutError(
                f"{prefix}strip {i} (w={fmt(s.width)}) uses {fmt(used)} of {fmt(along)}")

        # offsets must be monotonic and kerf-separated
        cursor = 0
        for j, pl in enumerate(s.placements):
            if pl.offset < cursor:
                raise LayoutError(
                    f"{prefix}strip {i} part {j} at {fmt(pl.offset)} overlaps previous "
                    f"(needs >= {fmt(cursor)})")
            cursor = pl.offset + pl.length + cfg.kerf_cross

            if pl.width > s.width:
                raise LayoutError(
                    f"{prefix}strip {i} part {j} is {fmt(pl.width)} wide in a "
                    f"{fmt(s.width)} strip")
            if pl.width < s.width and not cfg.allow_trim:
                raise LayoutError(
                    f"{prefix}strip {i} part {j} needs a trim cut but trims are disabled")

            # the placement must be a real orientation of the part
            if (pl.width, pl.length) not in pl.part.variants():
                raise LayoutError(
                    f"{prefix}strip {i} part {j} is {fmt(pl.width)}x{fmt(pl.length)}, "
                    f"not an orientation of {fmt(pl.part.w)}x{fmt(pl.part.l)}")
            if abs(pl.part.thickness - p.thickness) > 1e-6:
                raise LayoutError(
                    f"{prefix}strip {i} part {j} is {pl.part.thickness}\" thick "
                    f"on a {p.thickness}\" sheet")


def check_job(patterns: list[Pattern], demand: list[PartType], cfg: CutConfig) -> dict:
    """Validate every sheet and confirm demand is met. Returns produced-vs-demanded."""
    for i, p in enumerate(patterns):
        check_pattern(p, cfg, where=f"sheet {i}")

    produced: dict[PartType, int] = {}
    for p in patterns:
        for pt, n in p.part_counts().items():
            produced[pt] = produced.get(pt, 0) + n

    shortfall = {}
    for pt in demand:
        got = produced.get(pt, 0)
        if got < pt.qty:
            shortfall[pt] = (got, pt.qty)
    if shortfall:
        lines = [f"  {fmt(pt.w)}x{fmt(pt.l)} @{pt.thickness}\": produced {g}, need {n}"
                 for pt, (g, n) in shortfall.items()]
        raise LayoutError("demand not met:\n" + "\n".join(lines))

    surplus = {pt: produced[pt] - pt.qty for pt in demand if produced.get(pt, 0) > pt.qty}
    return {"produced": produced, "surplus": surplus}
