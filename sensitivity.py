"""Re-score a saved layout under varied cost knobs.

The layout is held fixed; only the weights move. This separates "what does this plan
cost" from "which plan is best", and shows which assumptions actually matter.
"""
import argparse
import dataclasses
import pickle

from src.cost import score

ap = argparse.ArgumentParser()
ap.add_argument("pkl", nargs="?", default="out/best.pkl")
a = ap.parse_args()

d = pickle.load(open(a.pkl, "rb"))
pats, cfg = d["patterns"], d["cfg"]
base = score(pats, cfg)

print(f"layout: {base.n_sheets} sheets, {base.n_rips} rips, "
      f"{base.n_track_stops} stop changes, {base.n_cross} mitre cuts, "
      f"{base.n_wide_cross} track-saw crosscuts, {base.n_trims} trims\n")

SWEEPS = [
    ("t_wide_cross", "track-saw crosscut (min)", [0.5, 1.0, 2.0, 3.0, 5.0]),
    ("t_track_stop", "extra min to move the track stop", [0.0, 2.0, 4.0, 8.0]),
    ("t_cross", "mitre saw cut (min)", [0.25, 0.5, 1.0]),
    ("t_trim", "trim rip (min)", [1.0, 3.0, 5.0]),
    ("value_of_time", "$/hour", [0.0, 40.0, 75.0, 150.0]),
    ("t_sheet_handling", "min to set up a sheet", [0.0, 5.0, 10.0]),
]

for field, label, values in SWEEPS:
    cur = getattr(cfg, field)
    row = []
    for v in values:
        sc = score(pats, dataclasses.replace(cfg, **{field: v}))
        mark = "*" if v == cur else " "
        row.append(f"{v:>6g}{mark} ${sc.dollars:>8,.0f}")
    print(f"{label:34s} " + "  ".join(row))

print("\n* = current setting.  Layout fixed; only the weights vary.")
print(f"material floor for this layout: ${base.material:,.0f}")
