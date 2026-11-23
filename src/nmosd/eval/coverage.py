"""`nmosd coverage`: which of the codebook's fields (1-303) the latest reconcile run filled, as counts only.

For every field: where its value comes from, how many patients have a proposal, and, for a field read from the notes,
how many of the patients WITHOUT one have notes that its field group reads (its document filter selects them), i.e.
where the notes may hold an answer the model missed. Safe to paste: field names, numbers and counts only (no values,
text, IDs or dates). It answers "which sections could have been filled and aren't" without anyone reading the notes
outside the user's computer.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import TextIO

from nmosd.extract.prefilter import group_specs, is_candidate
from nmosd.redcap.dictionary import Dictionary, default_dictionary_path, load_dictionary
from nmosd.redcap.sources import _note_fields, form_label, hospital_names_kept, source
from nmosd.store.db import connect, get_meta


def coverage(extract: Path, deid: Path, dictionary: Path | None = None, skip: set[str] | None = None) -> dict:
    dic: Dictionary = load_dictionary(dictionary or default_dictionary_path())
    con_x, con_d = connect(extract, readonly=True), connect(deid, readonly=True)
    try:
        try:
            hospital_names = hospital_names_kept(get_meta(con_d, "manifest"))
        except sqlite3.Error:
            hospital_names = False
        run = con_x.execute("SELECT MAX(run_id) FROM proposals").fetchone()[0]
        patients = [r[0] for r in con_d.execute("SELECT study_id FROM patients ORDER BY study_id")]
        filled: dict[str, set[str]] = defaultdict(set)
        instances: dict[str, int] = defaultdict(int)
        for r in con_x.execute("SELECT study_id, instrument, field, value FROM proposals WHERE run_id=?", (run,)):
            if r["value"] not in (None, "", "[]"):
                filled[r["field"]].add(r["study_id"])
                instances[r["field"]] += 1
        # patients with at least one document each note group would read (relevance class + keywords)
        specs, skip = group_specs(), skip or {"low_value"}
        readable: dict[str, set[str]] = defaultdict(set)
        for d in con_d.execute("SELECT study_id, relevance_class, report_description, text FROM documents"):
            for g in specs:
                if g in readable and d["study_id"] in readable[g]:
                    continue
                if is_candidate(g, d["relevance_class"], d["text"] or "", skip, d["report_description"])[0]:
                    readable[g].add(d["study_id"])
    finally:
        con_x.close()
        con_d.close()
    rows = []
    for n in dic.fields:
        kind, why = source(dic, n, hospital_names)
        group = _note_fields().get(n) if kind == "notes" else None
        have = filled.get(n, set())
        missing_with_notes = len(readable.get(group, set()) - have) if group else None
        rows.append({"number": dic[n].number, "field": n, "form": dic[n].form, "source": kind, "group": group,
                     "patients": len(have), "instances": instances.get(n, 0), "missing_with_notes": missing_with_notes,
                     "why": why})
    return {"run": run, "patients": len(patients), "rows": rows}


def run_coverage(extract: Path, deid: Path, dictionary: Path | None = None, stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    if not Path(extract).exists():
        print("No extraction database yet: run extract and reconcile first.", file=stdout)
        return 1
    res = coverage(extract, deid, dictionary)
    if not res["run"]:
        print("No reconcile run yet: run reconcile first.", file=stdout)
        return 1
    dic = load_dictionary(dictionary or default_dictionary_path())
    n = res["patients"]
    print(f"# Field coverage of the latest reconcile run ({n} patients; counts only)\n", file=stdout)
    print("Source: notes = read by the model; derived = worked out from other answers; table = Demographics table; "
          "redcap / identifier / reviewer = not proposed here. 'Without, notes read' = patients with no proposal "
          "although the field's group read some of their notes: look there for misses.\n", file=stdout)
    form = None
    for r in res["rows"]:
        if r["form"] != form:
            form = r["form"]
            print(f"\n## {form_label(dic, form)}\n\n| # | Field | Source | Patients with a value | Instances | "
                  "Without, notes read |\n|---|---|---|---|---|---|", file=stdout)
        if r["source"] in ("redcap", "identifier", "reviewer"):
            print(f"| {r['number']} | {r['field']} | {r['source']} | – | – | – |", file=stdout)
            continue
        gap = "" if r["missing_with_notes"] is None else str(r["missing_with_notes"])
        print(f"| {r['number']} | {r['field']} | {r['source']}{' (' + r['group'] + ')' if r['group'] else ''} | "
              f"{r['patients']}/{n} | {r['instances']} | {gap} |", file=stdout)
    notes = [r for r in res["rows"] if r["source"] == "notes"]
    never = [r for r in notes if r["patients"] == 0]
    print(f"\n**{len(notes)} fields are read from the notes; {len(never)} have no value for any patient.**", file=stdout)
    return 0
