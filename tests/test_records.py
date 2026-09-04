"""Record keeping: only genuine improvements, never an overwrite, never anonymous."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from src.model import CutConfig, PartType, Pattern, Placement, Strip, config_to_dict, to_units
from src.records import best_for, config_diff, maybe_add, summarise

FAILS = []


def check(cond, msg):
    if not cond:
        FAILS.append(msg)


def _layout(n_sheets, cfg):
    """n_sheets identical sheets, so more sheets == strictly more expensive."""
    pt = PartType(w=to_units(20), l=to_units(30), thickness=0.5, qty=n_sheets,
                  label="20x30")
    pats = []
    for _ in range(n_sheets):
        s = Strip(width=to_units(20))
        s.placements.append(Placement(part=pt, length=to_units(30),
                                      width=to_units(20), offset=0))
        pats.append(Pattern(thickness=0.5, strips=[s]))
    return pats, [pt]


def test_first_record_is_written():
    cfg = CutConfig()
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        pats, demand = _layout(3, cfg)
        got = maybe_add(rd, "abc123", "m.step", pats, demand, cfg)
        check(got is not None, "first record was not written")
        check(got["beat"] is None, f"first record should beat nothing, got {got}")
        check(len(list(rd.glob('*.pkl'))) == 1, "expected exactly one record file")


def test_a_worse_layout_is_refused():
    cfg = CutConfig()
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        good, demand3 = _layout(2, cfg)
        maybe_add(rd, "abc123", "m.step", good, demand3, cfg)
        worse, demand5 = _layout(5, cfg)
        got = maybe_add(rd, "abc123", "m.step", worse, demand5, cfg)
        check(got is None, f"a more expensive layout was accepted as a record: {got}")
        check(len(list(rd.glob('*.pkl'))) == 1,
              "a refused record should not leave a file behind")


def test_beating_a_record_adds_a_file_and_keeps_the_old_one():
    cfg = CutConfig()
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        worse, dw = _layout(4, cfg)
        maybe_add(rd, "abc123", "m.step", worse, dw, cfg)
        import time
        time.sleep(1.05)              # record filenames are stamped to the second
        better, db = _layout(2, cfg)
        got = maybe_add(rd, "abc123", "m.step", better, db, cfg)
        check(got is not None, "a cheaper layout was not recorded")
        check(got["beat"] is not None and got["beat"] > got["dollars"],
              f"the beaten price should be higher than the new one: {got}")
        check(len(list(rd.glob('*.pkl'))) == 2,
              "beating a record must ADD a file, not overwrite the old one")


def test_records_are_per_file():
    cfg = CutConfig()
    with tempfile.TemporaryDirectory() as d:
        rd = Path(d)
        cheap, dc = _layout(2, cfg)
        maybe_add(rd, "fileA", "a.step", cheap, dc, cfg)
        # the same layout is a first record for a DIFFERENT file, however it scores
        got = maybe_add(rd, "fileB", "b.step", *_layout(6, cfg), cfg)
        check(got is not None, "a different file should get its own first record")
        b = best_for(rd, "fileA", cfg)
        check(b is not None and b[1]["filename"] == "a.step",
              "best_for leaked across files")


def test_anonymous_records_are_refused():
    cfg = CutConfig()
    with tempfile.TemporaryDirectory() as d:
        pats, demand = _layout(3, cfg)
        try:
            maybe_add(Path(d), "", "m.step", pats, demand, cfg)
            check(False, "a record with no file identity was accepted -- it would "
                         "form its own family and beat nothing")
        except ValueError as e:
            check("file identity" in str(e), f"unhelpful message: {e}")


def test_config_diff_and_mismatch_flag():
    import dataclasses
    cfg = CutConfig()
    other = dataclasses.replace(cfg, min_per_track_crosscut=2.5)
    d = config_diff(config_to_dict(cfg), config_to_dict(other))
    check(d == ["min_per_track_crosscut"], f"expected one differing knob, got {d}")
    check(config_diff(config_to_dict(cfg), config_to_dict(cfg)) == [],
          "identical configs should report no differences")

    with tempfile.TemporaryDirectory() as td:
        rd = Path(td)
        pats, demand = _layout(2, cfg)
        maybe_add(rd, "abc", "m.step", pats, demand, cfg)
        rows = summarise(rd, cfg)
        check(rows[0]["config_matches"], "record set under the same config was flagged")
        rows = summarise(rd, other)
        check(not rows[0]["config_matches"],
              "record set under different costs was NOT flagged")
        check(rows[0]["config_diff"] == ["min_per_track_crosscut"],
              f"wrong diff reported: {rows[0]['config_diff']}")
        check(rows[0]["dollars"] != rows[0]["dollars_when_set"],
              "repriced value should differ from the price it was set at")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            n0 = len(FAILS)
            try:
                fn()
            except Exception as e:
                FAILS.append(f"{name} raised {type(e).__name__}: {e}")
            print(f"{'PASS' if len(FAILS) == n0 else 'FAIL'}  {name}")
    if FAILS:
        print(f"\n{len(FAILS)} failure(s):")
        for f in FAILS[:20]:
            print("  -", f)
        sys.exit(1)
    print("\nall record checks passed")
