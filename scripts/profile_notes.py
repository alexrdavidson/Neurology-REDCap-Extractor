#!/usr/bin/env python3
"""
profile_notes.py: profile the INTERNAL structure of RPDR note text without
printing note content.

Reads the CSVs written by inspect_accdb.py --export-csv and prints a Markdown
report of:
  1. Date formats ("shapes": digits -> 9, letters -> A/a), structured and in-text
  2. How notes begin: shapes of the first lines (all letters/digits masked)
  3. Section headings: lines that look like headings and recur across at least
     --min-patients different patients (templated text, not one-off content)
  4. Keyword prevalence: NMOSD terms, drugs, antibodies, imaging, pregnancy
  5. Copy-forward: share of each note's paragraphs already seen in an earlier
     note for the same patient
  6. Size: estimated tokens per table and per patient
  7. Formatting: line endings, tabs, table-like lines, blank-line density

Usage:
    python3 profile_notes.py "/path/to/csv_export" > "/path/to/notes_profile.md"

Review the output before sharing. Headings are only shown if they occur for
several different patients, which filters out one-off text such as names. But
a clinician's name in a templated signature line could still pass, so read the
heading lists before pasting them anywhere.
"""

import argparse
import csv
import hashlib
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(sys.maxsize)

NOTE_TABLES_HINT = ("Note", "Radiology", "Pathology", "Discharge", "History")

KEYWORDS = {
    "NMO / NMOSD / neuromyelitis": r"\bnmo(sd)?\b|neuromyelitis|devic",
    "aquaporin / AQP4": r"aquaporin|\baqp-?4\b|\bnmo[- ]?igg\b",
    "MOG / MOGAD": r"\bmog(ad)?\b|myelin oligodendrocyte",
    "optic neuritis": r"optic neuritis|\bon\b(?= (attack|episode|relapse))",
    "myelitis / LETM": r"myelitis|\bletm\b|\btm\b",
    "area postrema": r"area postrema|intractable (hiccups|nausea|vomiting)",
    "relapse / attack / flare": r"\brelapse|\battack|\bflare",
    "rituximab": r"rituximab|rituxan|truxima|ruxience|riabni",
    "eculizumab / ravulizumab": r"eculizumab|soliris|ravulizumab|ultomiris",
    "inebilizumab": r"inebilizumab|uplizna",
    "satralizumab": r"satralizumab|enspryng",
    "tocilizumab": r"tocilizumab|actemra",
    "mycophenolate": r"mycophenolate|cellcept|\bmmf\b",
    "azathioprine": r"azathioprine|imuran",
    "steroids (IVMP / prednisone)": r"methylprednisolone|solu-?medrol|\bivmp\b|prednisone",
    "PLEX / IVIG": r"plasma ?exchange|plasmapheresis|\bplex\b|\bivig\b",
    "CD19 / CD20": r"\bcd ?19\b|\bcd ?20\b",
    "IgG / immunoglobulin level": r"\bigg\b|immunoglobulin",
    "EDSS / mRS": r"\bedss\b|\bmrs\b|rankin",
    "MRI": r"\bmri\b|\bmr (brain|spine|orbit)",
    "pregnancy / postpartum": r"pregnan|postpartum|post-partum|\bivf\b",
    "multiple sclerosis": r"multiple sclerosis|\bms\b",
}
KW = {k: re.compile(v, re.I) for k, v in KEYWORDS.items()}

DATE_RX = re.compile(
    r"\b\d{1,4}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(?:\d{1,2},?\s+)?(?:'?\d{2}|\d{4})\b"
    r"|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{2,4}\b",
    re.I,
)
HEADING_RX = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /&()'\-,.#]{1,50}):\s*$|^\s*([A-Z][A-Z0-9 /&()\-]{2,50})\s*$")


def shape(s: str) -> str:
    """Mask content: digits -> 9, uppercase -> A, lowercase -> a; collapse runs."""
    out = []
    for ch in s:
        if ch.isdigit():
            c = "9"
        elif ch.isalpha():
            c = "A" if ch.isupper() else "a"
        else:
            c = ch
        out.append(c)
    s = "".join(out)
    return re.sub(r"([Aa9])\1{3,}", lambda m: m.group(1) * 3 + "+", s)


def date_shape(s: str) -> str:
    """Digits -> 9, keep month words' shape only."""
    return re.sub(r"[A-Za-z]+", lambda m: "Mon" if len(m.group()) <= 4 else "Month", re.sub(r"\d", "9", s))


def paragraphs(text: str):
    for p in re.split(r"\n\s*\n", text):
        p = re.sub(r"\s+", " ", p).strip().lower()
        if len(p) >= 80:          # ignore tiny template lines
            yield hashlib.sha1(p.encode()).hexdigest()


def load(path: Path):
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        yield from csv.DictReader(f)


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "–"


def profile(csv_dir: Path, min_patients: int, top: int):
    files = sorted(p for p in csv_dir.glob("*.csv") if any(h in p.stem for h in NOTE_TABLES_HINT))
    dem = csv_dir / "Demographics.csv"
    print(f"# Note structure profile\n\nSource folder: `{csv_dir.name}` · tables: {', '.join(p.stem for p in files)}\n")
    print(f"Headings shown only if they occur for ≥{min_patients} different patients.\n")

    if dem.exists():
        dob = Counter(date_shape(r.get("Date_of_Birth", "")) for r in load(dem))
        print("## Demographics date formats\n")
        for s, c in dob.most_common():
            print(f"- `Date_of_Birth` `{s}`: {c}")
        print()

    tokens_per_patient = defaultdict(int)
    for path in files:
        rows = list(load(path))
        if not rows or "Report_Text" not in rows[0]:
            continue
        n = len(rows)
        rows.sort(key=lambda r: (r.get("EMPI", ""), r.get("Report_Date_Time", "")))
        print(f"## {path.stem} ({n:,} notes)\n")

        # 1. structured date formats
        dt = Counter(date_shape(r.get("Report_Date_Time", "")) for r in rows)
        print("**`Report_Date_Time` formats**\n")
        for s, c in dt.most_common(6):
            print(f"- `{s}`: {c}")

        # in-text date shapes
        tds = Counter()
        for r in rows:
            for m in DATE_RX.finditer(r["Report_Text"]):
                tds[date_shape(m.group())] += 1
        print("\n**Date formats written inside the text (top 12)**\n")
        for s, c in tds.most_common(12):
            print(f"- `{s}`: {c:,}")

        # 2. note openings
        first = Counter()
        for r in rows:
            lines = [l for l in r["Report_Text"].replace("\r\n", "\n").split("\n") if l.strip()][:4]
            first[" ⏎ ".join(shape(l.strip())[:60] for l in lines)] += 1
        print(f"\n**How notes begin: first 4 non-blank lines, fully masked (top 8 of {len(first)} patterns)**\n")
        for s, c in first.most_common(8):
            print(f"- ({c}) `{s}`")

        # 3. headings recurring across patients
        head_pat = defaultdict(set)
        head_notes = Counter()
        for r in rows:
            seen = set()
            for line in r["Report_Text"].replace("\r\n", "\n").split("\n"):
                m = HEADING_RX.match(line)
                if m:
                    h = (m.group(1) or m.group(2)).strip()
                    h = re.sub(r"\d", "9", h)
                    head_pat[h].add(r.get("EMPI", ""))
                    if h not in seen:
                        head_notes[h] += 1
                        seen.add(h)
        common = [(h, head_notes[h], len(p)) for h, p in head_pat.items() if len(p) >= min_patients]
        common.sort(key=lambda x: -x[1])
        print(f"\n**Recurring section headings (top {top}; notes containing it · patients)**: review before sharing\n")
        for h, c, p in common[:top]:
            print(f"- `{h}`: {c} notes · {p} pts")

        # 4. keywords
        print("\n**Keyword prevalence (notes · patients)**\n")
        print("| Term | Notes | Patients |\n|---|---|---|")
        for k, rx in KW.items():
            notes = [r for r in rows if rx.search(r["Report_Text"])]
            print(f"| {k} | {len(notes)} ({pct(len(notes), n)}) | {len({r.get('EMPI') for r in notes})} |")

        # 5. copy-forward
        seen_by_pt = defaultdict(set)
        ratios = []
        for r in rows:
            hs = list(paragraphs(r["Report_Text"]))
            if hs:
                prior = seen_by_pt[r.get("EMPI", "")]
                ratios.append(sum(h in prior for h in hs) / len(hs))
                prior.update(hs)
        if ratios:
            q = statistics.quantiles(ratios, n=4) if len(ratios) > 1 else [ratios[0]] * 3
            print(f"\n**Copy-forward**: share of a note's paragraphs (≥80 chars) already in an earlier note "
                  f"for the same patient: median {100 * statistics.median(ratios):.0f}%, "
                  f"IQR {100 * q[0]:.0f}–{100 * q[2]:.0f}%")

        # 6. size
        toks = []
        for r in rows:
            t = len(r["Report_Text"]) / 4
            toks.append(t)
            tokens_per_patient[r.get("EMPI", "")] += t
        print(f"\n**Size**: ~{sum(toks) / 1e6:.2f}M tokens total (chars/4), median {statistics.median(toks):,.0f} per note")

        # 7. formatting
        crlf = sum("\r\n" in r["Report_Text"] for r in rows)
        tabs = sum("\t" in r["Report_Text"] for r in rows)
        tabular = sum(bool(re.search(r"\S {3,}\S.* {3,}\S", r["Report_Text"])) for r in rows)
        print(f"\n**Formatting**: CRLF line endings {pct(crlf, n)} · tabs {pct(tabs, n)} · "
              f"column-aligned (table-like) lines {pct(tabular, n)}\n")

    if tokens_per_patient:
        v = sorted(tokens_per_patient.values())
        print("## Estimated tokens per patient (all note tables)\n")
        print(f"- median {statistics.median(v):,.0f} · min {v[0]:,.0f} · max {v[-1]:,.0f} · "
              f"5-patient pilot at the median ≈ {5 * statistics.median(v) / 1e6:.2f}M tokens")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv_dir", type=Path)
    p.add_argument("--min-patients", type=int, default=3,
                   help="only show headings seen for at least this many patients (default 3)")
    p.add_argument("--top", type=int, default=60, help="headings to list per table (default 60)")
    a = p.parse_args()
    if not a.csv_dir.is_dir():
        sys.exit(f"Folder not found: {a.csv_dir}")
    profile(a.csv_dir, a.min_patients, a.top)


if __name__ == "__main__":
    main()
