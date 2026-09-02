"""Multi-restart search, parallel across cores.

Restarts are fully independent — each is a construction from its own seeded RNG,
followed by local search — so they parallelise with no shared state. Seeding is
derived from (base seed, restart index) alone, which keeps results identical whether
one worker runs them or ten.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass

from .cost import score
from .heuristic import solve
from .improve import improve
from .model import CutConfig, PartType, Pattern
from .validate import LayoutError, check_job


def restart_seed(base: int, index: int) -> int:
    return base * 100003 + index


def draw_params(rng: random.Random, allow_trim: bool) -> tuple[dict, dict]:
    """Randomised construction and local-search parameters for one restart."""
    kw = dict(
        jitter=rng.choice([0.0, 0.0, 0.01, 0.03, 0.06]),
        trim_weight=rng.choice([0.25, 0.5, 1.0, 2.0, 5.0]) if allow_trim else 1e6,
    )
    imp = dict(
        temp0=rng.choice([0.0, 5.0, 15.0]),
        ruin_frac=rng.choice([0.2, 0.3, 0.45]),
    )
    return kw, imp


@dataclass
class Restart:
    index: int
    dollars: float
    patterns: list | None
    params: dict
    error: str | None = None


def run_restart(demand: list[PartType], cfg: CutConfig, base_seed: int, index: int,
                iters: int) -> Restart:
    rng = random.Random(restart_seed(base_seed, index))
    kw, imp = draw_params(rng, cfg.allow_trim)
    try:
        pats = solve(demand, cfg, rng, **kw)
        check_job(pats, demand, cfg)
        pats, sc = improve(demand=demand, patterns=pats, cfg=cfg, rng=rng,
                           iters=iters, **imp, **kw)
        check_job(pats, demand, cfg)
    except (LayoutError, ValueError) as e:
        return Restart(index, float("inf"), None, {**kw, **imp}, str(e))
    return Restart(index, sc.dollars, pats, {**kw, **imp})


# --- worker plumbing ------------------------------------------------------------
# The pool uses 'spawn' on macOS, so the payload must be picklable and the entry point
# importable at module level. Each worker pins BLAS to one thread: the knapsacks are
# small, and letting numpy spawn its own threads inside ten processes just thrashes.

_CTX = {}


def _init(demand, cfg, base_seed, iters):
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[var] = "1"
    _CTX.update(demand=demand, cfg=cfg, base_seed=base_seed, iters=iters)


def _work(index: int) -> Restart:
    return run_restart(_CTX["demand"], _CTX["cfg"], _CTX["base_seed"], index,
                       _CTX["iters"])


def search(demand: list[PartType], cfg: CutConfig, restarts: int, iters: int,
           base_seed: int = 0, workers: int | None = None, on_result=None):
    """Run `restarts` independent restarts, returning (best_patterns, all Restarts).

    workers=1 runs in-process, which keeps tracebacks readable when debugging.
    """
    if workers is None:
        workers = min(os.cpu_count() or 1, restarts)

    out: list[Restart] = []
    if workers <= 1:
        for i in range(restarts):
            r = run_restart(demand, cfg, base_seed, i, iters)
            out.append(r)
            if on_result:
                on_result(r)
    else:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        with ctx.Pool(workers, initializer=_init,
                      initargs=(demand, cfg, base_seed, iters)) as pool:
            for r in pool.imap_unordered(_work, range(restarts), chunksize=1):
                out.append(r)
                if on_result:
                    on_result(r)

    valid = [r for r in out if r.patterns is not None]
    if not valid:
        raise RuntimeError("every restart failed validation")
    best = min(valid, key=lambda r: r.dollars)
    return best, out
