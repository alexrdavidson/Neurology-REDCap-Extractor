"""REDCap CSV layout: column order and file style (BOM, line endings, quoting).

The project's own REDCap export (e.g. `NMOSDDatabase_DATA_<date>_<time>.csv`) is the format our files must
match. It holds PHI, so only its HEADER LINE is ever read here: field names plus how the file is written.
No data row is read by `read_template`.

Without a template, columns follow REDCap's standard export order: record_id, the repeat columns, then each
form's fields in dictionary order followed by that form's `<form>_complete` column.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from nmosd.redcap.dictionary import MISSING_DATA_CODES, Dictionary

STRUCTURAL = ["record_id", "redcap_repeat_instrument", "redcap_repeat_instance"]
# Date formats REDCap exports can use (`export.date_format` in settings; profile-redcap-csv reports which one
# the project's export uses). Values are kept as YYYY-MM-DD inside the pipeline and converted on write.
DATE_FORMATS = ("YYYY-MM-DD", "MM/DD/YYYY", "M/D/YYYY", "DD/MM/YYYY", "D/M/YYYY")
_SLASH = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})")


def format_date(value: str | None, fmt: str = "YYYY-MM-DD") -> str | None:
    """YYYY-MM-DD → the export format. Missing-data codes (UNK, NA) and blanks pass through unchanged."""
    if fmt not in DATE_FORMATS:
        raise ValueError(f"unknown date format {fmt!r}; use one of {', '.join(DATE_FORMATS)}")
    if not value or value in MISSING_DATA_CODES:
        return value
    d = date.fromisoformat(value)
    return {"YYYY-MM-DD": d.isoformat(), "MM/DD/YYYY": f"{d.month:02d}/{d.day:02d}/{d.year:04d}",
            "M/D/YYYY": f"{d.month}/{d.day}/{d.year:04d}", "DD/MM/YYYY": f"{d.day:02d}/{d.month:02d}/{d.year:04d}",
            "D/M/YYYY": f"{d.day}/{d.month}/{d.year:04d}"}[fmt]


def parse_date(value: str | None, fmt: str = "MM/DD/YYYY") -> date | None:
    """Read a REDCap date: YYYY-MM-DD always, or a slash date in the order `fmt` gives (month first unless fmt
    starts with D). None for blanks, missing-data codes and anything unparseable."""
    v = (value or "").strip()
    if not v or v in MISSING_DATA_CODES:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        pass
    m = _SLASH.fullmatch(v)
    if not m:
        return None
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y = y if y >= 100 else 2000 + y if y < 70 else 1900 + y
    month, day = (b, a) if fmt.startswith("D") else (a, b)
    try:
        return date(y, month, day)
    except ValueError:
        return None
REDCAP_SPECIAL = {"redcap_event_name", "redcap_repeat_instrument", "redcap_repeat_instance",
                  "redcap_data_access_group", "redcap_survey_identifier"}
MAX_HEADER_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class CsvStyle:
    bom: bool = False
    newline: str = "\r\n"
    quote_all: bool = False

    def describe(self) -> str:
        return (f"{'UTF-8 with BOM' if self.bom else 'UTF-8'}, {'CRLF' if self.newline == chr(13) + chr(10) else 'LF'} "
                f"line endings, {'every value quoted' if self.quote_all else 'quotes only where needed'}")


def _header_bytes(path: Path) -> bytes:
    """The first line of the file only (bounded), never a data row."""
    with Path(path).open("rb") as f:
        head = f.readline(MAX_HEADER_BYTES)
    if not head.endswith(b"\n") and len(head) >= MAX_HEADER_BYTES:
        raise ValueError("header line too long or not a CSV")
    return head


def read_template(path: Path) -> tuple[list[str], CsvStyle]:
    """(column names in order, file style) from a REDCap CSV's header line."""
    raw = _header_bytes(path)
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if text.endswith("\r\n") else "\n"
    line = text.rstrip("\r\n")
    cols = next(csv.reader(io.StringIO(line)), [])
    quote_all = bool(cols) and line.startswith('"') and line.count('"') >= 2 * len(cols)
    return [c.strip() for c in cols], CsvStyle(bom, newline, quote_all)


def sniff_style(path: Path) -> CsvStyle:
    return read_template(path)[1]


def standard_columns(dic: Dictionary, forms: set[str]) -> list[str]:
    """REDCap export order for the given forms (no calc/descriptive/identifier fields: an import file)."""
    cols = list(STRUCTURAL)
    for form, names in dic.forms.items():
        if form not in forms:
            continue
        for name in names:
            f = dic[name]
            if name == "record_id" or not f.is_exportable or f.identifier:
                continue
            cols += list(f.checkbox_columns(MISSING_DATA_CODES).values()) if f.is_checkbox else [name]
        cols.append(f"{form}_complete")
    return cols


def write_csv(path: Path, columns: list[str], rows: list[dict], style: CsvStyle | None = None) -> None:
    style = style or CsvStyle()
    with Path(path).open("w", newline="", encoding="utf-8-sig" if style.bom else "utf-8") as f:
        w = csv.writer(f, lineterminator=style.newline,
                       quoting=csv.QUOTE_ALL if style.quote_all else csv.QUOTE_MINIMAL)
        w.writerow(columns)
        for r in rows:
            w.writerow([r.get(c, "") if r.get(c) is not None else "" for c in columns])
