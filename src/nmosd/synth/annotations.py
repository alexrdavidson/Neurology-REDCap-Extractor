"""Locate synthetic annotated quotes in (possibly de-identified) text: literal segments must match
verbatim (whitespace-tolerant); date slots match any short date-like text."""

from __future__ import annotations

import re

_MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATE_SHAPES = "|".join([
    rf"{_MON}[ \t]+\d{{1,2}}(?:st|nd|rd|th)?,?[ \t]+(?:\d{{4}}|'\d{{2}})",
    rf"\d{{1,2}}[ -]{_MON}[ -]\d{{2,4}}",
    rf"{_MON},?[ \t]+(?:of[ \t]+)?(?:\d{{4}}|'\d{{2}})",
    r"(?:early |late |mid-?)?(?:spring|summer|fall|autumn|winter)[ \t]+(?:of[ \t]+)?(?:\d{4}|'\d{2})",
    r"\d{4}-\d{1,2}-\d{1,2}",
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
    r"\d{1,2}/\d{4}",
    r"\d{1,2}/\d{1,2}",
    r"\d{4}",
])
SLOT = "(?P<{name}>" + _DATE_SHAPES.replace("{", "{{").replace("}", "}}") + ")"


def quote_pattern(segments: list) -> re.Pattern:
    parts, seen = [], set()
    for seg in segments:
        if isinstance(seg, dict):
            name = seg["slot"]
            parts.append(SLOT.format(name=name) if name not in seen else rf"(?P={name})")
            seen.add(name)
        else:
            parts.append(r"\s+".join(re.escape(w) for w in seg.split(" ")) if seg.strip() else re.escape(seg))
    return re.compile(r"(?<![\w+])" + "".join(parts))
