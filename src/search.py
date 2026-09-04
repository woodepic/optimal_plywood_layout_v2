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


# Measured throughput of one worker: local-search iterations per second. Used to size
# a restart so that several complete inside a wall-clock budget instead of one
# straddling it.
ITERS_PER_WORKER_SECOND = 65


def iters_for_budget(seconds: float, lo: int = 150, hi: int = 4000) -> int:
    """Pick a per-restart iteration count for a wall-clock budget.

    Aims for a restart lasting about a third of the budget, so roughly three waves of
    workers complete. Too high and nothing finishes before the deadline; too low and
    the search never gets past construction into the local search that does the work.
    """
    return max(lo, min(hi, int(seconds * ITERS_PER_WORKER_SECOND / 3)))


def restart_seed(base: int, index: int) -> int:
    return base * 100003 + index


def draw_params(rng: random.Random, allow_trim: bool,
                extra_imp: dict | None = None) -> tuple[dict, dict]:
    """Randomised construction and local-search parameters for one restart."""
    kw = dict(
        jitter=rng.choice([0.0, 0.0, 0.01, 0.03, 0.06]),
        trim_weight=rng.choice([0.25, 0.5, 1.0, 2.0, 5.0]) if allow_trim else 1e6,
    )
    imp = dict(
        temp0=rng.choice([0.0, 5.0, 15.0]),
        ruin_frac=rng.choice([0.2, 0.3, 0.45]),
    )
    if extra_imp:
        imp.update(extra_imp)
    return kw, imp


@dataclass
class Restart:
    index: int
    dollars: float
    patterns: list | None
    params: dict
    error: str | None = None


def run_restart(demand: list[PartType], cfg: CutConfig, base_seed: int, index: int,
                iters: int, extra: dict | None = None) -> Restart:
    rng = random.Random(restart_seed(base_seed, index))
    extra = dict(extra or {})
    imp_extra = {k: extra.pop(k) for k in ("weight_thickness",) if k in extra}
    kw, imp = draw_params(rng, cfg.allow_trim, imp_extra)
    if extra:
        kw.update(extra)
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


def _init(demand, cfg, base_seed, iters, extra=None):
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[var] = "1"
    _CTX.update(demand=demand, cfg=cfg, base_seed=base_seed, iters=iters,
                extra=extra or {})


def _work(index: int) -> Restart:
    return run_restart(_CTX["demand"], _CTX["cfg"], _CTX["base_seed"], index,
                       _CTX["iters"], _CTX.get("extra"))


def search(demand: list[PartType], cfg: CutConfig, restarts: int, iters: int,
           base_seed: int = 0, workers: int | None = None, on_result=None,
           extra: dict | None = None, max_seconds: float | None = None):
    """Run independent restarts, returning (best Restart, all Restarts).

    workers=1 runs in-process, which keeps tracebacks readable when debugging.

    max_seconds stops accepting results once the deadline passes, so the caller can ask
    for "the best you can do in 60 seconds" instead of guessing a restart count. The
    restart budget then acts as a ceiling rather than a target, and results already in
    flight are discarded when the pool shuts down -- so the deadline is honoured
    roughly, not to the millisecond.
    """
    import time as _time
    deadline = None if max_seconds is None else _time.monotonic() + max_seconds
    if workers is None:
        workers = min(os.cpu_count() or 1, restarts)

    out: list[Restart] = []
    if workers <= 1:
        for i in range(restarts):
            r = run_restart(demand, cfg, base_seed, i, iters, extra)
            out.append(r)
            if on_result:
                on_result(r)
            if deadline is not None and _time.monotonic() >= deadline:
                break
    else:
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        with ctx.Pool(workers, initializer=_init,
                      initargs=(demand, cfg, base_seed, iters, extra)) as pool:
            it = pool.imap_unordered(_work, range(restarts), chunksize=1)
            while True:
                # The deadline is enforced by waiting on the iterator with a timeout,
                # not by checking the clock after each result. Checking afterwards only
                # works if results actually arrive: with iters set high enough that one
                # restart outlasts the whole budget, the loop would block forever and
                # the budget would be silently ignored.
                remaining = None
                if deadline is not None:
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        pool.terminate()
                        break
                try:
                    r = it.next(timeout=remaining)
                except mp.TimeoutError:
                    pool.terminate()
                    break
                except StopIteration:
                    break
                out.append(r)
                if on_result:
                    on_result(r)

    valid = [r for r in out if r.patterns is not None]
    if not valid:
        if not out and max_seconds is not None:
            raise RuntimeError(
                f"no restart finished within {max_seconds:.0f}s at {iters} local-search "
                f"iterations each. Allow more time, or lower iters.")
        raise RuntimeError("every restart failed validation")
    best = min(valid, key=lambda r: r.dollars)
    return best, out
