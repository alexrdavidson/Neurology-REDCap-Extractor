"""`nmosd timing` (17.11.26): where the extraction's time went, from the call log's metadata only."""

from __future__ import annotations

import io
import json

from nmosd.eval.timing import run_timing, timing
from nmosd.store.db import connect


def _log(path, rows):
    con = connect(path, schema="extract")
    for i, (run, chunk, group, rec, at) in enumerate(rows):
        con.execute("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?)",
                    (f"c{i}", run, chunk, f"stage1:{group}", json.dumps(rec), "sha", "SECRET ANSWER TEXT", at))
    con.commit()
    con.close()


def _rec(wall, out, attempts=1, limit=1024):
    return {"wall_s": wall, "eval_count": out, "prompt_eval_count": 2000, "attempts": attempts, "error": None,
            "options": {"num_predict": limit}, "eval_duration_ns": int(wall * 0.8e9),
            "prompt_eval_duration_ns": int(wall * 0.2e9)}


def test_timing_counts_cut_offs_repeats_and_only_the_latest_run(tmp_path):
    db = tmp_path / "extract.sqlite"
    _log(db, [
        ("old", "DOC-AAAAAAAAAA:imaging:0", "imaging", _rec(99, 100), "2026-11-13T01:00:00"),   # an older run
        ("new", "DOC-AAAAAAAAAA:imaging:0", "imaging", _rec(20, 1024, attempts=3), "2026-11-17T22:00:00"),
        ("new", "DOC-AAAAAAAAAA:imaging:0", "imaging", _rec(10, 400), "2026-11-17T22:01:00"),
        ("new", "DOC-AAAAAAAAAA:imaging:0", "imaging", _rec(10, 400), "2026-11-17T22:02:00"),
        ("new2", "DOC-BBBBBBBBBB:labs:0", "labs", _rec(15, 300), "2026-11-17T22:03:00"),
    ])
    res = timing(db)
    im = res["imaging"]
    assert (im["calls"], im["chunks"], im["docs"], im["cut_off"], im["repeats"]) == (3, 1, 1, 1, 2)
    assert im["seconds"] == 40 and im["repeat_seconds"] == 40 and im["cut_seconds"] == 60
    assert res["labs"]["calls"] == 1 and res["labs"]["cut_off"] == 0
    buf = io.StringIO()
    assert run_timing(db, stdout=buf) == 0
    text = buf.getvalue()
    assert "| imaging | 1 | 1 | 3 | 1 | 2 |" in text and "Cut-off answers" in text
    assert "DOC-" not in text and "SECRET" not in text and "2026" not in text


def test_timing_without_calls(tmp_path):
    db = tmp_path / "extract.sqlite"
    connect(db, schema="extract").close()
    buf = io.StringIO()
    assert run_timing(db, stdout=buf) == 1 and "No model calls" in buf.getvalue()


def test_timing_counts_overlapping_calls_once(tmp_path):
    """With several questions at once (llm.parallel) each call counts for its share of the clock."""
    db = tmp_path / "extract.sqlite"
    rec = {**_rec(40, 300), "parallel": 4}
    _log(db, [("r", f"DOC-AAAAAAAAAA:labs:{i}", "labs", rec, f"2026-11-18T01:0{i}:00") for i in range(4)])
    assert timing(db)["labs"]["seconds"] == 40
