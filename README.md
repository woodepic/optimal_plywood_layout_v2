# Plywood cut-layout optimizer

Reads a STEP assembly of plywood panels and produces a near-optimal 2-stage guillotine
cut layout, scored against a fully configurable cost model (material + shop time).

## Cut model

Physically matched to a track saw + mitre saw workflow, one sheet at a time:

1. **Rip** the sheet into strips with the track saw (stage 1).
2. **Crosscut** each strip into parts with the mitre saw (stage 2). Strips wider than
   `max_crosscut_width` go back to the track saw instead.
3. **Trim** (optional, "non-exact 2-stage"): a third rip for a part narrower than its
   strip. Priced separately so you can decide how annoying it is.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install cadquery-ocp numpy scipy ortools

.venv/bin/python run.py "Master Kitchen Layout V3.step" -n 120 --improve 300
.venv/bin/python run.py --no-trim          # exact 2-stage, no trim cuts
.venv/bin/python run.py --shave 0.125      # what-if: shave near-miss widths
.venv/bin/python show.py out/best.pkl             # cut list
.venv/bin/python advise.py                        # design-side easy wins
.venv/bin/python sensitivity.py                   # re-score under varied weights
.venv/bin/python tests/test_core.py        # cross-checks
```

`-n` is the number of randomized restarts, `--improve` the local-search iterations per
restart. Every candidate is geometrically validated before it can be accepted, and the
best score wins.

## Cost model

Edit `CutConfig` in `src/model.py`. Names encode three things: `min_per_*` is minutes
charged per occurrence, `extra_min_per_*` is minutes charged *on top of* the underlying
cut, and the middle word says which saw does the work.

| Knob | Default | Meaning |
|---|---|---|
| `sheet_cost_by_thickness` | ¾"=$75, ½"=$60 | keyed by thickness; unlisted thicknesses raise |
| `labour_dollars_per_hour` | 75 | the one exchange rate between material and time |
| `min_per_track_rip` | 1.0 | track saw rip reusing the current stop |
| `extra_min_per_track_stop_change` | 4.0 | moving the stop, so a fresh width costs 1 + 4 = 5 |
| `min_per_track_crosscut` | 5.0 | crosscut on the track saw — a strip too wide for the mitre |
| `min_per_mitre_crosscut` | 0.25 | mitre saw cut |
| `extra_min_per_mitre_stop_change` | 1.0 | resetting the mitre stop block |
| `min_per_trim_rip` | 1.0 | the third cut, on a part narrower than its strip |
| `extra_min_per_trim_stop_change` | 4.0 | setting the stop for a trim width |
| `min_per_sheet_setup` | 5.0 | per sheet, getting it onto the horses |
| `min_per_strip_handling` | 1.0 | per strip, carrying it to the saw |
| `min_per_saw_changeover` | 1.0 | per trip between the two saws |
| `kerf_track_saw` / `kerf_mitre_saw` | 1/16" each | |
| `mitre_max_crosscut_width` | 14" | above this, crosscuts go to the track saw |
| `sheet_w` / `sheet_l` | 48" / 96" | |
| `sheet_edge_trim_across` / `_along` | 0 / 0 | factory edges trusted |
| `allow_trim` | True | non-exact 2-stage |
| `GRID` | 1/32" | internal resolution; all dimensions round *up* |

Setting `labour_dollars_per_hour = 0` recovers pure minimum-sheet-count. Setting
`min_per_trim_rip` very high recovers exact 2-stage.

**Counting rules worth knowing.** A mitre stop change is charged once per distinct
length *per sheet*, not per strip — all a sheet's mitre-bound strips come to the saw
together, so one setting serves every part of that length. Saw changeovers are derived
from the station sequence each sheet actually needs: rips must come first, and a trim
rip can only happen after its part is crosscut free, so a sheet with both mitre cuts and
trims costs two changeovers while a track-saw-only sheet costs none. Sequences chain
across sheets, so finishing on the track saw and starting the next sheet there is free.

## The champion layout

`run.py` keeps exactly one layout on disk: `out/best.pkl`, with a diffable
`out/best_cutlist.txt` beside it. Before replacing it, the stored layout is re-scored
under the *current* weights, so changing a knob re-ranks the champion honestly instead
of grandfathering a score computed under old ones. Everything else in `out/` is
gitignored.

## How it works

- **`geometry.py`** — per solid, finds the sheet plane from the dominant planar face
  pair, builds a local frame from its normal, and takes the minimum-area bounding
  rectangle of the projected outline. The world-frame AABB of a rotated panel is *not*
  its footprint; that mistake silently inflates the sheet count.
- **`heuristic.py`** — greedy strip formation. Every candidate strip width is costed by
  knapsack and the best *area density* wins, so a half-empty strip and an over-wide one
  are compared on the same scale. Strips are then shrunk to the widest part they hold
  (normal patterns) and bin-packed across the sheet width, widest first. Sheet packing
  charges one track-saw stop per distinct width via block-lifted knapsack items, so
  batching same-width rips is discovered rather than bolted on.
- **`improve.py`** — ruin-and-recreate plus a strip-draining move, accepted on the true
  score. This is what fixes the greedy's end-game, where low-quantity leftovers each
  land alone in a barely-filled strip.
- **`validate.py`** — independent geometry checker. Kerf and DP off-by-one bugs are
  silent, so nothing is scored until it passes.

## Design advice (`advise.py`)

The optimizer takes parts as given; `advise.py` asks which parts are awkward and how
little would have to change. Four analyses, all suggestions about the CAD model and
never applied automatically:

- **Mitre saw capacity.** A part's *smaller* dimension decides which saw crosscuts it,
  and that is a 20x cost difference per cut. Shows how many parts each capacity would
  reach, and which parts only just miss.
- **Strips per sheet.** Widths that only just fail to fit another strip once the rip
  kerf is spent (two 24" strips need 48-1/16").
- **Parts per strip.** The same argument along the strip length.
- **Near-duplicate dimensions.** Distinct strip widths cost a track stop change each,
  distinct lengths a mitre stop change each. Dimensions differing by less than that is
  pure overhead.

## Not yet built

Column generation (`colgen.py`, `pricing.py`) for a *certified* bound — currently the
only lower bound is the per-thickness area floor. And the UI.
