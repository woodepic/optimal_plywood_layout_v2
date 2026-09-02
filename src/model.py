"""Part types, cut configuration, layout structures and the cost model.

All linear dimensions are integers in 1/32" units. The grid must be fine enough that
kerf rounds up to it without waste: a 1/16" kerf is exactly 2 units.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import ceil

GRID = 32  # units per inch


def to_units(inches: float) -> int:
    """Round UP to the grid. Never round a part smaller than it is."""
    return int(ceil(inches * GRID - 1e-9))


def to_inches(units: int) -> float:
    return units / GRID


def fmt(units: int) -> str:
    """Render grid units as inches with a 32nds fraction."""
    whole, frac = divmod(units, GRID)
    if frac == 0:
        return f'{whole}"'
    n, d = frac, GRID
    while n % 2 == 0:
        n //= 2
        d //= 2
    return f'{whole}-{n}/{d}"'


@dataclass(frozen=True)
class PartType:
    """A distinct part, canonicalised so w <= l, with a quantity."""
    w: int
    l: int
    thickness: float
    qty: int
    rotatable: bool = True
    label: str = ""

    @property
    def area(self) -> int:
        return self.w * self.l

    def variants(self) -> tuple[tuple[int, int], ...]:
        """Orientations available: (width, length) pairs."""
        if not self.rotatable or self.w == self.l:
            return ((self.w, self.l),)
        return ((self.w, self.l), (self.l, self.w))


@dataclass(frozen=True)
class CutConfig:
    """Sheet geometry, saw parameters and the full weighted cost model."""
    sheet_w: int = 48 * GRID
    sheet_l: int = 96 * GRID
    trim_w: int = 0
    trim_l: int = 0
    kerf_rip: int = 2          # 1/16"
    kerf_cross: int = 2        # 1/16"
    max_crosscut_width: int = 14 * GRID

    sheet_cost: tuple = ((0.75, 75.0), (0.5, 60.0))
    value_of_time: float = 75.0   # $/hour

    t_rip: float = 1.0            # min, a track saw cut reusing the current stop
    t_track_stop: float = 4.0     # min, extra when the stop must be moved (1+4 = 5)
    t_cross: float = 0.5          # min, a mitre saw cut
    t_mitre_stop: float = 0.0     # min, extra to reset the mitre stop block
    t_trim: float = 1.0           # min, the third (rip) cut on a non-exact part
    t_trim_stop: float = 4.0      # min, extra to set the stop for a trim width
    t_wide_cross: float = 1.0     # min, crosscut on the track saw (strip too wide for mitre)
    t_sheet_handling: float = 0.0
    t_strip_handling: float = 0.0
    t_tool_transition: float = 0.0

    allow_trim: bool = True

    @property
    def usable_w(self) -> int:
        return self.sheet_w - self.trim_w

    @property
    def usable_l(self) -> int:
        return self.sheet_l - self.trim_l

    def cost_of_sheet(self, thickness: float) -> float:
        for t, c in self.sheet_cost:
            if abs(t - thickness) < 1e-6:
                return c
        raise KeyError(f"no sheet price configured for thickness {thickness}")

    def dollars_per_min(self) -> float:
        return self.value_of_time / 60.0


@dataclass
class Placement:
    """One part positioned inside a strip."""
    part: PartType
    length: int          # extent along the strip
    width: int           # extent across the strip (<= strip width)
    offset: int          # distance from the strip's near end

    @property
    def needs_trim(self) -> bool:
        return False     # set by Strip.finalise


@dataclass
class Strip:
    """A ripped strip, crosscut into parts along its length."""
    width: int
    placements: list[Placement] = field(default_factory=list)

    def used_length(self, cfg: CutConfig) -> int:
        if not self.placements:
            return 0
        return sum(p.length for p in self.placements) + \
            self.kerf_count() * cfg.kerf_cross

    def kerf_count(self) -> int:
        return max(0, len(self.placements) - 1)

    def trim_widths(self) -> list[int]:
        """Widths that need a third cut because the part is narrower than the strip."""
        return [p.width for p in self.placements if p.width < self.width]


@dataclass
class Pattern:
    """A single sheet's cutting plan. Strips run along `usable_l`, stacked across `usable_w`."""
    thickness: float
    strips: list[Strip] = field(default_factory=list)
    swapped: bool = False   # True if rips run parallel to the short sheet dimension

    def sheet_dims(self, cfg: CutConfig) -> tuple[int, int]:
        """(across, along) — the direction strips stack, and the direction they run."""
        return (cfg.usable_l, cfg.usable_w) if self.swapped else (cfg.usable_w, cfg.usable_l)

    def counts(self, cfg: CutConfig) -> dict:
        """Physical operation counts for this one sheet."""
        across, along = self.sheet_dims(cfg)
        used_across = sum(s.width for s in self.strips) + \
            max(0, len(self.strips) - 1) * cfg.kerf_rip
        # a rip per strip, less one if the strips exactly consume the sheet width
        remainder_across = across - used_across
        n_rips = len(self.strips) if remainder_across > 0 else max(0, len(self.strips) - 1)

        n_cross = 0
        n_wide_cross = 0
        n_trims = 0
        trim_widths = set()
        mitre_stops = 0
        for s in self.strips:
            rem = along - s.used_length(cfg)
            cuts = len(s.placements) if rem > 0 else max(0, len(s.placements) - 1)
            if s.width > cfg.max_crosscut_width:
                n_wide_cross += cuts
            else:
                n_cross += cuts
            mitre_stops += len({p.length for p in s.placements})
            tw = s.trim_widths()
            n_trims += len(tw)
            trim_widths.update(tw)

        widths = [s.width for s in self.strips]
        return {
            "n_strips": len(self.strips),
            "n_rips": n_rips,
            "distinct_widths": len(set(widths)),
            "widths": widths,
            "n_cross": n_cross,
            "n_wide_cross": n_wide_cross,
            "n_trims": n_trims,
            "n_trim_widths": len(trim_widths),
            "mitre_stops": mitre_stops,
            "n_parts": sum(len(s.placements) for s in self.strips),
            "used_area": sum(p.length * p.width for s in self.strips for p in s.placements),
        }

    def part_counts(self) -> dict[PartType, int]:
        out: dict[PartType, int] = {}
        for s in self.strips:
            for p in s.placements:
                out[p.part] = out.get(p.part, 0) + 1
        return out
