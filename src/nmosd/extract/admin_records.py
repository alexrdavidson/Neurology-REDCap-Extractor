"""Infusion and administration records read with text patterns, not the model (18.11.26, the `treatment_admin` group).

The pilot's reference review found that the doses and dates of maintenance infusions sit in two fixed layouts that
the model missed or misread (a drug guessed from a dose line, each infusion taken for a new course):

* the record system's table::

      Administrations This Visit
      ravulizumab-cwvz (ULTOMIRIS) 2,700 mg in sodium chloride 0.9% 54 mL IVPB
      Admin Date      Action     Dose        Rate       Route
      01/10/2025      New Bag    2,700 mg    50 mL/hr   Intravenous

* an infusion nurse's template::

      Med: Ravulizumab (Ultomiris)
      Frequency: every 8 weeks
      Dose: 3,000 mg

Each record becomes one mention of the drug: event "ongoing" on the day it was given (the table's date, else the
note's date), with its dose and frequency. A record is used only when the drug is named: in the table's drug line,
on the template's "Med:" line, or when the note names exactly one maintenance drug. A dose line with no drug named
anywhere gives nothing (the model had guessed rituximab from "300 mg every 26 weeks"). Combine merges the mentions
into the drug's course, takes dose and frequency from them, and treats a first recorded dose that differs from the
later ones as the loading dose, i.e. the start.
"""

from __future__ import annotations

import re
import uuid
from datetime import date

from nmosd.extract.checks import DRUG_NAMES, names_drug
from nmosd.textproc.dates import date_and_estimate
from nmosd.textproc.view import TextView

GROUP = "treatment_admin"
ADMIN_HEAD = re.compile(r"(?im)^[ \t]*Administrations This Visit[ \t]*$")
ADMIN_COLS = re.compile(r"(?i)^\s*Admin Date\b")
DOSE = re.compile(r"(?i)(\d[\d,]*(?:\.\d+)?)\s*(mg|mcg|g|units?|mL)\b")
T_MED = re.compile(r"(?im)^[ \t]*(?:Med(?:ication)?|Drug)[ \t]*:[ \t]*(\S[^\r\n]*)$")
T_DOSE = re.compile(r"(?im)^[ \t]*Dose[ \t]*:[ \t]*(\d[\d,]*(?:\.\d+)?)[ \t]*(mg|mcg|g|units?)\b[^\r\n]*$")
T_FREQ = re.compile(r"(?im)^[ \t]*Frequency[ \t]*:[ \t]*(\S[^\r\n]*?)[ \t.]*$")
MAINTENANCE = [c for c in DRUG_NAMES if c != "16"]   # steroids are pre-medication in an infusion note


def drug_codes(text: str) -> list[str]:
    """REDCap `maintenance_therapy` codes of the maintenance drugs `text` names (not steroids)."""
    return [c for c in MAINTENANCE if names_drug(text, c)]


def frequency_code(text: str | None) -> tuple[str | None, str | None]:
    """(maintenance_tx_freq code, other_frequency_detail) for 'every 8 weeks', 'q6 months', 'daily', ..."""
    t = " ".join((text or "").lower().split()).rstrip(".")
    if not t:
        return None, None
    if re.search(r"\b(twice (?:a |per )?da(?:il)?y|bid|b\.i\.d)\b", t):
        return "1", None
    if re.search(r"\b(once )?(daily|every day|qd)\b", t):
        return "2", None
    m = re.search(r"\b(?:every|q)\s*(\d+)\s*(week|wk|month|mo|day)s?\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        weeks = n if unit.startswith("w") else (n * 52 / 12 if unit.startswith("mo") else n / 7)
        if abs(weeks - 1) < 0.01:
            return "3", None
        if abs(weeks - 2) < 0.01:
            return "4", None
        if 4 <= weeks <= 4.4:
            return "5", None
        if 25.5 <= weeks <= 26.5:
            return "6", None
        return "7", f"every {n} {'week' if unit.startswith('w') else ('month' if unit.startswith('mo') else 'day')}s"
    if re.search(r"\bweekly\b", t):
        return "3", None
    if re.search(r"\bmonthly\b", t):
        return "5", None
    return "7", t[:60]


def _dose(amount: str, unit: str) -> str:
    return f"{amount} {unit}"


def _line(text: str, pos: int) -> tuple[int, int]:
    a = text.rfind("\n", 0, pos) + 1
    b = text.find("\n", pos)
    return a, len(text) if b < 0 else b


def find_records(text: str, doc_date: str | None) -> list[dict]:
    """[{code, date (ISO or None), dose, freq, freq_other, start, end}] for the administration records in `text`."""
    pivot = int(doc_date[:4]) if doc_date and doc_date[:4].isdigit() else date.today().year
    day = (doc_date or "")[:10] or None
    out = []
    used: list[tuple[int, int]] = []
    for h in ADMIN_HEAD.finditer(text):
        lines = [(m.start(), m.end()) for m in re.finditer(r"[^\r\n]*\S[^\r\n]*", text[h.end():h.end() + 1500])]
        lines = [(h.end() + a, h.end() + b) for a, b in lines]
        k = 0
        while k + 1 < len(lines):   # a drug line, the column headings, then one row per administration
            a, b = lines[k]
            if not ADMIN_COLS.match(text[lines[k + 1][0]:lines[k + 1][1]]):
                break
            rows, j = [], k + 2
            while j < len(lines):
                row = text[lines[j][0]:lines[j][1]]
                when = date_and_estimate(row, pivot, max_start=4)
                if not when:
                    break
                rows.append((lines[j], when[0], DOSE.search(row)))
                j += 1
            if not rows:
                break
            drug_line = text[a:b]
            codes = drug_codes(drug_line)
            if len(codes) == 1:      # a maintenance drug, named on the drug line (not Botox, not a pre-medication)
                first = DOSE.search(drug_line)
                for (_ra, rb), iso, dm in rows:
                    dose = _dose(*dm.groups()) if dm else (_dose(*first.groups()) if first else None)
                    out.append({"code": codes[0], "date": iso, "dose": dose, "freq": None, "freq_other": None,
                                "start": a, "end": rb})
                    used.append((a, rb))
            k = j
    for d in T_DOSE.finditer(text):
        if any(a <= d.start() < b for a, b in used):
            continue
        near_a, near_b = max(0, d.start() - 400), min(len(text), d.end() + 400)
        med = next((m for m in T_MED.finditer(text, near_a, near_b)), None)
        codes = drug_codes(med.group(1)) if med else drug_codes(text)
        if len(codes) != 1:
            continue                 # no drug named, or more than one: never guess it from the dose
        fm = next((m for m in T_FREQ.finditer(text, near_a, near_b)), None)
        code, other = frequency_code(fm.group(1)) if fm else (None, None)
        spans = [x for x in (med, fm, d) if x]
        a = min(x.start() for x in spans)
        out.append({"code": codes[0], "date": day, "dose": _dose(d.group(1), d.group(2)), "freq": code,
                    "freq_other": other, "start": _line(text, a)[0], "end": max(x.end() for x in spans)})
    return sorted(out, key=lambda r: r["start"])


def admin_rows(chunk: TextView, *, run_id: str, doc_id: str, chunk_id: str, study_id: str,
               doc_date: str | None) -> list[dict]:
    """Evidence rows (the extraction DB's layout) for the `treatment_admin` group: one item per record."""
    rows = []
    for rec in find_records(chunk.text, doc_date):
        item = uuid.uuid4().hex[:16]
        start = chunk.to_original(rec["start"])
        end = chunk.to_original(max(rec["start"], rec["end"] - 1)) + 1
        quote = " ".join(chunk.text[rec["start"]:rec["end"]].split())[:300]
        base = {"run_id": run_id, "doc_id": doc_id, "chunk_id": chunk_id, "study_id": study_id, "field_group": GROUP,
                "item_id": item, "event_date": rec["date"], "estimate_code": "1" if rec["date"] else None,
                "quote": quote, "char_start": start, "char_end": end, "certainty": "high", "negated": 0,
                "hypothetical": 0, "verified": 1, "verify_score": 1.0, "doc_date": doc_date}
        values = {"maintenance_therapy": rec["code"], "event": "ongoing", "maintenance_tx_dosing": rec["dose"],
                  "maintenance_tx_freq": rec["freq"], "other_frequency_detail": rec["freq_other"]}
        rows += [base | {"evidence_id": uuid.uuid4().hex, "field": f, "value": v} for f, v in values.items() if v]
    return rows
