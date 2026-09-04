"""Local web UI for the plywood layout solver.

Runs on this machine because uploading a STEP file and running the solver both need
Python (OpenCASCADE for the geometry, numpy for the knapsacks). Binds 0.0.0.0 by
default so any device on the same network can reach it.

Three speeds are exposed separately, because they differ by four orders of magnitude:

  rescore   milliseconds   move a cost knob, see this layout repriced
  solve     as long as you ask for, honouring a wall-clock budget
  compare   milliseconds   every saved layout repriced under one cost model

The distinction matters: repricing tells you what a plan costs under new weights, but
only re-solving tells you what the BEST plan under those weights would be. The UI keeps
them as separate buttons rather than pretending a slider re-optimises.
"""
from __future__ import annotations

import json
import pickle
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, request, send_from_directory

from src.cost import score
from src.model import CutConfig, config_from_dict, config_to_dict, load_config
from src.parts import area_bound, load_demand
from src.search import iters_for_budget, search
from src.serialize import layout_to_dict
from src.validate import check_job

STATE = ROOT / "out" / "webapp"
UPLOADS = STATE / "uploads"
SAVED = STATE / "saved"
for d in (UPLOADS, SAVED):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _cfg_from_request(data) -> CutConfig:
    raw = (data or {}).get("config")
    if not raw:
        p = ROOT / "config.json"
        return load_config(str(p)) if p.exists() else CutConfig()
    return config_from_dict(raw)


def _load_bundle(path: Path):
    with open(path, "rb") as f:
        return pickle.load(f)


# --- static -----------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# --- config -----------------------------------------------------------------------

@app.get("/api/config")
def get_config():
    p = ROOT / "config.json"
    cfg = load_config(str(p)) if p.exists() else CutConfig()
    return jsonify(config_to_dict(cfg))


@app.post("/api/config")
def put_config():
    """Persist a cost model. Rejects unknown keys rather than ignoring them."""
    try:
        cfg = config_from_dict(request.json)
    except (ValueError, KeyError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    with open(ROOT / "config.json", "w") as f:
        json.dump(config_to_dict(cfg), f, indent=2, sort_keys=True)
        f.write("\n")
    return jsonify(config_to_dict(cfg))


# --- upload -----------------------------------------------------------------------

@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "no file"}), 400
    if not f.filename.lower().endswith((".step", ".stp")):
        return jsonify({"error": "expected a .step or .stp file"}), 400

    sid = uuid.uuid4().hex[:12]
    path = UPLOADS / f"{sid}.step"
    f.save(path)

    cfg = _cfg_from_request(None)
    try:
        demand = load_demand(str(path), cfg)
    except Exception as e:
        path.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 400

    with open(UPLOADS / f"{sid}.demand.pkl", "wb") as fh:
        pickle.dump({"demand": demand, "step": str(path),
                     "filename": f.filename}, fh)

    bounds = area_bound(demand, cfg)
    by_t: dict[float, dict] = {}
    for pt in demand:
        e = by_t.setdefault(pt.thickness, {"parts": 0, "types": 0, "area": 0})
        e["parts"] += pt.qty
        e["types"] += 1
        e["area"] += pt.area * pt.qty
    return jsonify({
        "id": sid, "filename": f.filename,
        "parts": sum(p.qty for p in demand), "types": len(demand),
        "thicknesses": [
            {"thickness": t, "parts": v["parts"], "types": v["types"],
             "sqft": v["area"] / (32 * 32 * 144),
             "floor": bounds[t], "floor_int": int(-(-bounds[t] // 1))}
            for t, v in sorted(by_t.items(), reverse=True)],
    })


@app.get("/api/uploads")
def list_uploads():
    out = []
    for p in sorted(UPLOADS.glob("*.demand.pkl"), key=lambda q: -q.stat().st_mtime):
        b = _load_bundle(p)
        out.append({"id": p.name.split(".")[0], "filename": b["filename"],
                    "parts": sum(x.qty for x in b["demand"]),
                    "when": p.stat().st_mtime})
    return jsonify(out)


# --- solve ------------------------------------------------------------------------

def _run_solve(job_id: str, sid: str, cfg: CutConfig, seconds: float, seed: int):
    job = _jobs[job_id]
    try:
        b = _load_bundle(UPLOADS / f"{sid}.demand.pkl")
        demand = b["demand"]
        t0 = time.monotonic()

        def note(r):
            with _lock:
                job["done"] = job.get("done", 0) + 1
                job["elapsed"] = time.monotonic() - t0
                if r.patterns is not None:
                    d = r.dollars
                    if job.get("best") is None or d < job["best"]:
                        job["best"] = d

        iters = job["iters"]
        best, all_r = search(demand, cfg, restarts=100000, iters=iters,
                             base_seed=seed, on_result=note, max_seconds=seconds)
        check_job(best.patterns, demand, cfg)
        payload = layout_to_dict(best.patterns, demand, cfg)
        payload["provenance"] = {"seed": seed, "restart_index": best.index,
                                 "params": best.params, "seconds": seconds,
                                 "restarts_completed": len(all_r),
                                 "iters": iters,
                                 "step_id": sid, "filename": b["filename"]}
        with open(STATE / f"job_{job_id}.pkl", "wb") as fh:
            pickle.dump({"patterns": best.patterns, "demand": demand,
                         "cfg_fields": config_to_dict(cfg),
                         "provenance": payload["provenance"]}, fh)
        with _lock:
            job["status"] = "done"
            job["result"] = payload
    except Exception as e:
        with _lock:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            job["traceback"] = traceback.format_exc()


@app.post("/api/solve")
def start_solve():
    data = request.json or {}
    sid = data.get("step_id")
    if not sid or not (UPLOADS / f"{sid}.demand.pkl").exists():
        return jsonify({"error": "unknown step_id -- upload a file first"}), 400
    try:
        cfg = _cfg_from_request(data)
    except (ValueError, KeyError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    seconds = max(1.0, min(float(data.get("seconds", 30)), 3600.0))
    seed = int(data.get("seed", int(time.time()) % 100000))

    job_id = uuid.uuid4().hex[:12]
    iters = int(data.get("iters") or iters_for_budget(seconds))
    _jobs[job_id] = {"status": "running", "done": 0, "best": None,
                     "elapsed": 0.0, "seconds": seconds, "iters": iters}
    threading.Thread(target=_run_solve, args=(job_id, sid, cfg, seconds, seed),
                     daemon=True).start()
    return jsonify({"job_id": job_id})


@app.get("/api/solve/<job_id>")
def poll_solve(job_id):
    job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "unknown job"}), 404
    with _lock:
        return jsonify(dict(job))


# --- rescore, save, compare -------------------------------------------------------

def _bundle_for(ref: str):
    if ref.startswith("job_"):
        p = STATE / f"{ref}.pkl"
    else:
        p = SAVED / f"{ref}.pkl"
    if not p.exists():
        return None
    return _load_bundle(p)


@app.post("/api/rescore")
def rescore():
    """Reprice an existing layout. No re-solving: the layout is held fixed."""
    data = request.json or {}
    b = _bundle_for(data.get("ref", ""))
    if b is None:
        return jsonify({"error": "unknown layout ref"}), 404
    try:
        cfg = _cfg_from_request(data)
    except (ValueError, KeyError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    try:
        check_job(b["patterns"], b["demand"], cfg)
    except Exception as e:
        return jsonify({"error": f"layout is not valid under this cost model: {e}"}), 400
    payload = layout_to_dict(b["patterns"], b["demand"], cfg,
                             name=b.get("name", ""))
    payload["provenance"] = b.get("provenance", {})
    return jsonify(payload)


@app.post("/api/saved")
def save_layout():
    data = request.json or {}
    b = _bundle_for(data.get("ref", ""))
    if b is None:
        return jsonify({"error": "unknown layout ref"}), 404
    name = (data.get("name") or "").strip() or f"layout {time.strftime('%H:%M:%S')}"
    lid = uuid.uuid4().hex[:12]
    b = dict(b)
    b["name"] = name
    with open(SAVED / f"{lid}.pkl", "wb") as fh:
        pickle.dump(b, fh)
    return jsonify({"id": lid, "name": name})


@app.get("/api/saved")
def list_saved():
    p = ROOT / "config.json"
    cfg = load_config(str(p)) if p.exists() else CutConfig()
    out = []
    for f in sorted(SAVED.glob("*.pkl"), key=lambda q: -q.stat().st_mtime):
        b = _load_bundle(f)
        try:
            sc = score(b["patterns"], cfg)
            row = {"dollars": sc.dollars, "material": sc.material,
                   "labour": sc.labour, "hours": sc.minutes / 60.0,
                   "n_sheets": sc.n_sheets, "n_rips": sc.n_rips,
                   "n_track_stops": sc.n_track_stops, "n_cross": sc.n_cross,
                   "n_wide_cross": sc.n_wide_cross, "n_trims": sc.n_trims,
                   "valid": True}
        except Exception as e:
            row = {"valid": False, "error": str(e)}
        row.update({"id": f.stem, "name": b.get("name", f.stem),
                    "when": f.stat().st_mtime,
                    "filename": b.get("provenance", {}).get("filename", ""),
                    "parts": sum(x.qty for x in b["demand"])})
        out.append(row)
    return jsonify({"config": config_to_dict(cfg), "layouts": out})


@app.delete("/api/saved/<lid>")
def delete_saved(lid):
    p = SAVED / f"{lid}.pkl"
    if not p.exists():
        return jsonify({"error": "unknown layout"}), 404
    p.unlink()
    return jsonify({"ok": True})


@app.get("/api/layout/<ref>")
def get_layout(ref):
    b = _bundle_for(ref)
    if b is None:
        return jsonify({"error": "unknown layout ref"}), 404
    p = ROOT / "config.json"
    cfg = load_config(str(p)) if p.exists() else CutConfig()
    payload = layout_to_dict(b["patterns"], b["demand"], cfg, b.get("name", ""))
    payload["provenance"] = b.get("provenance", {})
    return jsonify(payload)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0",
                    help="0.0.0.0 makes it reachable from other devices on the LAN")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()

    import socket
    try:
        lan = socket.gethostbyname(socket.gethostname())
    except Exception:
        lan = "?"
    print(f"\n  local:   http://localhost:{a.port}")
    if a.host == "0.0.0.0":
        print(f"  network: http://{lan}:{a.port}   (same WiFi only)\n")
    app.run(host=a.host, port=a.port, threaded=True, debug=False)
