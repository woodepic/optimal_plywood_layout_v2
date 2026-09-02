"""Dump a saved solution: score breakdown, kerf-cliff diagnostics, ordered cut list."""
import argparse
import pickle

from src.cost import score
from src.report import cliff_report, cut_list

ap = argparse.ArgumentParser()
ap.add_argument("pkl", nargs="?", default="out/best.pkl")
ap.add_argument("--sheets", type=int, default=None, help="how many sheets to print")
ap.add_argument("--cliff", action="store_true")
a = ap.parse_args()

d = pickle.load(open(a.pkl, "rb"))
pats, cfg, demand = d["patterns"], d["cfg"], d["demand"]
sc = score(pats, cfg)
print(sc)
print()
if a.cliff:
    print(cliff_report(demand, cfg))
    print()
print(cut_list(pats, cfg, limit=a.sheets))
