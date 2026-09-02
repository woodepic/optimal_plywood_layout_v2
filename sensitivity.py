"""Re-score a saved layout under varied cost knobs.

The layout is held fixed; only the weights move. This separates "what does this plan
cost" from "which plan is best", and shows which assumptions actually matter.
"""
import argparse
import dataclasses
import pickle

from src.cost import score
from src.model import CutConfig, load_config

ap = argparse.ArgumentParser()
ap.add_argument("pkl", nargs="?", default="out/best.pkl")
ap.add_argument("--config", default="config.json")
a = ap.parse_args()

d = pickle.load(open(a.pkl, "rb"))
import os
pats = d["patterns"]
cfg = load_config(a.config) if a.config and os.path.exists(a.config) else CutConfig()
base = score(pats, cfg)

print(f"layout: {base.n_sheets} sheets, {base.n_rips} rips, "
      f"{base.n_track_stops} stop changes, {base.n_cross} mitre cuts, "
      f"{base.n_wide_cross} track-saw crosscuts, {base.n_trims} trims\n")

SWEEPS = [
    ("min_per_track_crosscut", "track-saw crosscut (min)", [0.5, 1.0, 2.0, 3.0, 5.0]),
    ("extra_min_per_track_stop_change", "extra min to move the track stop", [0.0, 2.0, 4.0, 8.0]),
    ("min_per_mitre_crosscut", "mitre saw cut (min)", [0.25, 0.5, 1.0]),
    ("min_per_trim_rip", "trim rip (min)", [1.0, 3.0, 5.0]),
    ("labour_dollars_per_hour", "$/hour", [0.0, 40.0, 75.0, 150.0]),
    ("min_per_sheet_setup", "min to set up a sheet", [0.0, 5.0, 10.0]),
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
