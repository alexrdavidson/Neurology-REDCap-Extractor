"""`nmosd profile-redcap-csv`: METADATA-ONLY layout of a REDCap data export (it holds PHI).

Prints the file style, column names in order, row/record counts, rows per repeating instrument and event,
and per column: how many cells are filled, what KIND of value they hold (integer, decimal, date Y-M-D, ...),
the longest value's length and which row types fill it. It never prints a value, an ID or a date.
Instrument and event names are printed only when they look like REDCap identifiers (lowercase, digits,
underscores); anything else is counted as "<non-standard>".
With a data dictionary it also reports how the columns compare with the dictionary and with our export.
"""

from __future__ import annotations

import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import TextIO

from nmosd.redcap.csv_format import REDCAP_SPECIAL, read_template, standard_columns
from nmosd.redcap.dictionary import MISSING_DATA_CODES, Dictionary, load_dictionary

IDENT = re.compile(r"[a-z][a-z0-9_]{0,99}")
KINDS = [
    ("integer", re.compile(r"-?\d+")),
    ("decimal", re.compile(r"-?\d*\.\d+")),
    ("date Y-M-D", re.compile(r"\d{4}-\d{2}-\d{2}")),
    ("datetime Y-M-D h:m", re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?::\d{2})?")),
    ("date M/D/Y or D/M/Y", re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")),
    ("date M-D-Y or D-M-Y", re.compile(r"\d{1,2}-\d{1,2}-\d{4}")),
    ("time", re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")),
]
BASE = "(non-repeating row)"
PILOT_FORMS = {"demographics", "relapse", "treatment", "antibody_lab_data"}


SLASH_PARTS = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")


def date_style(counts: Counter) -> str:
    """Recommended export.date_format from aggregate evidence about slash dates."""
    if counts["iso"] and not counts["slash"]:
        return "YYYY-MM-DD"
    day_first = counts["first_part_over_12"] > 0 and counts["second_part_over_12"] == 0
    padded = counts["unpadded"] == 0
    return ("DD/MM/YYYY" if padded else "D/M/YYYY") if day_first else ("MM/DD/YYYY" if padded else "M/D/YYYY")


def _note_date(value: str, counts: Counter) -> None:
    m = SLASH_PARTS.fullmatch(value)
    if not m:
        counts["iso"] += bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
        return
    a, b, y = m.groups()
    counts["slash"] += 1
    counts["first_part_over_12"] += int(a) > 12
    counts["second_part_over_12"] += int(b) > 12
    counts["zero_padded"] += a.startswith("0") or b.startswith("0")
    counts["unpadded"] += len(a) == 1 or len(b) == 1
    counts[f"year_{len(y)}_digits"] += 1


def kind_of(value: str) -> str:
    for name, rx in KINDS:
        if rx.fullmatch(value):
            return name
    return "text (multi-line)" if "\n" in value or "\r" in value else "text"


def _safe_name(v: str) -> str:
    return v if IDENT.fullmatch(v or "") else "<non-standard>"


def expected_export_order(dic: Dictionary) -> list[str]:
    """Column order of a full REDCap export for this dictionary (descriptive fields have no column)."""
    cols = []
    for form, names in dic.forms.items():
        for n in names:
            f = dic[n]
            if f.is_descriptive or f.is_form_status:
                continue
            cols += list(f.checkbox_columns(MISSING_DATA_CODES).values()) if f.is_checkbox else [n]
        cols.append(f"{form}_complete")
    return cols


def _names(names: list[str], limit: int = 60) -> str:
    if not names:
        return "none"
    more = f" … and {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more


def _open_text(path: Path):
    try:
        with Path(path).open(encoding="utf-8-sig") as f:
            f.read()
        return Path(path).open(newline="", encoding="utf-8-sig"), "UTF-8"
    except UnicodeDecodeError:
        return Path(path).open(newline="", encoding="cp1252"), "NOT UTF-8 (read as Windows-1252)"


def profile_csv(path: Path, dictionary: Path | None = None, out: TextIO | None = None) -> dict:
    out = out or sys.stdout
    csv.field_size_limit(2**31 - 1)
    header, style = read_template(path)
    fh, encoding = _open_text(path)
    filled, max_len = Counter(), Counter()
    kinds: dict[str, Counter] = defaultdict(Counter)
    where: dict[str, Counter] = defaultdict(Counter)
    rows_by, max_inst, events = Counter(), Counter(), Counter()
    records: set[str] = set()
    dates = Counter()
    n_rows = bad_rows = multiline = 0
    with fh:
        reader = csv.reader(fh)
        next(reader, None)
        ix = {c: i for i, c in enumerate(header)}
        for row in reader:
            if not any(row):
                continue
            n_rows += 1
            if len(row) != len(header):
                bad_rows += 1
            records.add(row[0] if row else "")
            inst = row[ix["redcap_repeat_instrument"]] if "redcap_repeat_instrument" in ix and \
                ix["redcap_repeat_instrument"] < len(row) else ""
            group = _safe_name(inst) if inst else BASE
            rows_by[group] += 1
            if inst and "redcap_repeat_instance" in ix and ix["redcap_repeat_instance"] < len(row):
                v = row[ix["redcap_repeat_instance"]]
                if v.isdigit():
                    max_inst[group] = max(max_inst[group], int(v))
            if "redcap_event_name" in ix and ix["redcap_event_name"] < len(row):
                events[_safe_name(row[ix["redcap_event_name"]]) if row[ix["redcap_event_name"]] else "(blank)"] += 1
            for c, v in zip(header, row, strict=False):
                if v == "":
                    continue
                filled[c] += 1
                k = kind_of(v)
                kinds[c][k] += 1
                multiline += k == "text (multi-line)"
                if k.startswith("date"):
                    _note_date(v, dates)
                if k.startswith("text"):
                    max_len[c] = max(max_len[c], len(v))
                where[c][group] += 1

    p = lambda s="": print(s, file=out)  # noqa: E731
    p("# REDCap CSV layout (metadata only: no values, IDs or dates)")
    p()
    p(f"- Encoding: {encoding}; style: {style.describe()}")
    p(f"- Columns: {len(header)}; data rows: {n_rows}; distinct records: {len(records)}")
    if bad_rows:
        p(f"- Rows whose cell count differs from the header: {bad_rows}")
    p(f"- First column (record ID field): {header[0] if header else '—'}")
    p(f"- REDCap structural columns: {_names([c for c in header if c in REDCAP_SPECIAL])}")
    p(f"- Longitudinal (redcap_event_name column): {'yes' if 'redcap_event_name' in ix else 'no'}")
    if events:
        p("- Rows per event: " + ", ".join(f"{k} {v}" for k, v in sorted(events.items())))
    p(f"- Cells containing line breaks: {multiline}")
    if dates["slash"] or dates["iso"]:
        p(f"- Dates: {dates['iso']} YYYY-MM-DD, {dates['slash']} with slashes (first part > 12: "
          f"{dates['first_part_over_12']}; second part > 12: {dates['second_part_over_12']}; with a leading zero: "
          f"{dates['zero_padded']}; with a 1-digit day or month: {dates['unpadded']}; 4-digit years: "
          f"{dates['year_4_digits']}; 2-digit years: {dates['year_2_digits']})")
        p(f"- Suggested setting: export.date_format: {date_style(dates)}")
    p()
    p("| Row type (redcap_repeat_instrument) | Rows | Highest instance |")
    p("|---|---|---|")
    for g, n in sorted(rows_by.items(), key=lambda kv: (kv[0] != BASE, kv[0])):
        p(f"| {g} | {n} | {max_inst[g] or '—'} |")
    p()
    p("## Columns in file order")
    p()
    p("| # | Column | Filled cells | Kinds of value | Longest text | Filled in row types |")
    p("|---|---|---|---|---|---|")
    for i, c in enumerate(header, 1):
        ks = ", ".join(f"{k} {n}" for k, n in kinds[c].most_common()) or "—"
        wh = ", ".join(f"{g} {n}" for g, n in sorted(where[c].items())) or "—"
        p(f"| {i} | {c} | {filled[c]} | {ks} | {max_len[c] or '—'} | {wh} |")
    result = {"columns": header, "rows": n_rows, "records": len(records), "style": style,
              "rows_by_instrument": dict(rows_by), "longitudinal": "redcap_event_name" in ix,
              "date_format": date_style(dates) if dates["slash"] or dates["iso"] else None}
    if dictionary:
        dic = load_dictionary(dictionary)
        fields = [c for c in header if c not in REDCAP_SPECIAL]
        expected = expected_export_order(dic)
        exp_set, got_set = set(expected), set(fields)
        p()
        p(f"## Compared with the data dictionary ({Path(dictionary).name}: {len(dic.fields)} fields, "
          f"{len(dic.forms)} forms)")
        p()
        p(f"- CSV columns the dictionary does not explain: {_names([c for c in fields if c not in exp_set])}")
        p(f"- Dictionary columns missing from the CSV: {_names([c for c in expected if c not in got_set])}")
        common_csv = [c for c in fields if c in exp_set]
        common_exp = [c for c in expected if c in got_set]
        diff = next((i for i, (a, b) in enumerate(zip(common_csv, common_exp, strict=False)) if a != b), None)
        p("- Order of the shared columns: " + ("same as REDCap's standard export order" if diff is None else
          f"differs from position {diff + 1} ({common_csv[diff]} in the CSV, {common_exp[diff]} expected)"))
        ours = standard_columns(dic, PILOT_FORMS & set(dic.forms))
        missing = [c for c in ours if c not in header and c != "record_id"]
        p(f"- Columns our export writes that the CSV lacks: {_names(missing)}")
        result |= {"not_in_dictionary": [c for c in fields if c not in exp_set], "export_missing": missing,
                   "order_differs_at": diff}
    return result
