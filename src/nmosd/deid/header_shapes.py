"""`nmosd header-shapes`: how do the notes in each table begin? Masked shapes and counts only, never values.

Used when `suggest-identifiers` reports few header matches. For every table it prints the share of notes whose
header block the parser recognises, and for the first lines of the notes that it does not recognise the most
common *shapes*: digits become 9, capital letters A, small letters a, and each run is collapsed (a name of any
length becomes `Aa+`). Only a short list of label words that are never identifying (MRN, DOB, Account, AM, ...)
is kept as written, so a line like `42 Years F MRN: 1234567 DOB: 3/4/1980` reads `9+ Aa+ A Aa+: 9+ ...` with
`MRN:` and `DOB:` kept. A shape is shown only when at least `min_count` notes of the table share it.
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import TextIO

from nmosd.deid.header import parse_header
from nmosd.store.db import connect

# Words that name a field and cannot identify anyone. Everything else is masked.
LABELS = {"MRN", "MR", "DOB", "Account", "Acct", "Years", "Year", "Male", "Female", "AM", "PM", "Phone", "Fax",
          "Tel", "Accession", "Number", "Report", "Status", "Type", "Exam", "Date", "Final", "Preliminary",
          "Revised", "Reason", "History", "Indication", "Page", "of", "Age", "Sex", "Encounter", "Visit"}
_WORD = re.compile(r"[A-Za-z]+|\d+")
MAX_SHAPE_CHARS = 120


def shape(line: str) -> str:
    """Mask one line: digits -> 9+, letters -> Aa+ / A+ / a+ (label words kept); punctuation and spacing kept."""
    def one(m: re.Match) -> str:
        w = m.group()
        if w.isdigit():
            return "9+"
        if w in LABELS:
            return w
        if w.isupper():
            return "A" if len(w) == 1 else "A+"
        if w.islower():
            return "a+"
        return "Aa+"
    out = re.sub(r"\s+", " ", _WORD.sub(one, line.strip()))
    return out if len(out) <= MAX_SHAPE_CHARS else out[:MAX_SHAPE_CHARS] + "…"


def first_lines(text: str, n: int) -> list[str]:
    out = []
    for ln in re.split(r"\r\n|\n|\r", text):
        if ln.strip():
            out.append(ln)
            if len(out) == n:
                break
    return out


def header_shapes(staging: Path, lines: int = 6, min_count: int = 2, top: int = 4,
                  stdout: TextIO | None = None) -> dict:
    stdout = stdout or sys.stdout
    con = connect(staging, readonly=True)
    by_table: dict[str, list[str]] = defaultdict(list)
    for r in con.execute("SELECT source_table, text FROM documents"):
        by_table[r["source_table"]].append(r["text"])
    con.close()
    result = {}
    print("# How notes begin (masked shapes and counts only: 9 = digits, A/Aa/a = letters, + = a run)\n", file=stdout)
    for table in sorted(by_table):
        texts = by_table[table]
        parsed = [parse_header(t, table) for t in texts]
        matched = [h.matched for h in parsed]
        result[table] = {"notes": len(texts), "header_recognised": sum(matched)}
        print(f"## {table}: {len(texts)} notes, header recognised in {sum(matched)}", file=stdout)
        todo = [t for t, ok in zip(texts, matched, strict=True) if not ok]
        if not todo:
            print("", file=stdout)
            continue
        missed = [h for h in parsed if not h.matched]
        closest = Counter(f"{h.layout} ({h.lines_matched} line{'s' if h.lines_matched != 1 else ''} matched)"
                          if h.layout else "no layout line matched" for h in missed)
        result[table]["banner_recognised"] = sum(h.banner is not None for h in missed)
        print(f"Not recognised: {len(missed)}; banner line recognised in {result[table]['banner_recognised']}; "
              "closest layout: " + "; ".join(f"{k} × {n}" for k, n in closest.most_common()), file=stdout)
        print(f"The first {lines} non-blank lines of the {len(todo)} notes that were not recognised "
              f"(shapes shared by at least {min(min_count, len(todo))} notes):\n", file=stdout)
        floor = min(min_count, len(todo))
        for pos in range(lines):
            c = Counter(shape(ls[pos]) if pos < len(ls) else "(no line)"
                        for ls in (first_lines(t, lines) for t in todo))
            common = [(s, n) for s, n in c.most_common(top) if n >= floor]
            if not common:
                print(f"- line {pos + 1}: no shape shared by {floor}+ notes", file=stdout)
                continue
            for s, n in common:
                print(f"- line {pos + 1} ({n}×): `{s}`", file=stdout)
        print("", file=stdout)
    return result
