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
.venv/bin/python show.py out/best.pkl --cliff     # cut list + diagnostics
.venv/bin/python tests/test_core.py        # cross-checks
```

`-n` is the number of randomized restarts, `--improve` the local-search iterations per
restart. Every candidate is geometrically validated before it can be accepted, and the
best score wins.

## Cost model

Edit `CutConfig` in `src/model.py`. Defaults:

| Knob | Default |
|---|---|
| `sheet_cost` | 3/4" $75, 1/2" $60 |
| `value_of_time` | $75/hour |
| `t_rip` | 1 min — track saw cut reusing the current stop |
| `t_track_stop` | 4 min extra — moving the stop (so 5 min for a fresh width) |
| `t_cross` | 0.5 min — mitre saw cut |
| `t_wide_cross` | 1 min — crosscut on the track saw (strip over mitre capacity) |
| `t_trim` / `t_trim_stop` | 1 min / 4 min extra — the third cut |
| `kerf_rip` / `kerf_cross` | 1/16" each |
| `max_crosscut_width` | 14" |
| grid | 1/32" (`GRID` in `src/model.py`) |

Setting `value_of_time = 0` recovers pure minimum-sheet-count. Setting `t_trim` very
high recovers exact 2-stage.

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

## Not yet built

Column generation (`colgen.py`, `pricing.py`) for a *certified* bound — currently the
only lower bound is the per-thickness area floor. And the UI.
