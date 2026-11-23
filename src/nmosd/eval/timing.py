"""`nmosd timing`: where the extraction's time went, from the call log. Counts, tokens and seconds only.

Reads the metadata of each model call (`llm_calls.record_json`: token counts, durations, attempts), never the
answers, for the latest run of every field group (tonight's run, not an older one). Safe to paste: field-group
names, counts and times; no text, IDs or dates.

Cut-off answers are the main thing to look for: an answer that ran into `llm.num_predict` is thrown away and the
excerpt is asked again in halves. Before 17.11.26 each cut-off answer was also asked twice more unchanged; the
call log records only the last attempt, so those repeats are estimated as `(attempts - 1) x` its time. With several
questions at once (`llm.parallel`, 17.11.26) each call counts for its share of the clock (its time / parallel).
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import TextIO

from nmosd.store.db import connect


def _cut(rec: dict) -> bool:
    if "cut_off" in (rec.get("extra") or {}):
        return bool(rec["extra"]["cut_off"])
    limit = (rec.get("options") or {}).get("num_predict")
    return bool(limit and (rec.get("eval_count") or 0) >= int(limit) - 4)


def timing(extract: Path) -> dict:
    con = connect(extract, readonly=True)
    try:
        rows = con.execute("SELECT run_id, chunk_id, purpose, record_json, created_at FROM llm_calls "
                           "WHERE purpose LIKE 'stage1:%' ORDER BY created_at").fetchall()
    finally:
        con.close()
    latest: dict[str, str] = {}
    for r in rows:   # in time order, so the last run of each group wins
        latest[r["purpose"]] = r["run_id"]
    groups: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    chunks: dict[str, set] = defaultdict(set)
    docs: dict[str, set] = defaultdict(set)
    for r in rows:
        if latest[r["purpose"]] != r["run_id"]:
            continue
        g = r["purpose"].split(":", 1)[1]
        rec = json.loads(r["record_json"] or "{}")
        s = groups[g]
        # with several questions at once (llm.parallel) the calls overlap: count each call's share of the clock
        wall = float(rec.get("wall_s") or 0) / max(1, int(rec.get("parallel") or 1))
        repeats = max(0, int(rec.get("attempts") or 1) - 1) if rec.get("error") is None else 0
        cut = _cut(rec)
        s["calls"] += 1
        s["cut_off"] += cut
        s["repeats"] += repeats
        s["seconds"] += wall
        s["repeat_seconds"] += repeats * wall
        s["cut_seconds"] += wall * (1 + repeats) if cut else 0
        s["errors"] += rec.get("error") is not None
        s["prompt_tokens"] += rec.get("prompt_eval_count") or 0
        s["output_tokens"] += rec.get("eval_count") or 0
        s["prompt_s"] += (rec.get("prompt_eval_duration_ns") or 0) / 1e9
        s["output_s"] += (rec.get("eval_duration_ns") or 0) / 1e9
        chunks[g].add(r["chunk_id"])
        docs[g].add((r["chunk_id"] or "").split(":", 1)[0])
    out = {}
    for g, s in groups.items():
        out[g] = {k: (int(v) if k in ("calls", "cut_off", "repeats", "errors", "prompt_tokens", "output_tokens") else v)
                  for k, v in s.items()}
        out[g]["chunks"], out[g]["docs"] = len(chunks[g]), len(docs[g])
    return out


def _h(seconds: float) -> str:
    return f"{seconds / 3600:.1f} h" if seconds >= 3600 else f"{seconds / 60:.0f} min"


def run_timing(extract: Path, deid: Path | None = None, cohort: int | None = None, stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    if not Path(extract).exists():
        print("No extraction database yet: run extract first.", file=stdout)
        return 1
    res = timing(extract)
    if not res:
        print("No model calls logged yet.", file=stdout)
        return 1
    tot = defaultdict(float)
    for s in res.values():
        for k, v in s.items():
            tot[k] += v
    spent = tot["seconds"] + tot["repeat_seconds"]
    print("# Where the extraction's time went (latest run of each field group; counts and times only)\n", file=stdout)
    print("| Field group | Notes | Excerpts | Model calls | Cut off | Repeated unchanged | Avg prompt tokens | "
          "Avg answer tokens | Time | Share |\n|---|---|---|---|---|---|---|---|---|---|", file=stdout)
    for g, s in sorted(res.items(), key=lambda kv: -(kv[1]["seconds"] + kv[1]["repeat_seconds"])):
        t = s["seconds"] + s["repeat_seconds"]
        print(f"| {g} | {s['docs']} | {s['chunks']} | {s['calls']} | {s['cut_off']} | {s['repeats']} | "
              f"{s['prompt_tokens'] / s['calls']:.0f} | {s['output_tokens'] / s['calls']:.0f} | {_h(t)} | "
              f"{100 * t / spent:.0f}% |", file=stdout)
    calls = tot["calls"] + tot["repeats"]
    print(f"\n**Total: {_h(spent)} in {int(calls)} model calls for {int(tot['chunks'])} excerpts "
          f"({spent / max(1, tot['chunks']):.0f} s per excerpt).**", file=stdout)
    if tot["output_s"]:
        print(f"- Writing answers: {tot['output_tokens'] / tot['output_s']:.0f} tokens/s, "
              f"{100 * tot['output_s'] / max(1, tot['seconds']):.0f}% of the timed calls.", file=stdout)
    if tot["prompt_s"]:
        print(f"- Reading the prompts: {tot['prompt_tokens'] / tot['prompt_s']:.0f} tokens/s, "
              f"{100 * tot['prompt_s'] / max(1, tot['seconds']):.0f}% of the timed calls.", file=stdout)
    print(f"- Cut-off answers (thrown away, then split): {int(tot['cut_off'])} calls, {_h(tot['cut_seconds'])} "
          f"({100 * tot['cut_seconds'] / spent:.0f}%), of which {_h(tot['repeat_seconds'])} asking the same "
          "question again unchanged (stopped on 17.11.26).", file=stdout)
    print(f"- Extra calls from splitting excerpts: {int(tot['calls'] - tot['chunks'])}.", file=stdout)
    if tot["errors"]:
        print(f"- Calls with no answer (Ollama errors or timeouts): {int(tot['errors'])}.", file=stdout)
    if deid and Path(deid).exists() and cohort:
        con = connect(deid, readonly=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
        finally:
            con.close()
        if n:
            print(f"\nAt this rate, {cohort} patients would take about {_h(spent / n * cohort)} "
                  f"({spent / n * cohort / 86400:.1f} days non-stop), or {_h((spent - tot['repeat_seconds']) / n * cohort)} "
                  f"without the unchanged repeats. Based on {n} patients; their number of notes drives it.", file=stdout)
    return 0
