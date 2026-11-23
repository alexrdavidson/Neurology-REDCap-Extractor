"""Dated header and list lines read with text patterns, not the model (18.11.26). Layouts from `nmosd shapes` over
the export (1,526 notes, 32 patients; masked shapes and counts only).

* `dx_dates`: the problem-list or visit-diagnosis row that names NMOSD with its date, e.g.
  ``Neuromyelitis optica    G36.0    03/14/2019 -`` (249 notes, 28 patients). The date is when the problem was
  entered, so the diagnosis was on or before it: Combine uses the earliest such date for `date_nmo_diagnosis` only
  when no note states a diagnosis date, with estimate code 4, and says where it came from.
* `admission_dates`: ``Admit date: 01/05/2025`` and ``Discharge date: 01/19/2025`` at the top of a discharge
  summary (8 patients). One item per note with both dates; Combine adds it to the stay it belongs to, before what
  the model read (the pilot's reference review found a discharge date taken from a progress note mid-stay).
"""

from __future__ import annotations

import re
import uuid
from datetime import date

from nmosd.textproc.dates import date_and_estimate
from nmosd.textproc.view import TextView

COL = re.compile(r" {2,}|\t+")
NMO_NAME = re.compile(r"(?i)^(?:neuromyelitis optica(?: spectrum disorders?)?|NMOSD|NMO spectrum disorder)"
                      r"(?: \((?:primary|devic(?:'s)?(?: disease)?)\))?$")
ADMIT = re.compile(r"(?im)^[ \t]*(?:Admit(?:ted)?|Admission)(?: Date)?[ \t]*:[ \t]*(\S[^\r\n]{0,24})")
DISCHARGE = re.compile(r"(?im)^[ \t]*Discharged?(?: Date)?[ \t]*:[ \t]*(\S[^\r\n]{0,24})")


def _pivot(doc_date: str | None) -> int:
    return int(doc_date[:4]) if doc_date and doc_date[:4].isdigit() else date.today().year


def _exact(text: str, pivot: int) -> str | None:
    got = date_and_estimate(text, pivot, max_start=0)
    return got[0] if got and got[1] == "1" else None


def find_dx_rows(text: str, doc_date: str | None) -> list[dict]:
    """[{date, start, end}] for the rows that name NMOSD in one cell and give a full date in a later cell."""
    pivot, out = _pivot(doc_date), []
    for m in re.finditer(r"[^\r\n]*\S[^\r\n]*", text):
        line = m.group()
        if len(line) > 200 or ("optica" not in line.lower() and "nmo" not in line.lower()):
            continue
        cells = [c.strip() for c in COL.split(line.strip()) if c.strip()]
        k = next((i for i, c in enumerate(cells) if NMO_NAME.match(c)), None)
        if k is None:
            continue
        when = next((d for c in cells[k + 1:] for d in [_exact(c, pivot)] if d), None)
        if when and (not doc_date or when <= doc_date[:10]):
            out.append({"date": when, "start": m.start() + (len(line) - len(line.lstrip())), "end": m.end()})
    return out


def find_admission_dates(text: str, doc_date: str | None) -> dict | None:
    """{admission, discharge, start, end} from the note's "Admit date:" / "Discharge date:" lines (either may be
    missing; None when neither is a full date)."""
    pivot = _pivot(doc_date)
    a = next(((m, d) for m in ADMIT.finditer(text) for d in [_exact(m.group(1), pivot)] if d), None)
    b = next(((m, d) for m in DISCHARGE.finditer(text) for d in [_exact(m.group(1), pivot)] if d), None)
    if not a and not b:
        return None
    if a and b and b[1] < a[1]:
        return None                # a discharge before the admission: not one stay's header
    spans = [x[0] for x in (a, b) if x]
    return {"admission": a[1] if a else None, "discharge": b[1] if b else None,
            "start": min(m.start() for m in spans), "end": max(m.end() for m in spans)}


def _base(chunk: TextView, group: str, start: int, end: int, when: str | None, *, run_id: str, doc_id: str,
          chunk_id: str, study_id: str, doc_date: str | None) -> dict:
    return {"run_id": run_id, "doc_id": doc_id, "chunk_id": chunk_id, "study_id": study_id, "field_group": group,
            "item_id": uuid.uuid4().hex[:16], "event_date": when, "estimate_code": "1" if when else None,
            "quote": " ".join(chunk.text[start:end].split())[:300], "char_start": chunk.to_original(start),
            "char_end": chunk.to_original(max(start, end - 1)) + 1, "certainty": "high", "negated": 0,
            "hypothetical": 0, "verified": 1, "verify_score": 1.0, "doc_date": doc_date}


def dx_date_rows(chunk: TextView, **kw) -> list[dict]:
    """Evidence rows for the `dx_dates` group: one item per problem-list row."""
    return [_base(chunk, "dx_dates", r["start"], r["end"], r["date"], **kw)
            | {"evidence_id": uuid.uuid4().hex, "field": "problem_noted", "value": r["date"]}
            for r in find_dx_rows(chunk.text, kw.get("doc_date"))]


def admission_date_rows(chunk: TextView, **kw) -> list[dict]:
    """Evidence rows for the `admission_dates` group: one item per note."""
    rec = find_admission_dates(chunk.text, kw.get("doc_date"))
    if not rec:
        return []
    base = _base(chunk, "admission_dates", rec["start"], rec["end"], rec["admission"] or rec["discharge"], **kw)
    return [base | {"evidence_id": uuid.uuid4().hex, "field": f, "value": v}
            for f, v in (("nr_admission", rec["admission"]), ("nr_discharge", rec["discharge"])) if v]
