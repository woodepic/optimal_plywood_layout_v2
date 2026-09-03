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
    """Sheet geometry, saw parameters and the full weighted cost model.

    Every operation Matt named has its own knob. Names carry three pieces of
    information: `min_per_*` is minutes charged per occurrence, `extra_min_per_*` is
    minutes charged *on top* of the underlying cut, and the middle word says which saw
    does the work (`track`, `mitre`, or `trim` for the third cut).
    """
    # --- sheet geometry (1/32" units) -----------------------------------------
    sheet_w: int = 48 * GRID
    sheet_l: int = 96 * GRID
    sheet_edge_trim_across: int = 0     # reference edge for the track saw
    sheet_edge_trim_along: int = 0      # squaring a strip end before the mitre stop

    # --- saw parameters -------------------------------------------------------
    kerf_track_saw: int = 2             # 1/16"
    kerf_mitre_saw: int = 2             # 1/16"
    mitre_max_crosscut_width: int = 14 * GRID

    # --- money ----------------------------------------------------------------
    sheet_cost_by_thickness: tuple = ((0.75, 75.0), (0.5, 60.0))
    labour_dollars_per_hour: float = 75.0

    # --- track saw: rips the sheet into strips --------------------------------
    min_per_track_rip: float = 1.0
    extra_min_per_track_stop_change: float = 4.0     # so a fresh width costs 1 + 4 = 5

    # --- track saw: crosscuts strips too wide for the mitre saw ---------------
    min_per_track_crosscut: float = 5.0

    # --- mitre saw: crosscuts strips to length --------------------------------
    min_per_mitre_crosscut: float = 0.25
    extra_min_per_mitre_stop_change: float = 1.0

    # --- the third cut: trim rip on a part narrower than its strip ------------
    min_per_trim_rip: float = 1.0
    extra_min_per_trim_stop_change: float = 4.0

    # --- handling and moving between saws -------------------------------------
    min_per_sheet_setup: float = 5.0
    min_per_strip_handling: float = 1.0
    min_per_saw_changeover: float = 1.0

    allow_trim: bool = True

    @property
    def usable_w(self) -> int:
        return self.sheet_w - self.sheet_edge_trim_across

    @property
    def usable_l(self) -> int:
        return self.sheet_l - self.sheet_edge_trim_along

    def cost_of_sheet(self, thickness: float) -> float:
        for t, c in self.sheet_cost_by_thickness:
            if abs(t - thickness) < 1e-6:
                return c
        raise KeyError(
            f"no sheet price configured for thickness {thickness}; "
            f"configured: {[t for t, _ in self.sheet_cost_by_thickness]}")

    def snap_thickness(self, measured: float, tol: float = 1.0 / 32) -> float:
        """Snap a measured thickness to the nearest configured stock.

        CAD does not hand you exactly 0.75. A metric-designed panel is 19 mm =
        0.7480", and STEP round-tripping produces things like 0.7500000001. Left
        unsnapped those are worse than a crash: load_demand groups by thickness, so a
        0.748 panel forms its OWN material group and quietly buys its own sheets.
        """
        stock = [t for t, _ in self.sheet_cost_by_thickness]
        if not stock:
            raise ValueError("no stock thicknesses configured")
        best = min(stock, key=lambda t: abs(t - measured))
        if abs(best - measured) > tol:
            raise ValueError(
                f'measured thickness {measured:.4f}" is not within {tol:.4f}" of any '
                f'configured stock {stock}. Either add it to '
                f'sheet_cost_by_thickness in config.json, or check whether this solid '
                f'is really a plywood panel.')
        return best

    def dollars_per_min(self) -> float:
        return self.labour_dollars_per_hour / 60.0


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
            self.kerf_count() * cfg.kerf_mitre_saw

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
            max(0, len(self.strips) - 1) * cfg.kerf_track_saw
        # a rip per strip, less one if the strips exactly consume the sheet width
        remainder_across = across - used_across
        n_rips = len(self.strips) if remainder_across > 0 else max(0, len(self.strips) - 1)

        n_cross = 0
        n_wide_cross = 0
        n_trims = 0
        trim_widths = set()
        for s in self.strips:
            rem = along - s.used_length(cfg)
            cuts = len(s.placements) if rem > 0 else max(0, len(s.placements) - 1)
            if s.width > cfg.mitre_max_crosscut_width:
                n_wide_cross += cuts
            else:
                n_cross += cuts
            tw = s.trim_widths()
            n_trims += len(tw)
            trim_widths.update(tw)
        mitre_stops = sum(len(r) for r in self.mitre_run_sequences(cfg))

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

    def mitre_run_sequences(self, cfg: CutConfig) -> list[list[int]]:
        """Per mitre-bound strip, the run-lengths the stop block must be set to, in order.

        A stop block only ever cuts from the END of the strip — it cannot reach a part
        in the middle. So the stop has to be set for each part in turn as the strip gets
        shorter, and consecutive parts of equal length are the only ones that share a
        setting. Parts are laid out length-sorted within a strip precisely so that equal
        lengths end up adjacent and collapse into one run.
        """
        out = []
        for s in self.strips:
            if s.width > cfg.mitre_max_crosscut_width or not s.placements:
                continue
            seq = [pl.length for pl in sorted(s.placements, key=lambda p: p.offset)]
            runs = [seq[0]]
            for v in seq[1:]:
                if v != runs[-1]:
                    runs.append(v)
            out.append(runs)
        return out

    def saw_stations(self, cfg: CutConfig) -> list[str]:
        """Which saw is used, in order, to break this sheet down.

        Rips must come first, and a trim rip can only happen once its part has been
        crosscut free — so trims land after the crosscuts and send you back to the
        track saw. Track-saw crosscuts share a station with the rips and cost nothing
        extra to reach.
        """
        c = self.counts(cfg)
        seq = []
        if c["n_rips"] > 0 or c["n_wide_cross"] > 0:
            seq.append("track")
        if c["n_cross"] > 0:
            seq.append("mitre")
        if c["n_trims"] > 0:
            seq.append("track")
        if not seq:
            seq.append("track")
        # collapse runs of the same station
        out = [seq[0]]
        for st in seq[1:]:
            if st != out[-1]:
                out.append(st)
        return out

    def part_counts(self) -> dict[PartType, int]:
        out: dict[PartType, int] = {}
        for s in self.strips:
            for p in s.placements:
                out[p.part] = out.get(p.part, 0) + 1
        return out


# --- configuration as data -------------------------------------------------------
# The UI will need to change costs at runtime, so CutConfig round-trips through a
# plain dict in HUMAN units — inches and minutes — never internal 1/32" grid units.
# Anything ending _in is inches; the loader converts and rounds lengths up to grid.

_LENGTH_FIELDS = {
    "sheet_w": "sheet_width_in",
    "sheet_l": "sheet_length_in",
    "sheet_edge_trim_across": "sheet_edge_trim_across_in",
    "sheet_edge_trim_along": "sheet_edge_trim_along_in",
    "kerf_track_saw": "kerf_track_saw_in",
    "kerf_mitre_saw": "kerf_mitre_saw_in",
    "mitre_max_crosscut_width": "mitre_max_crosscut_width_in",
}

_PLAIN_FIELDS = (
    "labour_dollars_per_hour",
    "min_per_track_rip",
    "extra_min_per_track_stop_change",
    "min_per_track_crosscut",
    "min_per_mitre_crosscut",
    "extra_min_per_mitre_stop_change",
    "min_per_trim_rip",
    "extra_min_per_trim_stop_change",
    "min_per_sheet_setup",
    "min_per_strip_handling",
    "min_per_saw_changeover",
    "allow_trim",
)


def config_to_dict(cfg: CutConfig) -> dict:
    out = {human: to_inches(getattr(cfg, field))
           for field, human in _LENGTH_FIELDS.items()}
    out.update({f: getattr(cfg, f) for f in _PLAIN_FIELDS})
    out["sheet_cost_by_thickness"] = {str(t): c for t, c in cfg.sheet_cost_by_thickness}
    return out


def config_from_dict(d: dict) -> CutConfig:
    """Build a CutConfig from human units. Unknown keys are rejected, not ignored."""
    known = set(_LENGTH_FIELDS.values()) | set(_PLAIN_FIELDS) | {"sheet_cost_by_thickness"}
    unknown = set(d) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")

    kw = {}
    for field, human in _LENGTH_FIELDS.items():
        if human in d:
            kw[field] = to_units(d[human])
    for f in _PLAIN_FIELDS:
        if f in d:
            kw[f] = d[f]
    if "sheet_cost_by_thickness" in d:
        kw["sheet_cost_by_thickness"] = tuple(
            (float(t), float(c)) for t, c in
            sorted(d["sheet_cost_by_thickness"].items(), key=lambda kv: -float(kv[0])))
    return CutConfig(**kw)


def load_config(path: str) -> CutConfig:
    import json
    with open(path) as f:
        return config_from_dict(json.load(f))


def save_config(cfg: CutConfig, path: str) -> None:
    import json
    with open(path, "w") as f:
        json.dump(config_to_dict(cfg), f, indent=2, sort_keys=True)
        f.write("\n")
