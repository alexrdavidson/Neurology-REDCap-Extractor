"""Per-patient boilerplate stripping and copy-forward removal.

Notes are processed in date order. A normalised paragraph (≥ min_chars) is sent to the LLM only at
its first occurrence; every later occurrence is recorded in a first-occurrence map so evidence can
still cite every note in which the text appears.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from nmosd.textproc.boilerplate import find_boilerplate
from nmosd.textproc.view import TextView

PARA_RX = re.compile(r"(?:\r\n|\n|\r)[ \t]*(?:\r\n|\n|\r)")


def normalise(p: str) -> str:
    return re.sub(r"\s+", " ", p).strip().lower()


def paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans, pos = [], 0
    for m in PARA_RX.finditer(text):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        spans.append((pos, len(text)))
    return spans


@dataclass
class RepeatRef:
    doc_id: str
    start: int
    end: int
    first_doc_id: str
    first_start: int
    first_end: int


@dataclass
class DedupeResult:
    views: dict[str, TextView]
    repeats: list[RepeatRef] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def dedupe_patient(docs: list[tuple[str, str]], min_chars: int = 80, strip_boilerplate: bool = True) -> DedupeResult:
    """docs: [(doc_id, text)] in chronological order for ONE patient."""
    seen: dict[str, tuple[str, int, int]] = {}
    views: dict[str, TextView] = {}
    repeats: list[RepeatRef] = []
    stats = {"chars_in": 0, "chars_after_boilerplate": 0, "chars_after_dedupe": 0, "repeated_paragraphs": 0,
             "boilerplate_blocks": 0}
    for doc_id, text in docs:
        view = TextView.full(text)
        stats["chars_in"] += len(text)
        if strip_boilerplate:
            bp = find_boilerplate(text)
            stats["boilerplate_blocks"] += len(bp)
            view = view.remove([(s, e) for s, e, _ in bp])
        stats["chars_after_boilerplate"] += len(view.text)
        drop = []
        for s, e in paragraph_spans(text):
            para = normalise(text[s:e])
            if len(para) < min_chars:
                continue
            h = hashlib.sha1(para.encode()).hexdigest()
            if h in seen:
                fd, fs, fe = seen[h]
                repeats.append(RepeatRef(doc_id, s, e, fd, fs, fe))
                drop.append((s, e))
            else:
                seen[h] = (doc_id, s, e)
        view = view.remove(drop)
        stats["repeated_paragraphs"] += len(drop)
        stats["chars_after_dedupe"] += len(view.text)
        views[doc_id] = view
    return DedupeResult(views, repeats, stats)


# --- repeated lines (17.11.26) -------------------------------------------------------------------------------------
# Problem lists and histories are copied from note to note with a line added or removed, so the paragraph is not an
# exact repeat and is read again in full. For a field group whose answers do not change when the same line is read
# again (`skip_repeated_lines: true` in config/fields/pilot_fields.yaml: the conditions list, background facts), lines
# already read in an earlier note of the same patient are left out. Only from lists (each line starts like an item, not
# like wrapped prose), so no sentence is cut in half; a paragraph whose every line was read before goes whole.

LINE_RX = re.compile(r"[^\r\n]+")
_ALNUM = re.compile(r"[A-Za-z0-9]")
_BULLETS = "•*-–·>o \t"


def line_key(line: str) -> str | None:
    """The comparison key of a line, or None for a heading ("Past Medical History:") or a line of little text."""
    s = normalise(line).lstrip(_BULLETS).strip()
    if len(_ALNUM.findall(s)) < 3 or s.endswith(":"):
        return None
    return s


def line_keys(view: TextView) -> set[str]:
    return {k for m in LINE_RX.finditer(view.text) if (k := line_key(m.group()))}


def _is_list(text: str, lines: list[tuple[int, int]]) -> bool:
    """Every line after the first starts like a list item (bullet, number, capital), none like wrapped prose."""
    return all((c := text[s:e].lstrip()[:1]) and not c.islower() for s, e in lines[1:])


def repeated_lines(view: TextView, seen: set[str]) -> tuple[list[tuple[int, int]], bool, int]:
    """Lines of `view` already read in an earlier note (keys in `seen`), as original-offset spans to leave out.

    Returns (spans, whether any line is new, number of lines left out). A paragraph whose lines were all read before
    is left out whole, headings included; in a list with new lines only the repeated items go (its first line stays,
    as it is often the list's title).
    """
    text = view.text
    drop: list[tuple[int, int]] = []
    new_any, n = False, 0
    for ps, pe in paragraph_spans(text):
        lines = [(ps + m.start(), ps + m.end()) for m in LINE_RX.finditer(text[ps:pe])]
        keys = [line_key(text[s:e]) for s, e in lines]
        content = [k for k in keys if k]
        if not content:
            continue
        fresh = [k for k in content if k not in seen]
        new_any = new_any or bool(fresh)
        if not fresh:
            gone = list(range(len(lines)))
        elif len(fresh) < len(content) and _is_list(text, lines):
            gone = [j for j, k in enumerate(keys) if j and k and k in seen]
        else:
            continue
        n += sum(1 for j in gone if keys[j])
        drop += [view.span_to_original(*lines[j]) for j in gone]
    return drop, new_any, n
