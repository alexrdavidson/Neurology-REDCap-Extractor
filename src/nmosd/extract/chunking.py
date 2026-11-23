"""Section-aware chunking with offsets back to the original (de-identified) document.

Headings come from config/sections.yaml (real headings from the metadata-only profile). For each field
group only the relevant sections are kept (e.g. Assessment/Plan + Medications + Tx: for treatment;
radiology IMPRESSION + FINDINGS for relapse/imaging), then split to fit the context window with overlap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from nmosd.config import load_rules
from nmosd.textproc.view import TextView

PREAMBLE_CHARS = 300


@dataclass
class Section:
    start: int
    end: int
    stype: str
    heading: str


@lru_cache(maxsize=1)
def _cfg():
    cfg = load_rules("sections.yaml")
    line_rx = re.compile(cfg["heading_line"])
    inline_rx = re.compile(cfg["inline_heading"], re.I)
    pats = [(stype, re.compile(rf"^(?:{p})$", re.I)) for stype, ps in cfg["sections"].items() for p in ps]
    return cfg, line_rx, inline_rx, pats


INLINE_TYPES = {"dx": "dx_summary", "tx": "tx_summary", "hpi": "hpi", "impression": "rad_impression",
                "findings": "rad_findings", "technique": "rad_technique", "plan": "assessment_plan",
                "assessment": "assessment_plan", "medications": "medications", "imaging": "data", "labs": "data"}


def _classify(heading: str) -> str | None:
    _, _, _, pats = _cfg()
    h = heading.strip().rstrip(":").strip()
    for stype, rx in pats:
        if rx.match(h):
            return stype
    return None


def find_sections(text: str) -> list[Section]:
    _, line_rx, inline_rx, _ = _cfg()
    starts: list[tuple[int, int, str, str, bool]] = []  # (start, line_end, stype, heading, single_line)
    for m in re.finditer(r"[^\r\n]*(?:\r\n|\n|\r|$)", text):
        line = m.group().rstrip("\r\n")
        if not line.strip():
            if m.end() >= len(text):
                break
            continue
        im = inline_rx.match(line)
        if im:
            key = im.group(1).lower()
            stype = INLINE_TYPES.get(key)
            if stype:
                single = key in ("dx", "tx")
                starts.append((m.start(), m.end(), stype, im.group(1), single))
                continue
        hm = line_rx.match(line)
        if hm and len(line.strip()) <= 64:
            stype = _classify(hm.group(1))
            if stype:
                starts.append((m.start(), m.end(), stype, hm.group(1).strip(), False))
        if m.end() >= len(text):
            break
    sections: list[Section] = []
    if not starts:
        return [Section(0, len(text), "whole", "")]
    if starts[0][0] > 0:
        sections.append(Section(0, starts[0][0], "preamble", ""))
    for i, (s, line_end, stype, heading, single) in enumerate(starts):
        nxt = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        end = line_end if single else nxt
        sections.append(Section(s, end, stype, heading))
        if single and line_end < nxt:
            # text after a single-line Dx:/Tx: summary belongs to the previous multi-line section type
            prev = next((x.stype for x in reversed(sections[:-1]) if x.stype not in ("dx_summary", "tx_summary")),
                        "preamble")
            sections.append(Section(line_end, nxt, prev, ""))
    return sections


def group_sections(group: str) -> tuple[set[str], set[str]]:
    cfg, *_ = _cfg()
    g = cfg["field_group_sections"].get(group, {})
    return set(g.get("sections", [])), set(g.get("always", []))


def keyword_windows(text: str, rx: re.Pattern, lines: int) -> list[tuple[int, int]]:
    """Original-offset ranges of `lines` non-empty lines either side of every line where `rx` matches (overlapping
    ranges merged). Notes are often double-spaced, so blank lines do not count."""
    spans = [(m.start(), m.end()) for m in re.finditer(r"[^\r\n]*\S[^\r\n]*", text)]
    hit = [i for i, (a, b) in enumerate(spans) if rx.search(text, a, b)]
    out: list[tuple[int, int]] = []
    for i in hit:
        a, b = spans[max(0, i - lines)][0], spans[min(len(spans) - 1, i + lines)][1]
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def select_view(view: TextView, group: str, keep_all_if_unsectioned: bool = True, preamble: bool = True) -> TextView:
    """Restrict a document view to what `group` should read (plus a short preamble, unless preamble=False).

    A group's spec in config/fields/pilot_fields.yaml may say (18.11.26, from the pilot's reference review):
    * `whole_note_under: N`: a note of at most N characters is read whole. Short notes (an infusion nurse's note,
      a message) have no headings the section rules know, so most of their text counted as preamble and was cut;
    * `focus: N`: read only N lines either side of each line with one of the group's keywords, anywhere in the
      note, whatever its section. For questions answered by one line or table row (an antibody result, a lab
      value, an antibiotic), not by a narrative: far less text for the model, and nothing is missed because its
      section was not on the group's list.
    Otherwise the group's sections (config/sections.yaml) are kept."""
    from nmosd.extract.prefilter import group_specs, keyword_rx

    cfg, *_ = _cfg()
    spec = group_specs().get(group, {})
    under = spec.get("whole_note_under")
    if under and len(view.original) <= int(under):
        return view
    if spec.get("focus"):
        keep = keyword_windows(view.original, keyword_rx(group), int(spec["focus"]))
        if preamble and keep:
            keep = [(0, min(len(view.original), PREAMBLE_CHARS)), *keep]
        return view.restrict(keep)
    if cfg["field_group_sections"].get(group, {}).get("whole_note"):
        return view
    secs = find_sections(view.original)
    wanted, always = group_sections(group)
    if len(secs) == 1 and secs[0].stype == "whole":
        return view if keep_all_if_unsectioned else view.restrict([])
    keep = [(s.start, s.end) for s in secs if s.stype in wanted | always]
    if preamble:
        keep += [(s.start, min(s.end, s.start + PREAMBLE_CHARS)) for s in secs if s.stype == "preamble"]
    return view.restrict(keep)


def split_view(view: TextView, max_chars: int, overlap_chars: int) -> list[TextView]:
    """Split a view into pieces of at most `max_chars` view characters, preferring paragraph/line breaks."""
    text = view.text
    if len(text) <= max_chars:
        return [view] if text.strip() else []
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            for sep in ("\r\n\r\n", "\n\n", "\r\n", "\n", ". "):
                cut = text.rfind(sep, start + max_chars // 2, end)
                if cut > 0:
                    end = cut + len(sep)
                    break
        a, b = view.to_original(start), view.to_original(max(start, end - 1)) + 1
        out.append(view.restrict([(a, b)]))
        if end >= len(text):
            break
        start = max(end - overlap_chars, start + 1)
    return out
