"""`nmosd lab-shapes`: how are the laboratory results the codebook needs written in the notes? Masked shapes and
counts only, never a value (18.11.26).

The 5 pilot patients' notes hold almost no CD19/CD20, immunoglobulin or lymphocyte results in tables, so a reader
for such tables could not be written from them. This command looks through every note of a database (the staging
or the de-identified one: all patients) for lines that mention those tests or an AQP4/MOG antibody result, and
prints for each family of tests how many notes and patients have such a line and the most common *shapes* of the
lines: digits become 9, letters A/Aa/a (a run of any length is one mark), and only laboratory words that cannot
identify anyone (test names, units, column headings, result words) are kept as written. A shape is shown only when
at least `min_notes` notes of at least `min_patients` different patients share it, so a line that occurs once
never appears. Nothing else is printed: safe to paste.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import TextIO

from nmosd.store.db import connect

FAMILIES = {
    "CD19 / CD20 / B cells": r"\bCD[ -]?(?:19|20)\b|\bB[- ]?cells?\b",
    "Immunoglobulins (IgG, IgA, IgM)": r"(?<![-\w])Ig[GAM]\b(?![- ]?(?:index|synth|antibod|ab\b))|\bimmunoglobulin[s ]+(?:[GAM]\b|level|quant)",
    "Lymphocyte count": r"\babs(?:olute)?\.? lymph|\blymph\w*,? ?(?:abs|#|count)|\bALC\b",
    "AQP4 antibody": r"\bAQP-?4\b|\baquaporin|\bNMO[- /]?(?:IgG|Ab|antibod)",
    "MOG antibody": r"\bMOG\b|myelin oligodendrocyte",
}
# Laboratory words kept as written (compared in lower case). Everything else is masked.
KEEP = {w.lower() for w in ["CD", "B", "cell", "cells", "lymphocyte", "lymphocytes", "lymphs", "lymph", "absolute", "abs", "count", "counts", "percent", "pct", "total", "serum", "plasma", "blood", "csf", "IgG", "IgA", "IgM", "IgE", "immunoglobulin", "immunoglobulins", "quantitative", "quant", "level", "levels", "AQP", "NMO", "MOG", "aquaporin", "myelin", "oligodendrocyte", "glycoprotein", "antibody", "antibodies", "ab", "FACS", "CBA", "ELISA", "assay", "cell", "based", "titer", "titre", "screen", "reflex", "receptor", "positive", "negative", "reactive", "nonreactive", "detected", "undetectable", "pending", "normal", "abnormal", "high", "low", "H", "L", "component", "value", "result", "results", "flag", "ref", "reference", "range", "latest", "units", "unit", "date", "time", "collected", "resulted", "status", "final", "lab", "labs", "laboratory", "test", "tests", "name", "specimen", "source", "method", "comment", "mg", "dL", "g", "uL", "mL", "mcL", "K", "mm", "cmm", "per", "of", "to", "and", "or", "with", "the", "in", "on", "at", "is", "was", "were", "not", "no", "none", "Mayo", "ARUP", "Quest", "LabCorp"]}
# Other structured data in the notes (`nmosd shapes`, 18.11.26): the same search for the blocks the other REDCap
# instruments are filled from. Drug, test and diagnosis names and table headings cannot identify anyone.
_DRUGS = (r"rituximab|rituxan|truxima|ruxience|riabni|mycophenolate|cellcept|eculizumab|soliris|ravulizumab|ultomiris|"
          r"inebilizumab|uplizna|satralizumab|enspryng|azathioprine|imuran|tocilizumab|actemra|methotrexate|"
          r"cyclophosphamide|cytoxan|interferon|avonex|natalizumab|tysabri|ocrelizumab|ocrevus|ofatumumab|kesimpta|"
          r"prednisone|methylprednisolone|solu-?medrol|immune globulin|IVIG|plasma exchange|PLEX|plasmapheresis")
OTHER_FAMILIES = {
    "Immunotherapy: administrations, orders, medication-list rows":
        rf"Administrations This Visit|\bAdmin Date\b|^\s*(?:Med(?:ication)?|Drug|Dose|Frequency|Pre-?meds?[^:]{{0,12}})\s*:"
        rf"|\bNew Bag\b|\bmL/hr\b|\b(?:{_DRUGS})\b",
    "Weight and height": r"\b(?:wt|weight|ht|height|BMI)\b",
    "Diagnosis and problem-list rows": r"neuromyelitis|\bNMOSD\b|\bNMO\b|\bDevic|\bG36\.0\b|\bMOGAD\b",
    "Disability scores (EDSS, mRS, timed walk)": r"\bEDSS\b|\bmRS\b|modified Rankin|\bT25FW\b|timed 25|25[- ]foot|9[- ]hole",
    "MRI study lines": r"^\s*(?:EXAM(?:INATION)?\s*:\s*)?(?:MRI?|MR)\b[^.]{0,90}$",
    "Attack lists": r"^\s*(?:Attacks?|Relapses?|(?:Relapse|Attack) history)\s*[:#]|^\s*(?:#?\d+[.)]|[-*])\s*[^.]{0,70}"
                    r"\b(?:optic neuritis|myelitis|LETM|area postrema)\b",
    "Admissions and discharges": r"^\s*(?:Admi(?:t|ssion)|Discharge|Date of (?:Admission|Discharge)|Length of Stay|LOS)"
                                 r"(?: Date)?\s*:",
}
KEEP |= {w.lower() for w in (_DRUGS.replace("-?", "").replace("|", " ") + """
administrations this visit admin action dose rate route documented by new bag given stopped held intravenous IV IVPB
infusion infused sodium chloride premeds pre meds med medication medications frequency every week weeks month months
day days daily twice once tablet tablets capsule capsules oral PO injection subcutaneous start end started
discontinued ordered order sig take refills disp hr mL for as needed PRN x number dx diagnosis diagnoses
wt weight ht height BMI kg lb lbs cm inches ft body mass index vitals vital signs BP pulse temp
neuromyelitis optica spectrum disorder NMOSD Devic MOGAD primary principal problem list noted onset resolved active
chronic priority visit encounter hospital ICD code G
EDSS mRS modified Rankin score scale timed foot walk hole peg seconds sec T FW
MRI MR brain spine cervical thoracic lumbar orbits orbit face head without contrast w wo exam examination study
impression findings technique comparison accession angio
attack attacks relapse relapses history optic neuritis myelitis LETM area postrema ON TM brainstem left right
bilateral OD OS OU treated IVMP steroids recovery complete partial hospitalized admitted admit admission
discharge discharged length stay LOS
""").split()}
_WORD = re.compile(r"[A-Za-z]+|\d+(?:[.,]\d+)*")
MAX_SHAPE = 130
TABLE_GAP = re.compile(r"\S(?: {2,}|\t+)\S")


def shape(line: str) -> str:
    """Mask one line: numbers -> 9, letters -> A / Aa / a (laboratory words kept); punctuation kept, spacing kept as
    one space or ` | ` for a column gap."""
    def one(m: re.Match) -> str:
        w = m.group()
        if w[0].isdigit():
            return "9"
        if w.lower() in KEEP:
            return w
        return "A" if w.isupper() else ("a" if w.islower() else "Aa")
    s = re.sub(r"(?: {2,}|\t+)", " | ", line.strip())
    s = re.sub(r"\[\[[^\]]*\]\]", "<#>", s)                       # a packet's date token
    s = re.sub(r"\[[A-Z][A-Z_]*(?:_\d+)?(?: 90\+)?\]", "[#]", s)    # a de-identification token ([PATIENT], ...)
    s = _WORD.sub(one, s)
    s = re.sub(r"(?:\b(?:A|Aa|a)\b[ ,]*){2,}", "a… ", s)           # a run of masked words is one mark
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= MAX_SHAPE else s[:MAX_SHAPE] + "…"


def _notes_from_db(db: Path):
    """(patient, note text) from a staging or de-identified database."""
    con = connect(db, readonly=True)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        who = "study_id" if "study_id" in cols else "empi"
        for r in con.execute(f"SELECT {who} AS who, text FROM documents"):
            yield r["who"], r["text"]
    finally:
        con.close()


def _notes_from_source(reader):
    """(patient, note text) from every note table of the source export (all patients, nothing staged)."""
    from nmosd.ingest.column_map import NOTE_COLUMNS, classify_table, resolve

    for table in reader.tables():
        cols = reader.columns(table)
        if classify_table(table, cols)[0] != "note":
            continue
        cm = resolve(cols, NOTE_COLUMNS)
        if not cm["empi"] or not cm["text"]:
            continue
        for r in reader.rows(table):
            yield (r.get(cm["empi"]) or "").strip(), r.get(cm["text"]) or ""


# columns of a structured laboratory table that name the test (never a result, a comment, a date or an identifier)
TEST_NAME_COL = re.compile(r"(?i)(test|group|component|loinc|proc|description|name)")
NOT_NAME_COL = re.compile(r"(?i)(result|value|comment|text|date|time|empi|mrn|epic|patient|provider|clinic|hospital|"
                          r"accession|flag|range|unit|specimen|status)")


def lab_table_tests(reader, min_rows: int, min_patients: int, top: int, stdout: TextIO) -> dict:
    """For each structured laboratory table of the source: its column names, and the test names (never results)
    that match the tests the codebook needs, with how many rows and patients have them."""
    from nmosd.ingest.column_map import classify_table

    out = {}
    rx = {name: re.compile(p, re.I) for name, p in FAMILIES.items()}
    for table in reader.tables():
        cols = reader.columns(table)
        kind, logical = classify_table(table, cols)
        if kind != "structured" or not re.search(r"(?i)lab", f"{table} {logical}"):
            continue
        name_cols = [c for c in cols if TEST_NAME_COL.search(c) and not NOT_NAME_COL.search(c)]
        empi = next((c for c in cols if c.lower() == "empi"), None)
        seen: dict[tuple[str, str, str], tuple[int, set]] = {}
        n_rows, pats = 0, set()
        for r in reader.rows(table):
            n_rows += 1
            who = (r.get(empi) or "").strip() if empi else ""
            pats.add(who)
            for c in name_cols:
                v = " ".join(str(r.get(c) or "").split())[:80]
                if not v:
                    continue
                for fam, pat in rx.items():
                    if pat.search(v):
                        n, ps = seen.get((fam, c, v), (0, set()))
                        ps.add(who)
                        seen[(fam, c, v)] = (n + 1, ps)
        out[table] = {"rows": n_rows, "patients": len(pats), "columns": cols}
        print(f"## Laboratory table `{table}`: {n_rows} rows, {len(pats)} patients", file=stdout)
        print("Columns: " + ", ".join(f"`{c}`" for c in cols), file=stdout)
        print(f"Test names (from {', '.join(f'`{c}`' for c in name_cols) or 'no name column found'}) that match the "
              f"tests the codebook needs; shown when at least {min_rows} rows of at least {min_patients} patients "
              "have them. No results are read.", file=stdout)
        for fam in FAMILIES:
            rows = sorted(((n, len(ps), c, v) for (f, c, v), (n, ps) in seen.items()
                           if f == fam and n >= min_rows and len(ps) >= min_patients), reverse=True)[:top]
            print(f"\n{fam}:" + ("" if rows else " none"), file=stdout)
            for n, np_, c, v in rows:
                print(f"- {n} rows, {np_} patients · `{c}` = `{v}`", file=stdout)
        print("", file=stdout)
    if not out:
        print("## No structured laboratory table in this source (a table named Lab or Labs with an EMPI column).\n",
              file=stdout)
    return out


def structured_tables(reader, min_rows: int, min_patients: int, stdout: TextIO) -> dict:
    """Every structured (non-note) table of the export: rows, patients and column NAMES; for a medication table also
    how many rows and patients name each maintenance drug (drug names only). No other value is read or printed."""
    from nmosd.extract.checks import DRUG_NAMES, names_drug
    from nmosd.ingest.column_map import classify_table

    out = {}
    print("## Structured tables in the export (names and counts only)\n", file=stdout)
    for table in reader.tables():
        cols = reader.columns(table)
        kind, logical = classify_table(table, cols)
        if kind != "structured":
            continue
        empi = next((c for c in cols if c.lower() == "empi"), None)
        med_cols = [c for c in cols if re.search(r"(?i)medication|drug", c) and not NOT_NAME_COL.search(c)]
        n_rows, pats = 0, set()
        drugs: dict[str, tuple[int, set]] = {}
        for r in reader.rows(table):
            n_rows += 1
            who = (r.get(empi) or "").strip() if empi else ""
            pats.add(who)
            for c in med_cols:
                v = str(r.get(c) or "")
                for code in DRUG_NAMES:
                    if names_drug(v, code):
                        n, ps = drugs.get(code, (0, set()))
                        ps.add(who)
                        drugs[code] = (n + 1, ps)
        out[table] = {"rows": n_rows, "patients": len(pats), "columns": cols}
        print(f"- `{table}` ({logical}): {n_rows} rows, {len(pats)} patients. Columns: "
              + ", ".join(f"`{c}`" for c in cols), file=stdout)
        shown = [(n, len(ps), code) for code, (n, ps) in drugs.items() if n >= min_rows and len(ps) >= min_patients]
        for n, np_, code in sorted(shown, reverse=True):
            print(f"  - {DRUG_NAMES[code][0]}: {n} rows, {np_} patients", file=stdout)
    if not out:
        print("- none", file=stdout)
    print("", file=stdout)
    return out


def run_lab_shapes(db: Path | None = None, min_notes: int = 3, min_patients: int = 2, top: int = 12,
                   stdout: TextIO | None = None, source: Path | None = None, backend: str = "auto",
                   everything: bool = False) -> dict:
    """db: a staging or de-identified database (the patients staged so far). source: the export itself (every
    patient), which also lists the test names of its laboratory table. everything (`nmosd shapes`): also the other
    structured data in the notes (OTHER_FAMILIES) and, with a source, every structured table's columns."""
    stdout = stdout or sys.stdout
    families = {**FAMILIES, **OTHER_FAMILIES} if everything else FAMILIES
    reader = None
    if source is not None:
        from nmosd.ingest.access_reader import open_source

        reader = open_source(source, backend)
    rx = {name: re.compile(p, re.I) for name, p in families.items()}
    notes: dict[str, set] = defaultdict(set)
    patients: dict[str, set] = defaultdict(set)
    shapes: dict[tuple[str, str], dict[str, tuple[set, set]]] = defaultdict(dict)
    n_docs, all_patients = 0, set()
    for n, (who, text) in enumerate(_notes_from_source(reader) if reader is not None else _notes_from_db(db)):
        n_docs += 1
        all_patients.add(who)
        for line in re.split(r"\r\n|\n|\r", text or ""):
            if not line.strip() or len(line) > 400:
                continue
            for name, pat in rx.items():
                if not pat.search(line):
                    continue
                notes[name].add(n)
                patients[name].add(who)
                kind = "table row" if TABLE_GAP.search(line.strip()) else "sentence"
                ns, ps = shapes[(name, kind)].setdefault(shape(line), (set(), set()))
                ns.add(n)
                ps.add(who)
    what = "structured data is" if everything else "laboratory results are"
    print(f"# How {what} written in the notes (masked shapes and counts only)\n", file=stdout)
    print(f"{n_docs} notes of {len(all_patients)} patients searched. 9 = a number, A / Aa / a = masked words, "
          "a… = several masked words, | = a column gap. A shape is shown only when at least "
          f"{min_notes} notes of at least {min_patients} patients share it.\n", file=stdout)
    result = {}
    for name in families:
        result[name] = {"notes": len(notes[name]), "patients": len(patients[name])}
        print(f"## {name}: {len(notes[name])} notes, {len(patients[name])} patients", file=stdout)
        for kind in ("table row", "sentence"):
            found = shapes.get((name, kind), {})
            common = sorted(((len(ns), len(ps), s) for s, (ns, ps) in found.items()
                             if len(ns) >= min_notes and len(ps) >= min_patients), reverse=True)[:top]
            result[name][kind] = {"shapes": len(found), "shapes_shown": len(common)}
            print(f"\n{kind.capitalize()}s: {len(found)} different shapes; the most common:", file=stdout)
            if not common:
                print("- none shared by enough notes and patients", file=stdout)
            for n_notes, n_pat, s in common:
                print(f"- {n_notes} notes, {n_pat} patients: `{s}`", file=stdout)
        print("", file=stdout)
    if reader is not None:
        try:
            result["lab_tables"] = lab_table_tests(reader, min_notes, min_patients, top, stdout)
            if everything:
                result["structured_tables"] = structured_tables(reader, min_notes, min_patients, stdout)
        finally:
            reader.close()
    return result
