"""Automatic record keeping, per STEP file.

A "record" is the cheapest layout ever found for a given model. Three things make this
less trivial than it sounds:

* **Which file.** Keyed by the SHA-256 of the STEP file's bytes, not by an upload id or
  a filename, so re-uploading the same model continues the same record family and two
  different models that happen to share a name do not collide.

* **Cheapest under which costs?** A layout's price depends entirely on the cost model,
  so "better" is meaningless until you say under which weights. A new solve is compared
  against every existing record for that file **re-scored under the config the new
  solve used**, which is the only apples-to-apples comparison available.

* **Never overwrite.** Each record is a new file. A record that is later beaten stays on
  disk, because the cost model that made it a record may be the one you come back to.
"""
from __future__ import annotations

import hashlib
import pickle
import time
from pathlib import Path

from .cost import score
from .model import CutConfig, config_to_dict
from .validate import check_job


def file_sha(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _score_under(bundle: dict, cfg: CutConfig):
    """Price a stored layout under `cfg`, or None if it is not valid there."""
    try:
        check_job(bundle["patterns"], bundle["demand"], cfg)
        return score(bundle["patterns"], cfg)
    except Exception:
        return None


def load_all(rec_dir: Path, sha: str | None = None) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(rec_dir.glob("*.pkl")):
        try:
            with open(p, "rb") as f:
                b = pickle.load(f)
        except Exception:
            continue
        if sha is None or b.get("sha") == sha:
            out.append((p, b))
    return out


def best_for(rec_dir: Path, sha: str, cfg: CutConfig):
    """Cheapest existing record for this file, priced under cfg. (path, bundle, score)."""
    best = None
    for p, b in load_all(rec_dir, sha):
        sc = _score_under(b, cfg)
        if sc is None:
            continue
        if best is None or sc.dollars < best[2].dollars:
            best = (p, b, sc)
    return best


def maybe_add(rec_dir: Path, sha: str, filename: str, patterns, demand,
              cfg: CutConfig, provenance: dict | None = None) -> dict | None:
    """Store a new record if this layout beats every existing one for the file.

    Returns a summary when a record was written, else None.
    """
    if not sha:
        raise ValueError(
            "refusing to write a record with no file identity: it would form its own "
            "record family and so beat nothing, however bad the layout")
    rec_dir.mkdir(parents=True, exist_ok=True)
    sc = score(patterns, cfg)
    prev = best_for(rec_dir, sha, cfg)
    if prev is not None and prev[2].dollars <= sc.dollars + 1e-9:
        return None

    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = rec_dir / f"{sha}__{stamp}.pkl"
    bundle = {
        "patterns": patterns, "demand": demand, "sha": sha,
        "filename": filename, "cfg_fields": config_to_dict(cfg),
        "dollars_when_set": sc.dollars, "when": time.time(),
        "provenance": provenance or {},
        "name": f"record {stamp} — {filename}",
    }
    with open(path, "wb") as f:
        pickle.dump(bundle, f)
    return {"id": path.stem, "dollars": sc.dollars,
            "beat": None if prev is None else prev[2].dollars}


def config_diff(stored: dict | None, current: dict) -> list[str]:
    """Which cost knobs differ between a record's config and the current one."""
    if not stored:
        return ["(no config recorded)"]
    keys = set(stored) | set(current)
    out = []
    for k in sorted(keys):
        a, b = stored.get(k), current.get(k)
        if isinstance(a, dict) and isinstance(b, dict):
            if a != b:
                out.append(k)
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(float(a) - float(b)) > 1e-9:
                out.append(k)
        elif a != b:
            out.append(k)
    return out


def summarise(rec_dir: Path, cfg: CutConfig) -> list[dict]:
    """All records, priced under cfg, newest first, flagged when their config differs."""
    cur = config_to_dict(cfg)
    rows = []
    for p, b in load_all(rec_dir):
        sc = _score_under(b, cfg)
        diff = config_diff(b.get("cfg_fields"), cur)
        rows.append({
            "id": p.stem, "sha": b.get("sha", ""),
            "filename": b.get("filename", ""), "name": b.get("name", p.stem),
            "when": b.get("when", p.stat().st_mtime),
            "dollars_when_set": b.get("dollars_when_set"),
            "dollars": None if sc is None else sc.dollars,
            "valid": sc is not None,
            "n_sheets": None if sc is None else sc.n_sheets,
            "hours": None if sc is None else sc.minutes / 60.0,
            "parts": sum(x.qty for x in b["demand"]),
            "config_matches": not diff,
            "config_diff": diff,
        })
    rows.sort(key=lambda r: -r["when"])
    # mark the current best per file under these costs
    best_by_sha: dict[str, float] = {}
    for r in rows:
        if r["dollars"] is None:
            continue
        s = r["sha"]
        if s not in best_by_sha or r["dollars"] < best_by_sha[s]:
            best_by_sha[s] = r["dollars"]
    for r in rows:
        r["is_best"] = (r["dollars"] is not None
                        and abs(r["dollars"] - best_by_sha.get(r["sha"], -1)) < 1e-9)
    return rows
