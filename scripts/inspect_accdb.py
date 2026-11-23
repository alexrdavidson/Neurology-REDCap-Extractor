#!/usr/bin/env python3
"""
inspect_accdb.py: look inside an RPDR Microsoft Access (.accdb) export on macOS.

Requires mdbtools:  brew install mdbtools
Uses only the Python standard library.

Two modes:

  1. Summary (default): prints METADATA ONLY, safe to paste into Claude:
     table names, row counts, column names, fill rates, distinct counts,
     text-length statistics and simple "looks like a date/number" checks.
     No cell values are printed.

         python3 inspect_accdb.py "/path/to/SB246_20261104_092942.accdb"

     Add --show-note-types to also print counts of Report_Description /
     Report_Type values (note titles such as "MRI SPINE"). Those are usually
     not identifying, but check them before sharing.

  2. Export: writes every table to a properly quoted CSV file (contains PHI)
     so YOU can open them. Keep the output folder next to the data, not in the
     code repository.

         python3 inspect_accdb.py "/path/to/file.accdb" --export-csv "/path/to/data/csv_export"

     Tip: open the note tables in a text editor (VS Code, BBEdit) rather than
     Excel. Excel cuts cells off at 32,767 characters and mangles line breaks.
"""

import argparse
import csv
import io
import shutil
import statistics
import subprocess
import sys
from collections import Counter
from pathlib import Path

csv.field_size_limit(sys.maxsize)

DISTINCT_CAP = 10_000          # stop counting distinct values beyond this
NOTE_TYPE_COLUMNS = {"report_description", "report_type", "report_status"}
DATE_HINTS = ("-", "/", ":")


def need_mdbtools():
    if not shutil.which("mdb-tables") or not shutil.which("mdb-export"):
        sys.exit("mdbtools not found. Install it with:  brew install mdbtools")


def list_tables(db: Path) -> list[str]:
    out = subprocess.run(["mdb-tables", "-1", str(db)],
                         capture_output=True, text=True, check=True).stdout
    return [t.strip() for t in out.splitlines() if t.strip()]


def export_table(db: Path, table: str):
    """Yield CSV rows (header first) from mdb-export, streaming."""
    proc = subprocess.Popen(
        ["mdb-export", "-D", "%Y-%m-%d %H:%M:%S", str(db), table],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    text = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace", newline="")
    yield from csv.reader(text)
    proc.wait()
    if proc.returncode != 0:
        err = proc.stderr.read().decode(errors="replace").strip()
        print(f"  ! mdb-export reported an error for {table}: {err[:200]}", file=sys.stderr)


def looks_like_date(v: str) -> bool:
    return len(v) >= 8 and any(h in v for h in DATE_HINTS) and sum(c.isdigit() for c in v) >= 6


def looks_like_number(v: str) -> bool:
    try:
        float(v.replace(",", ""))
        return True
    except ValueError:
        return False


class ColumnStats:
    def __init__(self, name: str):
        self.name = name
        self.filled = 0
        self.lengths: list[int] = []
        self.distinct: set[int] = set()     # hashes only, never values
        self.distinct_capped = False
        self.dates = 0
        self.numbers = 0
        self.multiline = 0
        self.report_end = 0
        self.categories: Counter = Counter()

    def add(self, v: str, keep_category: bool):
        if v is None or v == "":
            return
        self.filled += 1
        self.lengths.append(len(v))
        if not self.distinct_capped:
            self.distinct.add(hash(v))
            if len(self.distinct) > DISTINCT_CAP:
                self.distinct_capped = True
        if looks_like_date(v):
            self.dates += 1
        if looks_like_number(v):
            self.numbers += 1
        if "\n" in v:
            self.multiline += 1
        if "[report_end]" in v.lower():
            self.report_end += 1
        if keep_category:
            self.categories[v.strip()] += 1

    def row(self, n_rows: int) -> str:
        if self.filled == 0:
            return f"| {self.name} | 0% | 0 | – | – | – | – |"
        ls = sorted(self.lengths)
        p95 = ls[min(len(ls) - 1, int(0.95 * len(ls)))]
        distinct = f">{DISTINCT_CAP:,}" if self.distinct_capped else f"{len(self.distinct):,}"
        kind = []
        if self.dates / self.filled > 0.9:
            kind.append("date-like")
        if self.numbers / self.filled > 0.9:
            kind.append("numeric")
        if self.multiline:
            kind.append(f"multiline {100 * self.multiline / self.filled:.0f}%")
        if self.report_end:
            kind.append(f"[report_end] {100 * self.report_end / self.filled:.0f}%")
        return (f"| {self.name} | {100 * self.filled / max(n_rows, 1):.0f}% | {distinct} | "
                f"{ls[0]:,} / {int(statistics.median(ls)):,} / {p95:,} / {ls[-1]:,} | "
                f"{', '.join(kind) or '–'} |")


def summarise(db: Path, show_note_types: bool):
    tables = list_tables(db)
    print(f"# Metadata summary: {db.name}\n")
    print(f"{len(tables)} tables: {', '.join(tables)}\n")
    for table in tables:
        rows = export_table(db, table)
        try:
            header = next(rows)
        except StopIteration:
            print(f"## {table}\n\n(empty or unreadable)\n")
            continue
        stats = [ColumnStats(h) for h in header]
        keep = [show_note_types and h.strip().lower() in NOTE_TYPE_COLUMNS for h in header]
        n = 0
        bad_width = 0
        for r in rows:
            n += 1
            if len(r) != len(header):
                bad_width += 1
            for i, v in enumerate(r[: len(header)]):
                stats[i].add(v, keep[i])
        print(f"## {table}: {n:,} rows, {len(header)} columns")
        if bad_width:
            print(f"\n⚠️ {bad_width:,} rows had a different number of fields from the header.")
        print("\n| Column | Filled | Distinct | Length min / median / p95 / max | Notes |")
        print("|---|---|---|---|---|")
        for s in stats:
            print(s.row(n))
        if show_note_types:
            for s in stats:
                if s.categories:
                    print(f"\n**{s.name} counts (top 40)**: check before sharing\n")
                    for val, c in s.categories.most_common(40):
                        print(f"- {val}: {c:,}")
        print()


def export_all(db: Path, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    for table in list_tables(db):
        path = out_dir / f"{table}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, quoting=csv.QUOTE_ALL)
            n = -1
            for r in export_table(db, table):
                w.writerow(r)
                n += 1
        print(f"Wrote {path.name}: {max(n, 0):,} rows")
    print(f"\nCSV files are in: {out_dir}\nThese contain patient data. Keep them out of the code folder.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("accdb", type=Path)
    p.add_argument("--show-note-types", action="store_true",
                   help="also count Report_Description/Report_Type/Report_Status values")
    p.add_argument("--export-csv", type=Path, metavar="DIR",
                   help="export every table to CSV in DIR (contains PHI) instead of summarising")
    a = p.parse_args()
    need_mdbtools()
    if not a.accdb.exists():
        sys.exit(f"File not found: {a.accdb}")
    if a.export_csv:
        export_all(a.accdb, a.export_csv)
    else:
        summarise(a.accdb, a.show_note_types)


if __name__ == "__main__":
    main()
