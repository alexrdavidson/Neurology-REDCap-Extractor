"""Rebuild the REDCap data dictionary CSV from the project's printed codebook (metadata only, no PHI).

REDCap → Codebook → "Print page" saves a PDF whose table has four columns: the field number, the variable name (with
any branching logic below it), the label (field note in a smaller font) and the attributes (type, choices, field
annotation, calculation). This script reads the words and their positions with `pdftotext -bbox` (poppler) and writes
the dictionary in REDCap's own CSV layout, so `nmosd.redcap.dictionary.load_dictionary` reads it like a download from
REDCap. Field numbers are the row order (the codebook numbers every row, descriptive and calc fields included), and
the script stops if they are not 1..N without gaps.

Usage:  python scripts/codebook_to_dictionary.py "docs/NMOSD database _ REDCap_Codebook.pdf" \
            --out config/redcap/data_dictionary.csv
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HEADER = ["Variable / Field Name", "Form Name", "Section Header", "Field Type", "Field Label",
          "Choices, Calculations, OR Slider Labels", "Field Note", "Text Validation Type OR Show Slider Number",
          "Text Validation Min", "Text Validation Max", "Identifier?", "Branching Logic (Show field only if...)",
          "Required Field?", "Custom Alignment", "Question Number (surveys only)", "Matrix Group Name",
          "Matrix Ranking?", "Field Annotation"]
# column boundaries (points) of the printed codebook table
X_NAME, X_LABEL, X_ATTR = 60, 170, 390
CODE_X_MAX = 401          # a choice code starts at ~395; a wrapped choice label starts further right
NOTE_HEIGHT = 8.0         # field notes are set smaller (7.5 pt) than labels (8.9 pt)
WORD = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>')


@dataclass
class Word:
    page: int
    x: float
    y: float
    h: float
    text: str


@dataclass
class Row:
    number: int
    form: str
    words: list[Word] = field(default_factory=list)


def read_words(pdf: Path) -> list[Word]:
    exe = shutil.which("pdftotext")
    if not exe:
        sys.exit("pdftotext (poppler) is needed: brew install poppler / apt install poppler-utils")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "bbox.html"
        subprocess.run([exe, "-bbox", str(pdf), str(out)], check=True)
        text = out.read_text(encoding="utf-8")
    words = []
    for pno, page in enumerate(text.split("<page ")[1:]):
        for m in WORD.finditer(page):
            x, y, _x2, y2, w = m.groups()
            words.append(Word(pno, float(x), float(y), round(float(y2) - float(y), 1), html.unescape(w)))
    return words


def lines_of(words: list[Word], tol: float = 3.0) -> list[list[Word]]:
    """Group words into lines by page and y (words of one line can differ by a point or two)."""
    out: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.page, w.y, w.x)):
        if out and out[-1][0].page == w.page and abs(out[-1][0].y - w.y) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(line, key=lambda w: w.x) for line in out]


def join_wrapped(parts: list[str]) -> str:
    """Join lines of a column that wraps mid-token (variable names, branching logic)."""
    s = ""
    for p in parts:
        if s and re.search(r"[A-Za-z0-9_]$", s) and re.match(r"[A-Za-z0-9_(]", p):
            s += p
        else:
            s = f"{s} {p}".strip()
    return s


def split_rows(words: list[Word]) -> list[Row]:
    rows: list[Row] = []
    form = ""
    for line in lines_of(words):
        first = line[0]
        txt = " ".join(w.text for w in line)
        if first.y < 25 or txt.startswith("https://"):
            continue                                  # browser header / footer
        if txt.startswith("Instrument:"):
            m = re.search(r"\(([a-z0-9_]+)\)\s*$", txt)
            form = m.group(1) if m else form
            continue
        if first.x < X_NAME and first.text.isdigit():
            rows.append(Row(int(first.text), form))
            line = line[1:]
        if rows:
            rows[-1].words.extend(line)
    return rows


def parse_type(s: str) -> tuple[str, str, str, str, bool, bool]:
    """'text (integer, Min: 0, Max: 6)' / 'text (date_mdy), Identifier' → type, validation, min, max, id, req."""
    ident = "Identifier" in s
    req = "Required" in s
    s = re.sub(r",?\s*(Required|Identifier)", "", s).strip()
    m = re.match(r"(\w+)\s*(?:\((.*)\))?", s)
    ftype, inner = (m.group(1), m.group(2) or "") if m else (s, "")
    validation = vmin = vmax = ""
    if inner and ftype == "text":
        parts = [p.strip() for p in inner.split(",")]
        validation = parts[0]
        for p in parts[1:]:
            if p.startswith("Min:"):
                vmin = p[4:].strip()
            elif p.startswith("Max:"):
                vmax = p[4:].strip()
    return ftype, validation, vmin, vmax, ident, req


def parse_row(row: Row) -> dict[str, str]:
    name_parts, branch_parts, label_parts, note_parts, section = [], [], [], [], ""
    in_branch = False
    for line in lines_of([w for w in row.words if w.x < X_LABEL]):
        txt = " ".join(w.text for w in line)
        if txt.startswith("Show the field ONLY if"):
            in_branch = True
            continue
        (branch_parts if in_branch else name_parts).append(txt)
    for line in lines_of([w for w in row.words if X_LABEL <= w.x < X_ATTR]):
        txt = " ".join(w.text for w in line)
        if txt.startswith("Section Header:"):
            section = txt.removeprefix("Section Header:").strip()
        elif line[0].h < NOTE_HEIGHT:
            note_parts.append(txt)
        else:
            label_parts.append(txt)
    attr_lines = lines_of([w for w in row.words if w.x >= X_ATTR])
    if not attr_lines:
        raise ValueError(f"field {row.number}: no attributes")
    ftype, validation, vmin, vmax, ident, req = parse_type(" ".join(w.text for w in attr_lines[0]))
    choices: list[list[str]] = []
    annot: list[str] = []
    calc: list[str] = []
    mode = "choices"
    for line in attr_lines[1:]:
        txt = " ".join(w.text for w in line)
        if txt.startswith("Field Annotation:"):
            mode, txt = "annot", txt.removeprefix("Field Annotation:").strip()
        elif txt.startswith("Calculation:"):
            mode, txt = "calc", txt.removeprefix("Calculation:").strip()
        if mode == "annot":
            annot.append(txt)
        elif mode == "calc":
            calc.append(txt)
        elif line[0].x <= CODE_X_MAX:
            code, rest = line[0].text, line[1:]
            if ftype == "checkbox" and rest and rest[0].text.endswith(f"___{code.lower()}"):
                rest = rest[1:]
            choices.append([code, " ".join(w.text for w in rest)])
        elif choices:
            choices[-1][1] += " " + txt
        else:
            raise ValueError(f"field {row.number}: attribute text before any choice: {txt!r}")
    name = join_wrapped(name_parts)
    if not re.fullmatch(r"\[[a-z0-9_]+\]", name):
        raise ValueError(f"field {row.number}: odd variable name {name!r}")
    if ftype in {"yesno", "truefalse"}:
        choice_str = ""                               # implicit; REDCap leaves the column empty
    elif ftype == "calc":
        choice_str = join_wrapped(calc) if len(calc) > 1 else (calc[0] if calc else "")
    else:
        choice_str = " | ".join(f"{c}, {lab.strip()}" for c, lab in choices)
    return {
        "Variable / Field Name": name[1:-1], "Form Name": row.form, "Section Header": section,
        "Field Type": ftype, "Field Label": " ".join(label_parts),
        "Choices, Calculations, OR Slider Labels": choice_str, "Field Note": " ".join(note_parts),
        "Text Validation Type OR Show Slider Number": validation, "Text Validation Min": vmin,
        "Text Validation Max": vmax, "Identifier?": "y" if ident else "",
        "Branching Logic (Show field only if...)": join_wrapped(branch_parts), "Required Field?": "y" if req else "",
        "Custom Alignment": "", "Question Number (surveys only)": "", "Matrix Group Name": "", "Matrix Ranking?": "",
        "Field Annotation": " ".join(annot),
    }


def convert(pdf: Path) -> list[dict[str, str]]:
    rows = split_rows(read_words(pdf))
    numbers = [r.number for r in rows]
    if numbers != list(range(1, len(rows) + 1)):
        gaps = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
        raise ValueError(f"field numbers are not 1..N in order (missing: {gaps[:10]})")
    return [parse_row(r) for r in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    out = convert(a.pdf)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        w.writerows(out)
    forms = list(dict.fromkeys(r["Form Name"] for r in out))
    print(f"{len(out)} fields in {len(forms)} instruments → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
