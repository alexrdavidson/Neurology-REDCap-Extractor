"""Detect and shift dates in free text, preserving their written format.

Precision-aware shifting (documented in docs/date_shifting.md):

* Full dates (day, month, year known) and month/day without a year shift by `days`
  (one offset per patient, drawn uniformly from ±[30, 365]).
* Month-precision dates ("March 2019", "9/2016", "3/98") shift by whole `months`
  = round(days / 30.4375) (never 0).
* Year-precision mentions ("in 2016", "2016-2018", bare years in lists) shift by whole `years`
  = floor((6 + months) / 12), i.e. the year a 1 July anchor lands in after the month shift.
* Seasons ("summer 2016") move their label with the month shift (mid-season anchor + `months`) and
  their year by `years`, so the season changes but re-identification by year stays exact.

Each precision is therefore exactly invertible at re-identification time, and intervals within a
precision are preserved exactly (across precisions to within half a unit).

Ambiguous short forms `a/b` with a <= 12 and b <= 31 (e.g. "3/19": 19 March or March 2019) shift the
first number by `months`. If the month wraps past December/January, the second number also moves by
the carry (±1). That is exact for the month/year reading and within ±15 days (+1 day on a wrap) of the
day shift for the month/day reading. Wrapped mentions are flagged for review.

Look-alike values are never shifted: visual acuity (20/20, 20/400), strength (5/5, 4+/5),
blood pressure (120/80), reflexes (2+, 2/4 with a reflex cue), doses (1/2 tab), ratios and titres
(1:100), spinal levels (C2-C5, T4-T9, C5/6), plain ranges (3-4), scores (EDSS 3.5, 7/10 pain).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}
_MON_ALT = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))
_WD_ALT = ("Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
           "Mon|Tues|Tue|Wed|Thurs|Thur|Thu|Fri|Sat|Sun")
_WD = rf"(?:(?P<wd>{_WD_ALT})(?P<wdp>\.)?(?P<wdc>,)?\s+)?"

SEASONS = {"spring": 4, "summer": 7, "fall": 10, "autumn": 10, "winter": 1}
SEASON_OF_MONTH = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
                   6: "summer", 7: "summer", 8: "summer", 9: "fall", 10: "fall", 11: "fall"}

HOLIDAY_RX = re.compile(
    r"\b(?:Christmas(?:\s+(?:Eve|Day))?|X-?mas|Thanksgiving|Easter(?:\s+Sunday)?|"
    r"New\s+Year'?s(?:\s+(?:Eve|Day))?|Halloween|Independence\s+Day|Fourth\s+of\s+July|"
    r"Memorial\s+Day|Labor\s+Day|Valentine'?s(?:\s+Day)?|Mother'?s\s+Day|Father'?s\s+Day|"
    r"Hanukkah|Chanukah|Passover|Rosh\s+Hashanah|Yom\s+Kippur|Ramadan|Eid|Diwali|"
    r"(?:Lunar|Chinese)\s+New\s+Year|(?:Martin\s+Luther\s+King|MLK)\s+Day|Presidents'?\s+Day|"
    r"Veterans'?\s+Day|Columbus\s+Day|Patriots'?\s+Day|Marathon\s+Monday)\b"
)
BIRTHDAY_RX = re.compile(r"(?i)\bbirthday\b")


# --------------------------------------------------------------------------------------------
# Offsets
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Offsets:
    days: int

    @property
    def months(self) -> int:
        k = round(self.days / 30.4375)
        if k == 0:
            k = 1 if self.days > 0 else -1
        return k

    @property
    def years(self) -> int:
        return (6 + self.months) // 12


def add_months(y: int, m: int, k: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + k
    return idx // 12, idx % 12 + 1


def shift_date(d: date, off: Offsets) -> date:
    return d + timedelta(days=off.days)


PRECISION_OF_ESTIMATE = {"1": "day", "2": "month", "3": "year", "4": "day"}


def shift_by_precision(d: date, off: Offsets, estimate: str | None) -> date:
    """Shift a structured date the way its text would have been shifted (REDCap estimate code → precision)."""
    precision = PRECISION_OF_ESTIMATE.get(str(estimate or "1"), "day")
    if precision == "day":
        return shift_date(d, off)
    if precision == "month":
        y, m = add_months(d.year, d.month, off.months)
        return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))
    y = d.year + off.years
    return date(y, d.month, min(d.day, calendar.monthrange(y, d.month)[1]))


def unshift_by_precision(d: date, off: Offsets, estimate: str | None) -> date:
    return unshift_date(d, off, PRECISION_OF_ESTIMATE.get(str(estimate or "1"), "day"))


def unshift_date(d: date, off: Offsets, precision: str = "day") -> date:
    """Invert a shift. precision: 'day' | 'month' | 'year'."""
    if precision == "day":
        return d - timedelta(days=off.days)
    if precision == "month":
        y, m = add_months(d.year, d.month, -off.months)
        return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))
    if precision == "year":
        y = d.year - off.years
        return date(y, d.month, min(d.day, calendar.monthrange(y, d.month)[1]))
    raise ValueError(precision)


# --------------------------------------------------------------------------------------------
# Mentions
# --------------------------------------------------------------------------------------------
@dataclass
class DateMention:
    start: int
    end: int
    kind: str  # full | md | my | year | year_range | season | short_ambiguous | holiday
    text: str
    groups: dict = field(default_factory=dict)
    replacement: str | None = None
    flag: str | None = None
    value: date | None = None  # parsed original value (anchor for partial dates)


@dataclass
class LookalikeSkip:
    start: int
    end: int
    reason: str


_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("iso", re.compile(
        r"(?<![\w/\-.])(?P<y>(?:19|20)\d{2})(?P<sep>[-/.])(?P<m>\d{1,2})(?P=sep)(?P<d>\d{1,2})(?![\d])")),
    ("mdy", re.compile(
        _WD + r"(?<![\w/\-.+])(?P<m>\d{1,2})(?P<sep>[/\-.])(?P<d>\d{1,2})(?P=sep)(?P<y>\d{4}|\d{2})"
        r"(?![\d/]|-\d|\.\d)")),
    ("mon_d_y", re.compile(
        _WD + rf"\b(?P<mon>{_MON_ALT})(?P<monp>\.)?\s+(?P<d>\d{{1,2}})(?P<ord>st|nd|rd|th)?(?P<comma>,)?\s+"
        r"(?P<y>\d{4}|'\d{2})\b", re.I)),
    ("d_mon_y", re.compile(
        _WD + rf"\b(?P<d>\d{{1,2}})(?P<ord>st|nd|rd|th)?(?P<sep>[\s-]+)(?:(?P<of>of)\s+)?(?P<mon>{_MON_ALT})"
        r"(?P<monp>\.)?(?P<comma>,)?(?P<sep2>[\s-]+)(?P<y>\d{4}|\d{2})\b", re.I)),
    ("mon_y", re.compile(
        rf"\b(?P<mon>{_MON_ALT})(?P<monp>\.)?(?P<comma>,)?\s+(?:(?P<of>of)\s+)?(?P<y>\d{{4}}|'\d{{2}})\b", re.I)),
    ("mon_d", re.compile(
        _WD + rf"\b(?P<mon>{_MON_ALT})(?P<monp>\.)?\s+(?P<d>\d{{1,2}})(?P<ord>st|nd|rd|th)?\b"
        r"(?!\s*,?\s*(?:\d|'\d)|\s*(?:mg|mcg|ml|units?|%|/|:))", re.I)),
    ("d_mon", re.compile(
        rf"\b(?P<d>\d{{1,2}})(?P<ord>st|nd|rd|th)?\s+(?:(?P<of>of)\s+)?(?P<mon>{_MON_ALT})\b(?!\.?,?\s+\d)", re.I)),
    ("m_yyyy", re.compile(
        r"(?<![\w/\-.+])(?P<m>\d{1,2})(?P<sep>[/\-])(?P<y>(?:19|20)\d{2})(?![\d/]|-\d)")),
    ("short", re.compile(
        r"(?<![\w/\-.+:])(?P<a>\d{1,2})/(?P<b>\d{1,2})(?![\d/]|\.\d|\+|%|-\d)")),
    ("season", re.compile(
        r"\b(?:(?P<mod>early|late|mid)[- ]?)?(?P<season>spring|summer|fall|autumn|winter)\s+(?:(?P<of>of)\s+)?"
        r"(?P<y>\d{4}|'\d{2})\b", re.I)),
    ("year_range", re.compile(
        r"(?<![\w/\-.])(?P<y>(?:19|20)\d{2})(?P<dash>\s*(?:-|–|to)\s*)(?P<y2>(?:19|20)\d{2})(?![\w/\-])")),
    ("year_cue", re.compile(
        r"\b(?P<cue>in|since|from|until|till|through|thru|during|by|circa|ca\.|around|before|after|"
        r"early|late|mid|mid-|end of|beginning of|start of|as of)\s+(?P<y>(?:19|20)\d{2})\b"
        r"(?!\s*(?:mg|mcg|ml|units?|cells|/|-\d|%))", re.I)),
    ("year_paren", re.compile(r"\((?P<y>(?:19|20)\d{2})\)")),
    ("year_bare", re.compile(
        r"(?<![\w/\-.:#$@])(?P<y>(?:19|20)\d{2})(?![\w/\-:%]|\.\d|"
        r"\s*(?:mg|mcg|g\b|ml|units?|cells|copies|iu\b|u/l|x\b|times|hours|hrs|min|kg|lbs?|cal|steps|m\b|meters|ft|feet))",
        re.I)),
]
_TIME_CUE = re.compile(r"(?i)(?:\bat|@|\btime|\bhours?|\bhrs?|\bmilitary)\s*$")

_STRONG_DATE_CUE = re.compile(r"(?i)\b(on|dated?|since|from|until|through|starting|started|effective|as of|seen)\s*$")
_REFLEX_CUE = re.compile(r"(?i)(reflex|dtr|jerk|biceps|triceps|patellar|achilles|brachiorad|knee|ankle|br\b|bi\b|tri\b|pat\b|ach\b)")
_SCALE_CUE = re.compile(r"(?i)(pain|score|scale|severity|rated|rates|nrs|vas)")
_MURMUR_CUE = re.compile(r"(?i)(murmur|systolic|diastolic|grade)")
METRIC_VA_DENOMINATORS = {3, 4, 5, 6, 9, 12, 18, 24, 36, 60}
_VA_CUE = re.compile(r"(?i)(\bva\b|acuity|vision|\bod\b|\bos\b|\bou\b|snellen|\bcc\b|\bsc\b|\bph\b|pinhole|\bj\d)")
_DOSE_AFTER = re.compile(
    r"(?i)^\s*(tabs?|tablets?|pills?|caps?|capsules?|doses?|of\b|teaspoons?|tsp|tbsp|cups?|inch(es)?|in\b|"
    r"mg|mcg|ml|units?|strength|ppd|packs?|cigarettes?|the\b|a\b|her\b|his\b|times|x\b)")
_DOSE_BEFORE = re.compile(r"(?i)(take|takes|taking|took|give|given|half|about|approx\.?|approximately|~|x)\s*$")
_BARE_INT_BEFORE = re.compile(r"(?:^|[^\d/.\-])\d{1,2}\s+$")


def _two_digit_year(yy: int, pivot_year: int) -> int:
    y = 2000 + yy
    return y if y <= pivot_year else 1900 + yy


def _year_from(s: str, pivot_year: int) -> tuple[int, str]:
    """Return (year, style) where style is '4', '2' or "'2"."""
    if s.startswith("'"):
        return _two_digit_year(int(s[1:]), pivot_year), "'2"
    if len(s) == 2:
        return _two_digit_year(int(s), pivot_year), "2"
    return int(s), "4"


def _render_year(y: int, style: str) -> str:
    if style == "4":
        return f"{y:04d}"
    if style == "2":
        return f"{y % 100:02d}"
    return f"'{y % 100:02d}"


def _num(n: int, original: str) -> str:
    return f"{n:02d}" if len(original) == 2 and original.startswith("0") else str(n)


def _num_w(n: int, original: str) -> str:
    """Zero-pad when the original was written with two digits (fixed-width columns)."""
    return f"{n:02d}" if len(original) == 2 else str(n)


_FULL_MONTHS = [calendar.month_name[i] for i in range(13)]
_ABBR_MONTHS = [calendar.month_abbr[i] for i in range(13)]


_FULL_LOWER = {n.lower() for n in _FULL_MONTHS if n}


def _render_month(m: int, original: str, period: str | None) -> str:
    """Render month `m` in the style of `original` (full/abbreviated, case, trailing period)."""
    low = original.lower()
    is_full = low in _FULL_LOWER and not (low == "may" and period)
    name = _FULL_MONTHS[m] if is_full else _ABBR_MONTHS[m]
    if original.isupper() and len(original) > 1:
        name = name.upper()
    elif original.islower():
        name = name.lower()
    return name if is_full else name + (period or "")


def _render_weekday(d: date, original: str | None, period: str | None, comma: str | None,
                    following_ws: str) -> str:
    if not original:
        return ""
    full = calendar.day_name[d.weekday()]
    if len(original) <= 5 and original.lower() not in {n.lower() for n in calendar.day_name}:
        name = calendar.day_abbr[d.weekday()]
    else:
        name = full
    if original.isupper():
        name = name.upper()
    elif original.islower():
        name = name.lower()
    return f"{name}{period or ''}{comma or ''}{following_ws}"


def _ordinal(d: int, original: str | None) -> str:
    if not original:
        return ""
    if 11 <= d % 100 <= 13:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
    return suf.upper() if original.isupper() else suf


def _capitalised(token: str) -> bool:
    return token[:1].isupper()


def lookalike_reason(text: str, start: int, end: int, a: int, b: int) -> str | None:
    """Why a short `a/b` is NOT a date (None means treat it as a date)."""
    before = text[max(0, start - 60):start]
    line_before = before.split("\n")[-1]
    after = text[end:end + 25]
    if a > 12 or a == 0 or b == 0:
        return "not_a_month"
    if _DOSE_AFTER.match(after) or (a < b and _DOSE_BEFORE.search(line_before[-12:])):
        return "dose_or_fraction"
    if _BARE_INT_BEFORE.search(line_before[-8:]):
        return "mixed_fraction"
    if b == 5 and a <= 5 and not _STRONG_DATE_CUE.search(line_before[-14:]):
        return "strength"
    if b == 4 and a <= 4 and _REFLEX_CUE.search(line_before):
        return "reflex"
    if b == 10 and _SCALE_CUE.search(line_before + after):
        return "scale"
    if b == 6 and _MURMUR_CUE.search(line_before + after):
        return "murmur"
    if a in (3, 6) and b in METRIC_VA_DENOMINATORS and _VA_CUE.search(line_before[-25:]) \
            and not _STRONG_DATE_CUE.search(line_before[-14:]):
        return "visual_acuity"
    return None


# A dash/en dash between two date-shaped tokens ("10/2016-3/2018", "3/4/2019-3/8/2019", "3/19-5/19") is a
# range separator. It is masked (same length) before scanning so both ends are detected; spinal levels
# such as "C5-6/7" are unaffected because the left side is not date-shaped.
RANGE_DASH_RX = re.compile(r"(?<=\d/\d{2})[-\u2013](?=\d{1,2}/)|(?<=\d/\d{4})[-\u2013](?=\d{1,2}/)|"
                           r"(?<=(?:19|20)\d{2})[-\u2013](?=\d{1,2}/\d)")


def find_dates(text: str, pivot_year: int | None = None) -> tuple[list[DateMention], list[LookalikeSkip]]:
    """Find date mentions (non-overlapping) and the look-alikes deliberately skipped."""
    pivot = pivot_year or date.today().year
    cands: list[DateMention] = []
    skips: list[LookalikeSkip] = []
    original_text, text = text, RANGE_DASH_RX.sub(" ", text)
    for kind, rx in _PATTERNS:
        for m in rx.finditer(text):
            g = {k: v for k, v in m.groupdict().items() if v is not None}
            start, end = m.start(), m.end()
            if kind in ("year_cue", "year_paren", "year_bare"):
                start, end = m.start("y"), m.end("y")
            if "mon" in g and not _capitalised(g["mon"]):
                continue
            if "wd" in g and not _capitalised(g["wd"]):
                # drop a lowercase "weekday" prefix but keep the date
                start = m.start("m") if "m" in g else m.start("mon")
                g = {k: v for k, v in g.items() if k not in ("wd", "wdp", "wdc")}
            mention = _parse(kind, g, original_text[start:end], start, end, pivot, text)
            if isinstance(mention, LookalikeSkip):
                skips.append(mention)
            elif mention is not None:
                cands.append(mention)
    for m in HOLIDAY_RX.finditer(text):
        cands.append(DateMention(m.start(), m.end(), "holiday", m.group(), flag="holiday"))
    # resolve overlaps: longest first, then earliest
    cands.sort(key=lambda c: (-(c.end - c.start), c.start))
    chosen: list[DateMention] = []
    for c in cands:
        if all(c.end <= o.start or c.start >= o.end for o in chosen):
            chosen.append(c)
    chosen.sort(key=lambda c: c.start)
    return chosen, skips


def _valid(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _parse(kind, g, txt, start, end, pivot, text) -> DateMention | LookalikeSkip | None:
    if kind in ("iso", "mdy"):
        m, d = int(g["m"]), int(g["d"])
        if kind == "mdy" and g["sep"] == "." and len(g["y"]) != 4:
            return None
        y, ystyle = _year_from(g["y"], pivot)
        v = _valid(y, m, d)
        if v is None:
            return None
        g["ystyle"] = ystyle
        return DateMention(start, end, "full", txt, g, value=v)
    if kind in ("mon_d_y", "d_mon_y"):
        mon = MONTH_NAMES[g["mon"].lower()]
        y, ystyle = _year_from(g["y"], pivot)
        v = _valid(y, mon, int(g["d"]))
        if v is None:
            return None
        g["ystyle"] = ystyle
        return DateMention(start, end, "full", txt, g | {"form": kind}, value=v)
    if kind == "mon_y":
        mon = MONTH_NAMES[g["mon"].lower()]
        if g["mon"].lower() == "may" and not g.get("y", "").isdigit() and not g.get("y", "").startswith("'"):
            return None
        y, ystyle = _year_from(g["y"], pivot)
        g["ystyle"] = ystyle
        return DateMention(start, end, "my", txt, g | {"form": kind}, value=date(y, mon, 15))
    if kind in ("mon_d", "d_mon"):
        mon = MONTH_NAMES[g["mon"].lower()]
        d = int(g["d"])
        if g["mon"].lower() == "may" and g["mon"] != "May" and g["mon"] != "MAY":
            return None
        ref_year = 2000 if (mon, d) == (2, 29) else 2001
        v = _valid(ref_year, mon, d)
        if v is None:
            return None
        return DateMention(start, end, "md", txt, g | {"form": kind}, value=v)
    if kind == "m_yyyy":
        m = int(g["m"])
        if not 1 <= m <= 12:
            return None
        return DateMention(start, end, "my", txt, g | {"form": kind, "ystyle": "4"}, value=date(int(g["y"]), m, 15))
    if kind == "short":
        a, b = int(g["a"]), int(g["b"])
        why = lookalike_reason(text, start, end, a, b)
        if why:
            return LookalikeSkip(start, end, why)
        if b > 31:
            y = _two_digit_year(b, pivot)
            return DateMention(start, end, "my", txt, g | {"form": "m_yy", "m": g["a"], "y": g["b"], "ystyle": "2"},
                               value=date(y, a, 15))
        return DateMention(start, end, "short_ambiguous", txt, g)
    if kind == "season":
        y, ystyle = _year_from(g["y"], pivot)
        g["ystyle"] = ystyle
        return DateMention(start, end, "season", txt, g, value=date(y, SEASONS[g["season"].lower()], 15))
    if kind == "year_range":
        y1, y2 = int(g["y"]), int(g["y2"])
        if not (1900 <= y1 <= 2099 and y1 <= y2 <= y1 + 60):
            return None
        return DateMention(start, end, "year_range", txt, g, value=date(y1, 7, 1))
    if kind in ("year_cue", "year_paren"):
        return DateMention(start, end, "year", txt, g, value=date(int(g["y"]), 7, 1))
    if kind == "year_bare":
        y = int(g["y"])
        if y > pivot + 1 or _TIME_CUE.search(text[max(0, start - 12):start]):
            return None
        return DateMention(start, end, "year", txt, g, value=date(y, 7, 1))
    return None


# --------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------
def render_shifted(mn: DateMention, off: Offsets, ref_year: int | None = None) -> DateMention:
    g = mn.groups
    if mn.kind == "holiday":
        mn.replacement = "[HOLIDAY]"
        return mn
    if mn.kind == "full":
        nd = shift_date(mn.value, off)
        form = g.get("form")
        wd = ""
        if g.get("wd"):
            ws = re.match(rf"{re.escape(g['wd'])}\.?,?(\s+)", mn.text)
            wd = _render_weekday(nd, g["wd"], g.get("wdp"), g.get("wdc"), ws.group(1) if ws else " ")
        if form is None and "sep" in g and "mon" not in g:
            if mn.text.lstrip().startswith(g["y"]) and len(g["y"]) == 4 and "m" in g and g.get("ystyle") == "4" \
                    and mn.text.find(g["y"]) < mn.text.find(g["sep"]):
                core = f"{nd.year:04d}{g['sep']}{_num_w(nd.month, g['m'])}{g['sep']}{_num_w(nd.day, g['d'])}"
            else:
                core = (f"{_num_w(nd.month, g['m'])}{g['sep']}{_num_w(nd.day, g['d'])}{g['sep']}"
                        f"{_render_year(nd.year, g['ystyle'])}")
            mn.replacement = wd + core
            mn.value = mn.value
            return mn
        mon = _render_month(nd.month, g["mon"], g.get("monp"))
        if form == "mon_d_y":
            sp = re.search(r"(\s+)\d", mn.text[len(wd):] if wd else mn.text)
            sp1 = sp.group(1) if sp else " "
            tail = re.search(r"(\s+)(\d{4}|'\d{2})$", mn.text)
            sp2 = tail.group(1) if tail else " "
            core = (f"{mon}{sp1}{_num_w(nd.day, g['d'])}{_ordinal(nd.day, g.get('ord'))}{g.get('comma', '')}"
                    f"{sp2}{_render_year(nd.year, g['ystyle'])}")
        else:  # d_mon_y
            of = f"{g['of']} " if g.get("of") else ""
            core = (f"{_num_w(nd.day, g['d'])}{_ordinal(nd.day, g.get('ord'))}{g['sep']}{of}{mon}"
                    f"{g.get('comma', '')}{g['sep2']}{_render_year(nd.year, g['ystyle'])}")
        mn.replacement = wd + core
        return mn
    if mn.kind == "md":
        base = mn.value
        if ref_year:
            v = _valid(ref_year, base.month, base.day)
            base = v or base
        nd = shift_date(base, off)
        mon = _render_month(nd.month, g["mon"], g.get("monp"))
        if g.get("form") == "mon_d":
            sp = re.search(r"(\s+)\d", mn.text)
            wd = ""
            if g.get("wd"):
                ws = re.match(rf"{re.escape(g['wd'])}\.?,?(\s+)", mn.text)
                wd = _render_weekday(nd, g["wd"], g.get("wdp"), g.get("wdc"), ws.group(1) if ws else " ")
                sp = re.search(r"(\s+)\d", mn.text[len(ws.group(0)) if ws else 0:])
            mn.replacement = f"{wd}{mon}{sp.group(1) if sp else ' '}{_num_w(nd.day, g['d'])}{_ordinal(nd.day, g.get('ord'))}"
        else:
            of = "of " if g.get("of") else ""
            sp = re.search(r"\d(?:st|nd|rd|th)?(\s+)", mn.text, re.I)
            mn.replacement = f"{_num_w(nd.day, g['d'])}{_ordinal(nd.day, g.get('ord'))}{sp.group(1) if sp else ' '}{of}{mon}"
        return mn
    if mn.kind == "my":
        y, m = add_months(mn.value.year, mn.value.month, off.months)
        form = g.get("form")
        if form == "mon_y":
            mon = _render_month(m, g["mon"], g.get("monp"))
            sp = re.search(r"(\s+)(?:of\s+)?(?:\d|')", mn.text)
            of = "of " if g.get("of") else ""
            mn.replacement = f"{mon}{g.get('comma', '')}{sp.group(1) if sp else ' '}{of}{_render_year(y, g['ystyle'])}"
        elif form == "m_yyyy":
            mn.replacement = f"{_num_w(m, g['m'])}{g['sep']}{y:04d}"
        else:  # m_yy
            mn.replacement = f"{_num_w(m, g['m'])}/{_render_year(y, '2')}"
        return mn
    if mn.kind == "short_ambiguous":
        a, b = int(g["a"]), int(g["b"])
        idx = (a - 1) + off.months
        new_a, carry = idx % 12 + 1, idx // 12
        new_b = b
        if carry:
            mn.flag = "ambiguous_short_date_year_wrap"
            if 0 <= b + carry <= 99 and b + carry != 0:
                new_b = b + carry
        b_txt = f"{new_b:0{len(g['b'])}d}"
        mn.replacement = f"{_num_w(new_a, g['a'])}/{b_txt}"
        return mn
    if mn.kind == "year":
        mn.replacement = f"{mn.value.year + off.years:04d}"
        return mn
    if mn.kind == "season":
        # label moves with the month shift; the year moves by the patient's whole-year offset so that
        # re-identification (estimate code 3 → un-shift by years) is exact.
        _, new_m = add_months(mn.value.year, mn.value.month, off.months)
        label = SEASON_OF_MONTH[new_m]
        old_label = g["season"]
        if old_label.isupper():
            label = label.upper()
        elif old_label[0].isupper():
            label = label.capitalize()
        yrep = _render_year(mn.value.year + off.years, g["ystyle"])
        out = mn.text.replace(old_label, label, 1)
        mn.replacement = re.sub(r"(\d{4}|'\d{2})$", yrep, out)
        return mn
    if mn.kind == "year_range":
        y1 = int(g["y"]) + off.years
        y2 = int(g["y2"]) + off.years
        mn.replacement = f"{y1:04d}{g['dash']}{y2:04d}"
        return mn
    return mn


@dataclass
class ShiftResult:
    text: str
    mentions: list[DateMention]
    skips: list[LookalikeSkip]
    anchors: int = 0  # relative-but-anchored mentions (e.g. "birthday") flagged for review

    def counts(self) -> dict[str, int]:
        return mention_counts(self.mentions, self.skips, self.anchors)


def mention_counts(mentions: list[DateMention], skips: list[LookalikeSkip], anchors: int = 0) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in mentions:
        out[f"date_{m.kind}"] = out.get(f"date_{m.kind}", 0) + 1
        if m.flag:
            out[f"flag_{m.flag}"] = out.get(f"flag_{m.flag}", 0) + 1
    for s in skips:
        out[f"lookalike_{s.reason}"] = out.get(f"lookalike_{s.reason}", 0) + 1
    if anchors:
        out["flag_relative_anchor"] = anchors
    return out


def shift_text(text: str, off: Offsets, ref_year: int | None = None, pivot_year: int | None = None) -> ShiftResult:
    """Shift every date in `text`. `ref_year` anchors month/day mentions without a year;
    `pivot_year` resolves two-digit years (default: ref_year or this year)."""
    mentions, skips = find_dates(text, pivot_year or ref_year)
    out, pos = [], 0
    for mn in mentions:
        render_shifted(mn, off, ref_year)
        out.append(text[pos:mn.start])
        out.append(mn.replacement if mn.replacement is not None else mn.text)
        pos = mn.end
    out.append(text[pos:])
    return ShiftResult("".join(out), mentions, skips, anchors=len(BIRTHDAY_RX.findall(text)))
