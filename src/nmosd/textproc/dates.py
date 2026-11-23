"""Tolerant parsing of the structured RPDR date columns.

Formats seen in the real export (BUILD_PLAN §1b):
* `Report_Date_Time` is `mm/dd/yy hh:mm:ss` (two-digit year) in Visit, Progress and H&P notes and
  `m/d/yyyy h:mm:ss AM` in Discharge, Pathology and Radiology.
* `Date_of_Birth` is `mm/dd/yy hh:mm:ss`; the century is resolved with the `Age` column.
* mdbtools may also emit ISO `yyyy-mm-dd hh:mm:ss` when a column is a real Date/Time type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

_FORMATS = [
    "%m/%d/%y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%y %I:%M:%S %p",
    "%m/%d/%y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
]

_TWO_DIGIT_YEAR = re.compile(r"^\s*\d{1,2}/\d{1,2}/(\d{2})(?:\s|$)")


def _normalise(s: str) -> str:
    s = s.strip().replace(" ", " ")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?i)\b([ap])\.?m\.?$", lambda m: m.group(1).upper() + "M", s)
    return s


def parse_datetime(value: str | None, not_after: date | None = None) -> datetime | None:
    """Parse any supported structured date/time string. Two-digit years are pivoted so the
    result is not after `not_after` (default: today)."""
    if value is None:
        return None
    s = _normalise(str(value))
    if not s:
        return None
    for fmt in _FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if "%y" in fmt:
            limit = not_after or date.today()
            if dt.date() > limit:
                dt = dt.replace(year=dt.year - 100)
        return dt
    return None


@dataclass
class DobResult:
    dob: date | None
    resolved_by: str  # "four_digit", "age", "pivot", "unparsed"
    age_mismatch: bool = False


def _age_on(dob: date, ref: date) -> int:
    return ref.year - dob.year - ((ref.month, ref.day) < (dob.month, dob.day))


def parse_dob(value: str | None, age: str | int | None, reference: date | None = None) -> DobResult:
    """Parse Date_of_Birth. For two-digit years choose the century that matches `Age`
    (as of `reference`, default today) within ±1 year."""
    if value is None or not str(value).strip():
        return DobResult(None, "unparsed")
    s = _normalise(str(value))
    ref = reference or date.today()
    m = _TWO_DIGIT_YEAR.match(s)
    if not m:
        dt = parse_datetime(s)
        return DobResult(dt.date() if dt else None, "four_digit" if dt else "unparsed")
    dt = parse_datetime(s, not_after=date(9999, 12, 31))
    if dt is None:
        return DobResult(None, "unparsed")
    yy = int(m.group(1))
    candidates = []
    for century in (1800, 1900, 2000):
        try:
            candidates.append(dt.date().replace(year=century + yy))
        except ValueError:  # 29 Feb in a non-leap century year
            continue
    candidates = [c for c in candidates if c <= ref]
    try:
        age_n = int(float(str(age))) if age not in (None, "") else None
    except ValueError:
        age_n = None
    if age_n is not None:
        matching = [c for c in candidates if abs(_age_on(c, ref) - age_n) <= 1]
        if matching:
            return DobResult(matching[-1], "age")
        best = min(candidates, key=lambda c: abs(_age_on(c, ref) - age_n)) if candidates else None
        return DobResult(best, "age", age_mismatch=True)
    plausible = [c for c in candidates if 0 <= _age_on(c, ref) <= 110]
    return DobResult(plausible[-1] if plausible else None, "pivot")


def to_iso(dt: datetime | date | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return dt.isoformat()


SEASON_MID = {"spring": 4, "summer": 7, "fall": 10, "autumn": 10, "winter": 1}


def date_and_estimate(text: str, pivot: int, max_start: int | None = None) -> tuple[str, str] | None:
    """The first date written in `text` as (YYYY-MM-DD, REDCap estimate code), following the extraction rules:
    exact dates code 1; month/year the 15th, code 2; a year or season the middle of that period, code 3.
    `pivot` resolves two-digit years and dates written without a year. None when no date is found, or when it
    starts after `max_start`."""
    from nmosd.deid.date_shift import find_dates

    ms, _ = find_dates(text, pivot)
    if not ms or (max_start is not None and ms[0].start > max_start):
        return None
    return _mention_iso(ms[0], pivot)


def _mention_iso(m, pivot: int) -> tuple[str, str] | None:
    if m.kind == "full":
        return m.value.isoformat(), "1"
    if m.kind == "my":
        return m.value.isoformat(), "2"
    if m.kind == "short_ambiguous":
        a, b = int(m.groups["a"]), int(m.groups["b"])
        y = 2000 + b if 2000 + b <= pivot + 1 else 1900 + b
        return f"{y:04d}-{a:02d}-15", "2"
    if m.kind == "season":
        y = int(re.search(r"(\d{4}|'\d{2})$", m.text).group(1).strip("'"))
        y = y if y > 100 else (2000 + y if 2000 + y <= pivot + 1 else 1900 + y)
        return f"{y:04d}-{SEASON_MID[m.groups['season'].lower()]:02d}-15", "3"
    if m.kind in ("year", "year_range"):
        return f"{m.value.year:04d}-07-01", "3"
    if m.kind == "md":
        return f"{pivot:04d}-{m.value.month:02d}-{m.value.day:02d}", "1"
    return None


def written_precision(text: str, iso: str, pivot: int) -> str | None:
    """The estimate code that the way `text` writes the date `iso` allows: 1 when it gives that exact day, 2 when it
    gives only its month and year, 3 when it gives only the year or a season. None when `text` writes no date in
    that year (the date came from elsewhere, e.g. "last week" and the note's own date)."""
    from nmosd.deid.date_shift import find_dates

    try:
        y, mth = int(iso[:4]), int(iso[5:7])
    except (TypeError, ValueError):
        return None
    best = None
    for m in find_dates(text or "", pivot)[0]:
        try:
            got = _mention_iso(m, pivot)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if not got or int(got[0][:4]) != y:
            continue
        miso, est = got
        if est == "1" and miso != iso[:10]:
            continue
        if est == "2" and int(miso[5:7]) != mth:
            continue
        best = est if best is None else min(best, est)
    return best
