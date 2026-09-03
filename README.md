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
.venv/bin/python verify.py                 # audit a layout against the STEP file
.venv/bin/python tests/run_all.py          # all tests
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

**Counting rules worth knowing.** These are where the model earns or loses its
credibility, so they are spelled out.

*Track saw stop.* One setting per distinct strip width per sheet, less one when the
setting already standing carries over. Sheets are sequenced greedily to harvest those
carryovers, so several strips of the same width in a row are close to free.

*Mitre saw stop.* A stop block only ever cuts from the **end** of a strip — it cannot
reach a part in the middle. So the stop is reset for each part in turn as the strip gets
shorter, and only *consecutive* parts of equal length share a setting. Parts are laid
out length-sorted within each strip precisely so equal lengths become adjacent and
collapse into one run. A strip may be fed from either end, and strips may be taken in any
order, so settings carry over between strips and across sheets the same way rips do.

*Saw changeovers.* Derived from the station sequence each sheet actually needs. Rips come
first; a trim rip can only happen once its part is crosscut free, so it lands after the
crosscuts and sends you back to the track saw. A sheet needing both saws costs one
changeover, one needing a trim after mitre work costs two, and a track-saw-only sheet
costs none. Sequences chain across sheets.

## Changing costs at runtime

Costs live in `config.json` in **human units** — inches and minutes, never internal
1/32" grid units — and every CLI takes `--config`:

```bash
.venv/bin/python run.py --config config.json
.venv/bin/python sensitivity.py          # re-score the champion under varied weights
```

`src/model.py` provides `load_config`, `save_config`, `config_to_dict` and
`config_from_dict`. Unknown keys are rejected rather than silently ignored, so a typo in
a slider name fails loudly. Scoring tools always read the live config rather than the
config pickled alongside a layout, so changing a weight re-ranks existing layouts
honestly.

Three speeds, which matter for a UI:

| Operation | Cost | Use |
|---|---|---|
| Re-score a fixed layout | milliseconds | live slider feedback |
| Short re-solve (`-n 6 --improve 60`) | ~7 s | interactive "try it" |
| Full search (`-n 160 --improve 320`) | ~18 min | final answer |

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

## Verifying a layout against the STEP file (`verify.py`)

Everything in `validate.py` is *self-referential*: it confirms a layout is internally
consistent with the demand list — but that demand list came from `geometry.py` in the
same process. If geometry shrank every part, demand and layout would agree perfectly,
every test would pass, and the score would come out **better**, because smaller parts
are cheaper to cut. Dropped parts and shrunk parts both look like progress.

`verify.py` closes that hole by going back to the STEP file and cross-checking against
quantities computed by a different code path — OpenCASCADE's own volume and surface
integration, which shares nothing with the rotating-calipers bounding rectangle:

| check | catches |
|---|---|
| `face_area <= width x length` | a bounding rectangle smaller than the part |
| `volume <= thickness x width x length` | shrinkage in any of the three axes |
| solids in file == parts in layout | dropped parts |
| multiset of (t, w, l) matches the file | substituted or resized parts |
| surplus reported | overproduction, which costs material |
| rounded dims >= measured dims | grid rounding that shrinks a part |
| demand list == rounded measurements | any error in the geometry -> demand mapping |

That last pair matters more than it looks: 112 of the 201 parts in this assembly are
*not* on the 1/32" grid (dimensions like 23.100" and 18.325"), so the rounding step is
doing real work on more than half of them. All 112 round up, by at most 0.025" — you
can trim a part that is a hair large, but not one that is a hair small. Without these
two checks the audit would compare a rounded demand list against a rounded layout and a
rounding-direction bug would pass unnoticed.

Both ratio checks are **one-sided inequalities that hold for any shape**, not equalities
assuming a perfect prism. Cabinet parts have dados and grooves — up to 4.3% of the
material in this assembly is legitimately machined away — and an earlier version that
asserted `volume == thickness x face_area` produced 60 false alarms for exactly that
reason. Being ratios, they also do not depend on the unit-scale inference being right.

`tests/test_audit.py` corrupts one thing at a time — a shrunk width, a shrunk
thickness, a dropped part, a substituted size — and asserts the audit fails. An audit
nobody has watched fail proves nothing.

Run it before cutting.

## Failing loudly

Three places could once drop work in silence, which is the worst failure mode here: a
short layout still validates, still scores, and scores *better* for having less to cut.
All three now raise.

- `geometry.parts_from_shape` refuses to return fewer parts than there are solids. A
  solid that is not a flat panel is named and reported (`strict=False` to skip
  deliberately).
- `heuristic._build_strips` raises if parts remain that it cannot place, instead of
  returning what it managed.
- `CutConfig.snap_thickness` snaps a measured thickness to the nearest configured stock
  within 1/32" and refuses otherwise. Unsnapped values are worse than a crash: demand is
  grouped by thickness, so a 19 mm panel measured at 0.7480" would form its own material
  group and quietly buy its own sheets.

Champion layouts also record provenance — seed, restart index, construction parameters
and git revision — so any saved layout can be traced back to the run that found it.

## Where the search budget belongs (measured)

`experiment.py` holds `restarts x improve-iterations` constant and slides the split.
Restarts are diversification (independent constructions from different random parameter
draws); improve iterations are intensification (local search inside whichever basin the
construction found). At a 6000-unit budget:

| restarts | improve | secs | best | median |
|---|---|---|---|---|
| 300 | 20 | 311 | $2,266 | $2,372 |
| 150 | 40 | 205 | $2,242 | $2,358 |
| 75 | 80 | 144 | $2,251 | $2,332 |
| 38 | 160 | 116 | $2,234 | $2,303 |
| 19 | 320 | 122 | $2,218 | $2,276 |
| 10 | 600 | 99 | **$2,218** | $2,282 |

Depth beats breadth, and by more than the table suggests: fitting wall time to
`a*restarts + b*restarts*iters` gives **a = 0.73 s per construction and b = 0.0153 s per
improve iteration**, so one construction costs about as many seconds as 48 local-search
iterations. The 10x600 split reached the best score in a third the time of 300x20.

**But that advantage does not survive to large budgets.** At roughly 900-1000 s,
`160 x 320` scored $2,203.75 and `40 x 1200` scored $2,208.12 — a 0.2% spread, i.e.
noise. Both allocations saturate at about $2,205.

The best-of-k curve says the same about restarts alone: expected best is $2,381 at k=1,
$2,281 at k=100, $2,266 at k=300 — $15 for tripling the compute.

So neither knob breaks the plateau. Roughly 100-120 s of search gets within 0.6% of what
10x that budget achieves, which is why the defaults are `-n 24 --improve 500`. The
ceiling is set by the construction heuristic and the local-search move set, not by how
long either runs.

## Measured dead ends

Negative results, kept because they cost real time to establish and say where the
remaining gain is *not*.

**Second rip axis.** `Pattern.swapped` existed from the start and was never set. Now
implemented — and never chosen: 0 swapped sheets of 17 in every ablation arm, while
searching both axes costs 2.2x the wall time. The cause is a tiling argument specific
to this part mix: parts 22-35" long divide a 96" run into 2-4 pieces with little waste,
but divide a 48" run into exactly one, wasting ~30%. A mix with lengths near 24" or 48"
would invert this, so the capability stays behind `--swap`.

**Two-direction rips (banding).** Not built, on an economic argument. A band cut costs
`min_per_track_rip + extra_min_per_track_stop_change` = $6.25, so it must reclaim more
than 0.098 sheets of area to pay. Measured tail waste is 1.20 sheets across 17 sheets,
and only 2 sheets have tails big enough; best case assuming 100% reclaim is $15.97,
realistic ~$2. It becomes worth revisiting if stop changes get cheaper — at
`extra_min_per_track_stop_change = 0` the best case rises to $55. Note the swapped axis
is a *free* approximation of banding (shorter strips, no extra cuts), and even that does
not pay here.

**Block-lifted mitre runs.** Implemented (`_fill_strip_blocks`, items are "m parts of
length l", keyed by length so two parts of equal length share a setting). Measured
neutral for ~15% more time per iteration, so `--block-lift` is opt-in. The related
per-run mitre term in strip density measured *worse* in both isolation arms and is off
by default (`--mitre-stop-cost`).

**Trim economics.** Sweeping the construction bias: forbidding trims entirely costs
**$96** and 2 extra sheets ($2,285 vs $2,188), so trims clearly earn their place. No
fixed bias beat the default randomisation — control $2,188 against $2,191 (near-forbid),
$2,203, $2,222, and $2,249 (encourage-trims, which produced 49 trims dragging $150 of
stop changes). Conclusion: leave the per-restart randomisation alone.

**Judging a move by its gain rate.** `ruin_recreate` gains on only ~5% of applications,
so I cut its weight 16 -> 6. That cost ~$16 at equal wall time. Its rejected moves carry
the search out of a basin so the cheap surgical moves have somewhere new to polish —
work the gain counter cannot see. Diversifying moves should not be weighted by gain rate.

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
