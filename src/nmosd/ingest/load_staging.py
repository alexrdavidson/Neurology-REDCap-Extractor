"""`nmosd stage`: copy the selected patients' rows into a staging SQLite (IDENTIFIABLE, PHI location).

* One `documents` table across all note tables, text kept byte-for-byte except the trailing
  `[report_end]` marker.
* Report_Status policy: keep F and R; drop P when a final/revised version of the same report exists
  (configurable under `source:` in settings).
* Exact duplicate texts (after whitespace normalisation) within a patient collapse into one document;
  every source report is kept in `document_sources`.
* Relevance class (config/doc_types.yaml) and hospital site letter per document.
* Report_Date_Time parsed in both formats; Date_of_Birth century resolved from Age.
* Any extra structured table with an EMPI column (Med, Lab, Mrn, Con, ...) is copied automatically.

Prints counts only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

from nmosd import __version__
from nmosd.ingest.access_reader import open_source
from nmosd.ingest.column_map import DEMOGRAPHICS_COLUMNS, NOTE_COLUMNS, classify_table, resolve
from nmosd.ingest.doc_types import relevance_class, site_of
from nmosd.safety import ensure_phi_output
from nmosd.store.db import connect, set_meta
from nmosd.textproc.dates import parse_datetime, parse_dob, to_iso

REPORT_END_RX = re.compile(r"\[report_end\]\s*$", re.I)
STATUS_RANK = {"F": 0, "R": 1, "P": 2}


def read_empi_list(path: Path) -> list[str]:
    out = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip().strip(",")
        if line:
            out.append(line)
    return list(dict.fromkeys(out))


def export_date_for(source: Path, settings) -> date:
    configured = settings.get("source.export_date") if settings else None
    if configured:
        return date.fromisoformat(str(configured))
    m = re.search(r"_(\d{8})_", Path(source).name)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            pass
    return date.today()


def strip_report_end(text: str) -> str:
    return REPORT_END_RX.sub("", text)


def text_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()


def apply_status_policy(rows: list[dict], settings) -> tuple[list[dict], int]:
    keep = set(settings.get("source.keep_status", ["F", "R", "P"]) if settings else ["F", "R", "P"])
    drop_p = settings.get("source.drop_preliminary_if_final_exists", True) if settings else True
    match_on = settings.get("source.version_match_on", ["empi", "report_number"]) if settings else ["empi", "report_number"]
    groups: dict[tuple, set] = defaultdict(set)
    for r in rows:
        groups[tuple(r[k] for k in match_on)].add(r["report_status"].upper())
    kept, dropped = [], 0
    for r in rows:
        st = r["report_status"].upper()
        if st and st not in keep:
            dropped += 1
            continue
        if drop_p and st == "P" and groups[tuple(r[k] for k in match_on)] & {"F", "R"}:
            dropped += 1
            continue
        kept.append(r)
    return kept, dropped


def stage(source: Path, empi_list: Path | None, out: Path, settings=None, backend: str = "auto",
          all_patients: bool = False, stdout: TextIO | None = None) -> dict:
    stdout = stdout or sys.stdout
    out = ensure_phi_output(out, "the staging database")
    empis = None if all_patients else read_empi_list(empi_list)
    wanted = set(empis) if empis else None
    ref_date = export_date_for(source, settings)
    reader = open_source(source, backend)
    if out.exists():
        out.unlink()
        print("Replaced the existing staging database.", file=stdout)
    con = connect(out, "staging")
    counts: dict[str, dict] = {}
    notes: list[dict] = []
    found: set[str] = set()
    try:
        for table in reader.tables():
            cols = reader.columns(table)
            kind, logical = classify_table(table, cols)
            if kind == "skip":
                continue
            if kind == "note":
                cm = resolve(cols, NOTE_COLUMNS)
                if not cm["empi"] or not cm["text"]:
                    print(f"Skipping note table {table}: missing EMPI/Report_Text columns", file=stdout)
                    continue
                rows = []
                for r in reader.rows(table):
                    e = r.get(cm["empi"], "").strip()
                    if wanted is not None and e not in wanted:
                        continue
                    found.add(e)
                    rows.append({
                        "empi": e, "source_table": logical,
                        "report_number": r.get(cm["report_number"] or "", ""),
                        "report_datetime_raw": r.get(cm["report_datetime"] or "", ""),
                        "report_description": r.get(cm["report_description"] or "", ""),
                        "report_type": r.get(cm["report_type"] or "", ""),
                        "report_status": r.get(cm["report_status"] or "", "").strip(),
                        "text": strip_report_end(r.get(cm["text"], "")),
                    })
                kept, dropped = apply_status_policy(rows, settings)
                counts[logical] = {"rows": len(rows), "dropped_status": dropped, "kept": len(kept)}
                notes.extend(kept)
            elif logical == "Demographics":
                dm = resolve(cols, DEMOGRAPHICS_COLUMNS)
                n = 0
                for r in reader.rows(table):
                    e = r.get(dm["empi"] or "EMPI", "").strip()
                    if wanted is not None and e not in wanted:
                        continue
                    found.add(e)
                    res = parse_dob(r.get(dm["dob"] or "", ""), r.get(dm["age"] or "", ""), reference=ref_date)
                    con.execute("INSERT OR REPLACE INTO demographics VALUES (?,?,?,?,?)",
                                (e, to_iso(res.dob), res.resolved_by, int(res.age_mismatch), json.dumps(r)))
                    n += 1
                counts["Demographics"] = {"rows": n}
            else:
                lower = {c.lower(): c for c in cols}
                ecol = lower.get("empi")
                n = 0
                for r in reader.rows(table):
                    e = r.get(ecol, "").strip()
                    if wanted is not None and e not in wanted:
                        continue
                    found.add(e)
                    con.execute("INSERT INTO structured_rows VALUES (?,?,?)", (logical, e, json.dumps(r)))
                    n += 1
                counts[logical] = {"rows": n, "structured": True}
    finally:
        reader.close()

    # parse dates, site, relevance; collapse duplicates within patient
    for r in notes:
        dt = parse_datetime(r["report_datetime_raw"], not_after=ref_date)
        r["report_datetime"] = to_iso(dt)
        r["site"] = site_of(r["report_type"], r["report_description"])
        r["relevance_class"] = relevance_class(r["source_table"], r["report_description"], r["report_type"])
        r["text_hash"] = text_hash(r["text"])
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in notes:
        groups[(r["empi"], r["text_hash"])].append(r)
    collapsed = Counter()
    per_patient = Counter()
    for (_empi, _h), rs in sorted(groups.items(), key=lambda kv: (kv[0][0], min(x["report_datetime"] or "" for x in kv[1]))):
        rs.sort(key=lambda x: (STATUS_RANK.get(x["report_status"].upper(), 3), x["report_datetime"] or "9999"))
        rep = rs[0]
        cur = con.execute(
            "INSERT INTO documents (empi, source_table, report_number, report_datetime_raw, report_datetime, "
            "report_description, report_type, report_status, site, relevance_class, text, text_hash, n_chars, n_sources) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rep["empi"], rep["source_table"], rep["report_number"], rep["report_datetime_raw"], rep["report_datetime"],
             rep["report_description"], rep["report_type"], rep["report_status"], rep["site"], rep["relevance_class"],
             rep["text"], rep["text_hash"], len(rep["text"]), len(rs)))
        pk = cur.lastrowid
        con.executemany("INSERT INTO document_sources VALUES (?,?,?,?,?,?,?)",
                        [(pk, x["source_table"], x["report_number"], x["report_status"], x["report_datetime_raw"],
                          x["report_description"], x["report_type"]) for x in rs])
        collapsed[rep["source_table"]] += len(rs) - 1
        per_patient[rep["empi"]] += 1
    for t, c in counts.items():
        if not c.get("structured") and t != "Demographics":
            c["documents"] = c["kept"] - collapsed.get(t, 0)
            c["collapsed"] = collapsed.get(t, 0)
    unparsed = sum(1 for r in notes if not r["report_datetime"])
    set_meta(con, "stage_manifest", {
        "tool_version": __version__, "staged_at": datetime.now().isoformat(timespec="seconds"),
        "export_date": ref_date.isoformat(), "counts": counts, "patients_found": len(found),
        "patients_requested": len(wanted) if wanted is not None else None, "unparsed_datetimes": unparsed,
    })
    con.commit()
    con.close()
    _print_summary(stdout, counts, found, wanted, empis, per_patient, unparsed)
    return {"counts": counts, "patients_found": len(found), "per_patient": dict(per_patient)}


def _print_summary(out, counts, found, wanted, empis, per_patient, unparsed) -> None:
    if wanted is not None:
        print(f"Staged {len(found & wanted)} of {len(wanted)} requested patients "
              f"({len(wanted - found)} not found in the source).", file=out)
    else:
        print(f"Staged all {len(found)} patients.", file=out)
    print("\n| Table | Rows (selected pts) | Dropped by status policy | Documents | Exact duplicates collapsed |",
          file=out)
    print("|---|---|---|---|---|", file=out)
    for t, c in sorted(counts.items()):
        if c.get("structured") or t == "Demographics":
            print(f"| {t} | {c['rows']} | – | – | – |", file=out)
        else:
            print(f"| {t} | {c['rows']} | {c['dropped_status']} | {c['documents']} | {c['collapsed']} |", file=out)
    if empis:
        print("\nDocuments per patient (ordinal = line in your EMPI list):", file=out)
        print("  " + ", ".join(f"pt{i}: {per_patient.get(e, 0)}" for i, e in enumerate(empis, 1)), file=out)
    if unparsed:
        print(f"\nWARNING: {unparsed} Report_Date_Time values could not be parsed (counts only).", file=out)
