"""Parse note header blocks (config/deid/patterns.yaml) and harvest identifiers from them.

For inpatient and visit notes the whole header block (banner + layout lines) is replaced by a
neutral header `[NOTE_TYPE] [SITE] [SHIFTED_DATE]`. Radiology and pathology headers carry clinical
content (exam title, reason, specimen type), so they are matched (for reporting and harvesting) but
left in place for the identifier, pattern and date layers to clean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from nmosd.config import load_rules

REPLACE_TABLES = {"Discharge_Summaries", "HistoryAndPhysical", "ProgressNote", "VisitNote"}


@dataclass
class Layout:
    name: str
    tables: set[str]
    lines: list[tuple[re.Pattern, bool]]     # (pattern, optional)
    replace: bool


def _line_spec(x) -> tuple[re.Pattern, bool]:
    """A layout line is a regex (required) or `{optional: regex}` (matched when present, skipped otherwise)."""
    if isinstance(x, dict):
        return re.compile(x["optional"]), True
    return re.compile(x), False


@dataclass
class HeaderResult:
    matched: bool
    layout: str | None
    start: int                 # start of the header region (banner or first layout line)
    end: int                   # end of the header region (after its last line break)
    banner: tuple[int, int] | None
    replace: bool
    harvested: dict[str, list[str]] = field(default_factory=dict)
    lines_matched: int = 0


@lru_cache(maxsize=1)
def _config() -> tuple[re.Pattern, int, tuple[Layout, ...]]:
    cfg = load_rules("deid/patterns.yaml")["header"]
    layouts = tuple(
        Layout(lay["name"], set(lay.get("source_tables") or []), [_line_spec(x) for x in lay["lines"]],
               bool(set(lay.get("source_tables") or []) & REPLACE_TABLES) if "replace" not in lay else lay["replace"])
        for lay in cfg["layouts"])
    return re.compile(cfg["banner"], re.I), int(cfg.get("max_lines", 8)), layouts


def _lines(text: str, limit: int) -> list[tuple[int, int, str]]:
    out = []
    for m in re.finditer(r"[^\r\n]*(\r\n|\n|\r|$)", text):
        if len(out) >= limit or (m.start() == len(text)):
            break
        out.append((m.start(), m.end(), m.group().rstrip("\r\n")))
    return out


def _harvest(groups: dict[str, str], into: dict[str, list[str]]) -> None:
    for k, v in groups.items():
        if v and v.strip():
            into.setdefault(k, []).append(v.strip())


def parse_header(text: str, source_table: str) -> HeaderResult:
    banner_rx, max_lines, layouts = _config()
    lines = _lines(text, max_lines + 4)
    i = 0
    while i < len(lines) and not lines[i][2].strip():
        i += 1
    banner = None
    start = lines[i][0] if i < len(lines) else 0
    if i < len(lines) and banner_rx.match(lines[i][2]):
        banner = (lines[i][0], lines[i][1])
        i += 1
    candidates = [lay for lay in layouts if source_table in lay.tables] or list(layouts)
    best: HeaderResult | None = None
    for lay in candidates:
        harvested: dict[str, list[str]] = {}
        j = i
        while j < len(lines) and not lines[j][2].strip():
            j += 1
        first, end, k, required_ok = j, None, 0, True
        for rx, optional in lay.lines:
            t = j
            while t < len(lines) and not lines[t][2].strip():   # blank lines inside the header block
                t += 1
            m = rx.match(lines[t][2]) if t < len(lines) else None
            if m:
                _harvest(m.groupdict(), harvested)
                j, end, k = t + 1, lines[t][1], k + 1
            elif not optional:
                required_ok = False
                break
        if k == 0:
            continue
        complete = required_ok or (not lay.replace and k >= 1)
        res = HeaderResult(complete, lay.name, banner[0] if banner else lines[first][0], end, banner,
                           lay.replace and complete, harvested, k)
        # a complete layout beats a longer partial one; among equals the first listed wins
        if best is None or (res.matched, res.lines_matched) > (best.matched, best.lines_matched):
            best = res
    if best and best.matched:
        return best
    end = banner[1] if banner else start
    return HeaderResult(False, best.layout if best else None, start, end, banner, False,
                        best.harvested if best else {}, best.lines_matched if best else 0)


def neutral_header(note_type: str, site: str, shifted_date: str | None, newline: str = "\r\n") -> str:
    return f"[{note_type or 'NOTE'}] [{site or 'U'}] [{shifted_date or 'DATE UNKNOWN'}]{newline}"
