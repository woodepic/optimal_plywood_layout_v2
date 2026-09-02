"""Which moves earn their keep?

Runs a few restarts with per-move bookkeeping and reports, for each move, how often it
found something to do, how often that something was an improvement, and how often it
produced an invalid layout. A move that never applies is dead weight in the weighting;
one that applies constantly but never gains is burning iterations.
"""
from __future__ import annotations

import random

from src.cost import score
from src.heuristic import solve
from src.improve import MOVES, improve
from src.model import load_config
from src.parts import load_demand
from src.validate import check_job

cfg = load_config("config.json")
demand = load_demand("Master Kitchen Layout V3.step", cfg)

RESTARTS = 6
ITERS = 600
stats: dict[str, int] = {}
scores = []

for i in range(RESTARTS):
    rng = random.Random(4242 + i)
    kw = dict(jitter=rng.choice([0.0, 0.03]), trim_weight=rng.choice([0.5, 2.0]))
    pats = solve(demand, cfg, rng, **kw)
    check_job(pats, demand, cfg)
    pats, sc = improve(demand=demand, patterns=pats, cfg=cfg, rng=rng, iters=ITERS,
                       temp0=15.0, ruin_frac=0.3, stats=stats, **kw)
    scores.append(sc.dollars)

print(f"{RESTARTS} restarts x {ITERS} iters   best ${min(scores):,.2f}\n")
print(f'{"move":16s} {"tried":>7} {"applied":>8} {"gains":>7} {"rejects":>8} '
      f'{"none":>7} {"invalid":>8}  {"gain/applied":>12}')
total_gain = 0
for name, weight, _ in MOVES:
    gain = stats.get(name + ":gain", 0)
    rej = stats.get(name + ":reject", 0)
    none = stats.get(name + ":none", 0)
    inv = stats.get(name + ":invalid", 0)
    applied = gain + rej
    tried = applied + none + inv
    total_gain += gain
    rate = f"{gain / applied:.1%}" if applied else "-"
    print(f'{name:16s} {tried:>7} {applied:>8} {gain:>7} {rej:>8} {none:>7} '
          f'{inv:>8}  {rate:>12}')
print(f'\ntotal accepted improvements: {total_gain}')
print("note: 'gains' counts strict improvements; annealing also accepts some "
      "regressions, which are not shown.")
