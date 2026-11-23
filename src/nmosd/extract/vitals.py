"""Weight and height (REDCap #8 `weight`, #9 `height`) read from the vitals lines of clinical notes with text
patterns, not the model: they are written in a few fixed ways ("Wt 68.2 kg", "Weight: 150 lb", "Ht 5' 6\"",
"Height 167.6 cm"), and a pattern is exact, free and gives its own quote. Values are converted to kg and cm (one
decimal) and implausible ones are dropped. Reconcile proposes the measurement nearest the NMOSD diagnosis date.
"""

from __future__ import annotations

import re
import uuid

from nmosd.textproc.view import TextView

# 18.11.26: also "WEIGHT(lbs)   150 lbs" (the unit in brackets after the label), from `nmosd shapes`
WEIGHT = re.compile(r"(?i)\b(?:wt|weight)\b\.?\s*(?:\((?:lbs?|kgs?)\))?\s*[:=]?\s*(\d{2,3}(?:\.\d{1,2})?)\s*"
                    r"(kg|kilograms?|lbs?|pounds?)\b")
HEIGHT = re.compile(r"(?i)\b(?:ht|height)\b\.?\s*[:=]?\s*(?:"
                    r"(?P<ft>[4-7])\s*(?:'|ft|feet|foot)\s*(?P<inch>\d{1,2}(?:\.\d+)?)?\s*(?:\"|''|in(?:ches)?)?"
                    r"|(?P<cm>1\d{2}(?:\.\d+)?)\s*cm\b"
                    r"|(?P<m>[12]\.\d{1,2})\s*m\b"
                    r"|(?P<in>[4-8]\d(?:\.\d+)?)\s*(?:\"|in(?:ches)?)(?![a-z]))")
KG_RANGE, CM_RANGE = (25.0, 300.0), (120.0, 220.0)


def _weight_kg(m: re.Match) -> float | None:
    v, unit = float(m.group(1)), m.group(2).lower()
    kg = v if unit.startswith("k") else v * 0.45359237
    return round(kg, 1) if KG_RANGE[0] <= kg <= KG_RANGE[1] else None


def _height_cm(m: re.Match) -> float | None:
    g = m.groupdict()
    if g["ft"]:
        cm = (int(g["ft"]) * 12 + float(g["inch"] or 0)) * 2.54
    elif g["cm"]:
        cm = float(g["cm"])
    elif g["m"]:
        cm = float(g["m"]) * 100
    elif g["in"]:
        cm = float(g["in"]) * 2.54
    else:
        return None
    return round(cm, 1) if CM_RANGE[0] <= cm <= CM_RANGE[1] else None


def find_vitals(text: str) -> list[tuple[str, float, int, int]]:
    """(field, value, start, end) for every weight and height written in `text`."""
    out = []
    for rx, field, conv in ((WEIGHT, "weight", _weight_kg), (HEIGHT, "height", _height_cm)):
        for m in rx.finditer(text):
            v = conv(m)
            if v is not None:
                out.append((field, v, m.start(), m.end()))
    return sorted(out, key=lambda x: x[2])


def vitals_rows(chunk: TextView, *, run_id: str, doc_id: str, chunk_id: str, study_id: str,
                doc_date: str | None) -> list[dict]:
    """Evidence rows (the extraction DB's layout) for the `vitals` group, dated by the note."""
    rows = []
    day = (doc_date or "")[:10] or None
    for field, value, a, b in find_vitals(chunk.text):
        start, end = chunk.to_original(a), chunk.to_original(max(a, b - 1)) + 1
        rows.append({"evidence_id": uuid.uuid4().hex, "run_id": run_id, "doc_id": doc_id, "chunk_id": chunk_id,
                     "study_id": study_id, "field_group": "vitals", "item_id": uuid.uuid4().hex[:16], "field": field,
                     "value": f"{value:.1f}", "event_date": day, "estimate_code": "1" if day else None,
                     "quote": chunk.text[a:b], "char_start": start, "char_end": end, "certainty": "high",
                     "negated": 0, "hypothetical": 0, "verified": 1, "verify_score": 1.0, "doc_date": doc_date})
    return rows
