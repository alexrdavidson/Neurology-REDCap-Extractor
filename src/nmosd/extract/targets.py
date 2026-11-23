"""Which notes the disability (EDSS) field groups read: the REDCap `edss` instrument is filled at each maintenance
treatment start and stop and at the last follow-up (its `timing` field), so the model reads one examination note per
time point instead of every clinic note (16.11.26).

The time points come from the latest reconcile run (treatment start/stop dates), so the EDSS groups run on the
second pass: extract → reconcile → extract → reconcile (the guided app's step 7 does both). Reconcile uses the same
choice of note to date and label each EDSS instance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

EXAM_RX = re.compile(r"(?i)\b(neuro(?:logic(?:al)?)?\s+exam|exam(?:ination)?\b|gait|strength|reflexes|visual acuity|"
                     r"EDSS|ambulat|romberg|sensation|cranial nerves)")
BEFORE_DAYS, AFTER_DAYS = 180, 60     # look back for a baseline examination first, then shortly after
TIMING = {"start": "1", "stop": "2", "last": "3"}   # REDCap `timing` codes (#191)


@dataclass(frozen=True)
class Assessment:
    doc_id: str
    doc_date: date
    timing: str          # REDCap code: 1 treatment start, 2 treatment stop, 3 last follow-up
    anchor: date         # the treatment date or last note it belongs to
    label: str           # e.g. "start of 4 (2019-03-02)"


def pick_assessment_notes(anchors: list[tuple[date, str, str]], notes: list[tuple[str, date]]) -> list[Assessment]:
    """anchors: (date, 'start'|'stop', label); notes: (doc_id, date) of examination notes. One note per anchor
    (the nearest on or before it within BEFORE_DAYS, else the nearest after it within AFTER_DAYS) and the last
    examination note for the last follow-up."""
    out: list[Assessment] = []
    notes = sorted(notes, key=lambda n: (n[1], n[0]))
    for when, kind, label in anchors:
        before = [n for n in notes if 0 <= (when - n[1]).days <= BEFORE_DAYS]
        after = [n for n in notes if 0 < (n[1] - when).days <= AFTER_DAYS]
        pick = before[-1] if before else (after[0] if after else None)
        if pick:
            out.append(Assessment(pick[0], pick[1], TIMING[kind], when, label))
    if notes:
        last = notes[-1]
        out.append(Assessment(last[0], last[1], TIMING["last"], last[1], "last follow-up"))
    return out


def exam_notes(con_d, study_id: str | None = None) -> dict[str, list[tuple[str, date]]]:
    """{study_id: [(doc_id, date)]} of clinical notes with a neurological examination."""
    q = "SELECT doc_id, study_id, report_date, text FROM documents WHERE relevance_class='clinical_note'"
    args: tuple = ()
    if study_id:
        q += " AND study_id=?"
        args = (study_id,)
    out: dict[str, list[tuple[str, date]]] = {}
    for r in con_d.execute(q, args):
        if not r["report_date"] or not EXAM_RX.search(r["text"] or ""):
            continue
        try:
            d = date.fromisoformat(r["report_date"][:10])
        except ValueError:
            continue
        out.setdefault(r["study_id"], []).append((r["doc_id"], d))
    return out


def treatment_anchors(con_x) -> dict[str, list[tuple[date, str, str]]]:
    """Treatment start/stop dates of the latest reconcile run: {study_id: [(date, 'start'|'stop', label)]}."""
    try:
        run = con_x.execute("SELECT MAX(run_id) FROM proposals").fetchone()[0]
    except Exception:   # no proposals table yet
        return {}
    if not run:
        return {}
    drugs: dict[tuple[str, str], str] = {}
    rows = list(con_x.execute("SELECT study_id, instance_key, field, value FROM proposals WHERE run_id=? AND "
                              "instrument='treatment' AND field IN ('maintenance_therapy', 'date_starting_maint', "
                              "'date_stopping_maint')", (run,)))
    for r in rows:
        if r["field"] == "maintenance_therapy":
            drugs[(r["study_id"], r["instance_key"])] = r["value"] or "?"
    out: dict[str, list[tuple[date, str, str]]] = {}
    for r in rows:
        if r["field"] == "maintenance_therapy" or not r["value"]:
            continue
        try:
            d = date.fromisoformat(r["value"][:10])
        except ValueError:
            continue
        kind = "start" if r["field"] == "date_starting_maint" else "stop"
        out.setdefault(r["study_id"], []).append(
            (d, kind, f"{kind} of treatment {drugs.get((r['study_id'], r['instance_key']), '?')} ({d.isoformat()})"))
    return out


def disability_targets(extract_db: Path, con_d) -> set[str]:
    """The notes the EDSS groups read (empty until a reconcile run has found the treatments)."""
    from nmosd.store.db import connect

    if not Path(extract_db).exists():
        return set()
    con_x = connect(extract_db, readonly=True)
    try:
        anchors = treatment_anchors(con_x)
    finally:
        con_x.close()
    if not anchors:
        return set()
    notes = exam_notes(con_d)
    return {a.doc_id for sid, ns in notes.items() for a in pick_assessment_notes(anchors.get(sid, []), ns)}
