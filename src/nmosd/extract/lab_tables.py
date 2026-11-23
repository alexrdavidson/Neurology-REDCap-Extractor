"""Laboratory table rows read with text patterns, not the model (18.11.26, the `labs_table` group).

`nmosd lab-shapes` over the export (1,526 notes, 32 patients; masked shapes only) showed that the results the
codebook needs are pasted into notes as table rows in a few fixed layouts (there is no structured Lab table)::

    IGG                  612 (L)           03/14/2024          name | value (flag) | date
    IMMUNOGLOBULIN G     612    700 - 1600    mg/dL    Final   name | value | range | unit | status
    IgA                  85 mg/dL          70 - 400 mg/dL      name | value unit | range
    ABSOLUTE LYMPHS      1.2    1.0 - 4.8  K/uL    Final
    CD19 ABS             <1 (L)            03/14/2024
    NMO/AQP4 FACS, S     Reactive                              name | result [| titre]
    MOG FACS, S          Negative

A row is used only when its first cell is exactly one of the test names below (so "Anticardiolipin IgG" or
"IgG index" is not an immunoglobulin level) and its value parses. The date is the row's own date cell, else a
"Collected: <date>" line just above, else none (Combine then dates the result by the note and says so). Values keep
the unit the row gives; without one, immunoglobulins are taken as mg/dL only inside a plausible range.

CD19 and CD20 (the clinician's rules, 18.11.26). Laboratories report both an absolute count and a percentage for
each marker, so the marker's name and a number are not enough:

* a row named only `CD19` or `CD20` is read as a count or a percentage only when its unit says so (`cells/uL`,
  `%`), when the row gives both (the number beside a percentage is the count), or when its column heading says so;
  otherwise the raw result is kept as `cd19_unspecified` / `cd20_unspecified`, and Combine flags it without
  entering it;
* flow-panel rows `CD19+CD20-` and `CD19+CD20+` are subsets (`cd19p20n_*`, `cd19p20p_*`). Combine derives the total
  CD19+ count as their sum only when both come from the same note and date and no total is reported; it never
  enters `CD19+CD20+` as the total CD20+ count (that row may omit CD19-CD20+ cells), but shows it to the reviewer.
"""

from __future__ import annotations

import re
import uuid
from datetime import date

from nmosd.textproc.dates import date_and_estimate
from nmosd.textproc.view import TextView

GROUP = "labs_table"
COL = re.compile(r" {2,}|\t+")
_SUFFIX = r"(?:,? ?(?:serum|s|quant(?:itative)?|level|total))*"
NAMES: list[tuple[str, re.Pattern]] = [(t, re.compile(rf"(?i)^(?:{p}){_SUFFIX}$")) for t, p in [
    ("igg", r"IgG|immunoglobulin G"), ("iga", r"IgA|immunoglobulin A"), ("igm", r"IgM|immunoglobulin M"),
    ("alc", r"abs(?:olute)?\.? lymph(?:s|ocytes?)?|lymph(?:s|ocytes?)?,? ?(?:abs(?:olute)?|#)(?: count)?|ALC"),
    ("cd19_abs", r"CD ?19,? ?(?:abs(?:olute)?(?: count)?|#|count)|abs(?:olute)? CD ?19"),
    ("cd20_abs", r"CD ?20,? ?(?:abs(?:olute)?(?: count)?|#|count)|abs(?:olute)? CD ?20"),
    ("cd19_pct", r"CD ?19,? ?(?:%|percent|pct)|% ?CD ?19"),
    ("cd20_pct", r"CD ?20,? ?(?:%|percent|pct)|% ?CD ?20"),
    ("cd19p20n", r"CD ?19 ?\+ ?/? ?CD ?20 ?[-\u2212\u2013]"), ("cd19p20p", r"CD ?19 ?\+ ?/? ?CD ?20 ?\+"),
    ("cd19", r"CD ?19"), ("cd20", r"CD ?20"),      # the marker alone: count or percent must come from the row
]]
CD_MARKERS = ("cd19", "cd20", "cd19p20n", "cd19p20p")
ABS_UNITS = {"cells/ul", "/ul", "cells/mcl", "cells/mm3"}
ANTIBODY: list[tuple[str, re.Pattern]] = [
    ("aqp4", re.compile(r"(?i)^(?:NMO ?/ ?AQP-?4|AQP-?4|aquaporin[- ]?4|NMO)[- ]?(?:FACS|IgG|Ab|antibod(?:y|ies)|receptor ab)?"
                        r"(?: titer| titre)?(?:,? ?(?:serum|s))?$")),
    ("mog", re.compile(r"(?i)^(?:MOG|myelin oligodendrocyte glycoprotein)[- ]?(?:FACS|IgG\d?|Ab|antibod(?:y|ies))?"
                       r"(?: titer| titre)?(?:,? ?(?:serum|s))?$")),
]
RESULT = re.compile(r"(?i)^(reactive|positive|detected|negative|non-?reactive|not detected|undetectable)\b")
# a value cell: a number, perhaps with < or >, a unit and a flag such as (L), and nothing else (a range is not one)
VALUE = re.compile(r"(?i)^\s*([<>]=?)?\s*(\d+(?:\.\d+)?)\s*(mg/dL|g/L|mg/L|K/uL|x10\^?3/uL|10\*3/uL|cells/uL|"
                   r"cells/mcL|cells/mm3|/uL|%)?\s*(?:\([A-Za-z*!]{1,3}\))?\s*$")
UNIT = re.compile(r"(?i)^(mg/dL|g/L|mg/L|K/uL|x10\^?3/uL|10\*3/uL|thou/uL|cells/uL|/uL|%)$")
TITER = re.compile(r"1\s*:\s*[\d,]+")
COLLECTED = re.compile(r"(?i)\bCollected\s*:\s*(\S[^\r\n]{0,30})")
PLAUSIBLE_MG_DL = {"igg": (50, 6000), "iga": (5, 2000), "igm": (5, 4000)}


def _test_of(cell: str) -> str | None:
    c = " ".join(cell.split())
    return next((t for t, rx in NAMES if rx.match(c)), None)


def _antibody_of(cell: str) -> str | None:
    c = " ".join(cell.split())
    return next((t for t, rx in ANTIBODY if rx.match(c)), None)


def _heading_kind(text: str, lines: list[tuple[int, int]], k: int, col: int) -> str | None:
    """"abs" or "pct" when the table's heading row (the nearest line above without a number) labels column `col`
    as a count or a percentage; None when there is no such heading or it does not say."""
    for a, b in reversed(lines[max(0, k - 8):k]):
        line = text[a:b].strip()
        if re.search(r"\d", line):
            continue
        cells = [c.strip() for c in COL.split(line) if c.strip()]
        if len(cells) <= col:
            return None
        if re.search(r"(?i)%|percent", cells[col]):
            return "pct"
        return "abs" if re.search(r"(?i)\babs|count|cells|#|/uL", cells[col]) else None
    return None


def _cd_records(marker: str, cells: list[str], text: str, lines: list[tuple[int, int]], k: int) -> list[dict]:
    """The count and/or percentage a CD19/CD20 row gives, never assumed: by unit, by the row giving both (the number
    beside a percentage is the count), or by the column heading; otherwise the raw number as `<marker>_unspecified`."""
    nums = [(i, m) for i, c in enumerate(cells[1:], 1) for m in [VALUE.match(c)] if m]
    if not nums:
        return []
    unit = lambda m: (m.group(3) or "").lower()  # noqa: E731
    pct = next(((i, m) for i, m in nums if unit(m) == "%"), None)
    cnt = next(((i, m) for i, m in nums if unit(m) in ABS_UNITS), None)
    bare = [(i, m) for i, m in nums if not unit(m)]
    if cnt is None and pct is not None and bare:
        cnt = bare[0]
    out = []
    if cnt is None and pct is None and bare:
        i, m = bare[0]
        kind = _heading_kind(text, lines, k, i)
        out.append({"test": f"{marker}_{kind or 'unspecified'}", "value": f"{m.group(1) or ''}{m.group(2)}",
                    "unit": "%" if kind == "pct" else ""})
    if cnt is not None:
        out.append({"test": f"{marker}_abs", "value": f"{cnt[1].group(1) or ''}{cnt[1].group(2)}",
                    "unit": cnt[1].group(3) or ""})
    if pct is not None:
        out.append({"test": f"{marker}_pct", "value": f"{pct[1].group(1) or ''}{pct[1].group(2)}", "unit": "%"})
    return out


def find_lab_rows(text: str, doc_date: str | None) -> list[dict]:
    """One dict per table row read: {test|antibody, value, unit, result, titer, method, date, start, end}."""
    pivot = int(doc_date[:4]) if doc_date and doc_date[:4].isdigit() else date.today().year
    out = []
    lines = [(m.start(), m.end()) for m in re.finditer(r"[^\r\n]*\S[^\r\n]*", text)]
    for k, (a, b) in enumerate(lines):
        line = text[a:b].strip()
        if len(line) > 200:
            continue
        cells = [c.strip() for c in COL.split(line) if c.strip()]
        if len(cells) < 2:
            continue
        test, ab = _test_of(cells[0]), _antibody_of(cells[0])
        if not test and not ab:
            continue
        when = next((d[0] for c in cells[1:] for d in [date_and_estimate(c, pivot, max_start=0)] if d and d[1] == "1"),
                    None)
        if when is None:   # a "Collected: <date>" line of this result block, just above
            for pa, pb in reversed(lines[max(0, k - 12):k]):
                m = COLLECTED.search(text[pa:pb])
                if m:
                    got = date_and_estimate(m.group(1), pivot, max_start=0)
                    when = got[0] if got and got[1] == "1" else None
                    break
        row = {"date": when, "start": a + (len(text[a:b]) - len(text[a:b].lstrip())), "end": b}
        if ab:
            res = RESULT.match(cells[1])
            if not res:
                continue
            word = res.group(1).lower()
            titer = next((m.group(0).replace(" ", "") for c in cells[1:] for m in [TITER.search(c)] if m), None)
            out.append(row | {"antibody": ab, "titer": titer,
                              "result": "positive" if word in ("reactive", "positive", "detected") else "negative",
                              "method": "1" if re.search(r"(?i)FACS|cell[- ]based|CBA", cells[0]) else None})
            continue
        if test in CD_MARKERS:
            out += [row | rec for rec in _cd_records(test, cells, text, lines, k)]
            continue
        m = VALUE.match(cells[1])
        if not m:
            continue
        op, num, unit = m.group(1) or "", m.group(2), m.group(3)
        unit = unit or next((c for c in cells[2:] if UNIT.match(c)), None) or ("%" if test.endswith("_pct") else None)
        v = float(num)
        if test in PLAUSIBLE_MG_DL and not unit and not PLAUSIBLE_MG_DL[test][0] <= v <= PLAUSIBLE_MG_DL[test][1]:
            continue          # no unit and not a plausible mg/dL value: not guessed
        if test == "alc" and not unit:
            unit = "K/uL" if v < 30 else "cells/uL"
        out.append(row | {"test": test, "value": f"{op}{num}", "unit": unit or ("mg/dL" if test in PLAUSIBLE_MG_DL else "")})
    return out


def lab_table_rows(chunk: TextView, *, run_id: str, doc_id: str, chunk_id: str, study_id: str,
                   doc_date: str | None) -> list[dict]:
    """Evidence rows (the extraction DB's layout) for the `labs_table` group: one item per table row."""
    rows = []
    for rec in find_lab_rows(chunk.text, doc_date):
        item = uuid.uuid4().hex[:16]
        start = chunk.to_original(rec["start"])
        end = chunk.to_original(max(rec["start"], rec["end"] - 1)) + 1
        base = {"run_id": run_id, "doc_id": doc_id, "chunk_id": chunk_id, "study_id": study_id, "field_group": GROUP,
                "item_id": item, "event_date": rec["date"], "estimate_code": "1" if rec["date"] else None,
                "quote": " ".join(chunk.text[rec["start"]:rec["end"]].split())[:300], "char_start": start,
                "char_end": end, "certainty": "high", "negated": 0, "hypothetical": 0, "verified": 1,
                "verify_score": 1.0, "doc_date": doc_date}
        if "antibody" in rec:
            values = {"type_test": rec["antibody"], "result": rec["result"], "titer": rec["titer"],
                      "assay_method": rec["method"], "assay_date": rec["date"]}
        else:
            values = {"test": rec["test"], "value": rec["value"], "unit": rec["unit"], "test_date": rec["date"]}
        rows += [base | {"evidence_id": uuid.uuid4().hex, "field": f, "value": v} for f, v in values.items() if v]
    return rows
