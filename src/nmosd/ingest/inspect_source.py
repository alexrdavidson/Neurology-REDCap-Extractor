"""`nmosd inspect`: METADATA ONLY summary of an RPDR export.

Prints table names, column names, row counts, fill rates, distinct counts (from hashes, never
values), text-length statistics (min / median / p95 / max) and format hints. Note-title counts
(Report_Description) are printed only with --show-categories, after a warning on stderr.
"""

from __future__ import annotations

import hashlib
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import TextIO

from nmosd.ingest.access_reader import AccessReader
from nmosd.ingest.column_map import classify_table

DISTINCT_CAP = 10_000
SUSPICIOUS_LIMITS = {255, 256, 2048, 4096, 8000, 8192, 32767, 32768, 65535, 65536}


@dataclass
class ColStats:
    name: str
    filled: int = 0
    lengths: list[int] = field(default_factory=list)
    hashes: set = field(default_factory=set)
    capped: bool = False
    date_like: int = 0
    numeric: int = 0
    multiline: int = 0
    report_end: int = 0
    crlf: int = 0

    def add(self, v: str) -> None:
        if not v:
            return
        self.filled += 1
        self.lengths.append(len(v))
        if not self.capped:
            self.hashes.add(hashlib.blake2b(v.encode("utf-8", "replace"), digest_size=8).digest())
            self.capped = len(self.hashes) > DISTINCT_CAP
        if len(v) >= 8 and sum(ch.isdigit() for ch in v) >= 6 and any(s in v for s in "/-:"):
            self.date_like += 1
        try:
            float(v.replace(",", ""))
            self.numeric += 1
        except ValueError:
            pass
        if "\n" in v:
            self.multiline += 1
        if "\r\n" in v:
            self.crlf += 1
        if "[report_end]" in v.lower():
            self.report_end += 1

    def row(self, n: int) -> str:
        if not self.filled:
            return f"| {self.name} | 0% | 0 | – | – |"
        ls = sorted(self.lengths)
        p95 = ls[min(len(ls) - 1, int(0.95 * len(ls)))]
        distinct = f">{DISTINCT_CAP:,}" if self.capped else f"{len(self.hashes):,}"
        hints = []
        if self.date_like / self.filled > 0.9:
            hints.append("date-like")
        if self.numeric / self.filled > 0.9:
            hints.append("numeric")
        if self.multiline:
            hints.append(f"multiline {100 * self.multiline / self.filled:.0f}%")
        if self.crlf:
            hints.append(f"CRLF {100 * self.crlf / self.filled:.0f}%")
        if self.report_end:
            hints.append(f"[report_end] {100 * self.report_end / self.filled:.0f}%")
        lens = f"{ls[0]:,} / {int(statistics.median(ls)):,} / {p95:,} / {ls[-1]:,}"
        return (f"| {self.name} | {100 * self.filled / max(n, 1):.0f}% | {distinct} | {lens} | "
                f"{', '.join(hints) or '–'} |")


def inspect(reader: AccessReader, out: TextIO | None = None, show_categories: bool = False,
            source_label: str = "source") -> dict:
    out = out or sys.stdout
    tables = reader.tables()
    summary = {"backend": reader.backend, "tables": {}}
    print(f"# Metadata summary ({source_label}, backend: {reader.backend})\n", file=out)
    print(f"{len(tables)} tables: {', '.join(tables)}\n", file=out)
    memo_report = []
    for table in tables:
        cols = reader.columns(table)
        kind, logical = classify_table(table, cols)
        stats = [ColStats(c) for c in cols]
        cats: Counter = Counter()
        empis: set = set()
        n = 0
        for row in reader.rows(table):
            n += 1
            for s in stats:
                s.add(row.get(s.name, ""))
            if row.get("EMPI"):
                empis.add(hashlib.blake2b(row["EMPI"].encode(), digest_size=8).digest())
            if show_categories and "Report_Description" in row:
                cats[row["Report_Description"].strip()] += 1
        summary["tables"][table] = {"rows": n, "columns": cols, "kind": kind, "patients": len(empis)}
        label = f"{kind}" + (f" → {logical}" if logical != table else "")
        print(f"## {table}: {n:,} rows, {len(cols)} columns, {len(empis):,} patients ({label})\n", file=out)
        if cols:
            print("| Column | Filled | Distinct | Length min / median / p95 / max | Notes |", file=out)
            print("|---|---|---|---|---|", file=out)
            for s in stats:
                print(s.row(n), file=out)
                if s.lengths and max(s.lengths) > 255:
                    memo_report.append((table, s.name, max(s.lengths)))
        if show_categories and cats:
            print("\n**Report_Description counts (top 40)** — note titles; check before sharing\n", file=out)
            for val, c in cats.most_common(40):
                print(f"- {val}: {c:,}", file=out)
        print(file=out)
    if memo_report:
        print("## Truncation check (long-text columns)\n", file=out)
        print("Compare these maxima with the longest note in Access. A max exactly at a power-of-two or "
              "255/32767 boundary suggests truncation.\n", file=out)
        print("| Table | Column | Max length | Suspicious limit? |\n|---|---|---|---|", file=out)
        for t, c, m in memo_report:
            print(f"| {t} | {c} | {m:,} | {'YES' if m in SUSPICIOUS_LIMITS else 'no'} |", file=out)
        print(file=out)
    return summary
