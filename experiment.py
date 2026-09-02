"""Where does the search budget belong: more restarts, or deeper local search?

Total work is roughly restarts x improve-iterations. This holds that product fixed and
slides the split, so the two mechanisms can be compared directly:

  restarts  = diversification. Each one is an independent construction from a different
              random parameter draw, landing in a different basin.
  improve   = intensification. Local search inside whatever basin the construction
              found, accepted on true cost.

Also records every restart's score, so the best-of-k curve shows whether more restarts
would still be paying.
"""
from __future__ import annotations

import json
import random
import time

from src.bounds import cost_floor
from src.cost import score
from src.heuristic import solve
from src.improve import improve
from src.model import load_config
from src.parts import load_demand
from src.validate import LayoutError, check_job

BUDGET = 6000
SPLITS = [(300, 20), (150, 40), (75, 80), (38, 160), (19, 320), (10, 600)]

cfg = load_config("config.json")
demand = load_demand("Master Kitchen Layout V3.step", cfg)
floor = cost_floor(demand, cfg)["working"]
print(f"working floor ${floor:,.2f}   budget ~{BUDGET} restart x iter units\n")
print(f'{"restarts":>9} {"improve":>8} {"units":>7} {"secs":>7} '
      f'{"best":>10} {"median":>10} {"worst":>10}  {"vs floor":>9}')

results = {}
for restarts, iters in SPLITS:
    scores = []
    t0 = time.time()
    for i in range(restarts):
        rng = random.Random(9000 + i)
        kw = dict(jitter=rng.choice([0.0, 0.0, 0.01, 0.03, 0.06]),
                  trim_weight=rng.choice([0.25, 0.5, 1.0, 2.0, 5.0]))
        imp = dict(temp0=rng.choice([0.0, 5.0, 15.0]),
                   ruin_frac=rng.choice([0.2, 0.3, 0.45]))
        try:
            pats = solve(demand, cfg, rng, **kw)
            check_job(pats, demand, cfg)
            pats, sc = improve(demand=demand, patterns=pats, cfg=cfg, rng=rng,
                               iters=iters, **imp, **kw)
            check_job(pats, demand, cfg)
        except (LayoutError, ValueError):
            continue
        scores.append(sc.dollars)
    dt = time.time() - t0
    scores.sort()
    results[f"{restarts}x{iters}"] = scores
    print(f'{restarts:>9} {iters:>8} {restarts*iters:>7} {dt:>7.0f} '
          f'{scores[0]:>10,.0f} {scores[len(scores)//2]:>10,.0f} '
          f'{scores[-1]:>10,.0f}  {scores[0]/floor - 1:>8.1%}')

json.dump(results, open("out/experiment.json", "w"))

# best-of-k curve from the widest restart sweep: does another restart still pay?
print("\nbest-of-k curve (300 restarts at 20 iters, restarts in original order):")
raw = results["300x20"]
import statistics
order = raw[:]           # already sorted; simulate draws by sampling without replacement
rng = random.Random(0)
for k in (1, 2, 5, 10, 20, 50, 100, 200, 300):
    if k > len(order):
        break
    trials = [min(rng.sample(order, k)) for _ in range(400)]
    print(f'  k={k:>3}  expected best ${statistics.mean(trials):>9,.0f}   '
          f'p(beat ${order[0]:,.0f}) = {sum(1 for t in trials if t <= order[0])/len(trials):.2%}')
