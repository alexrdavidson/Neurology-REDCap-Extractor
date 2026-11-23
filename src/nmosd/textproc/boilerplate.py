"""Locate boilerplate blocks (config/boilerplate.yaml) as original-offset ranges."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from nmosd.config import load_rules


@dataclass
class Block:
    name: str
    start: re.Pattern
    end: re.Pattern | None
    max_lines: int | None


@lru_cache(maxsize=1)
def _blocks() -> tuple[Block, ...]:
    cfg = load_rules("boilerplate.yaml")
    return tuple(Block(b["name"], re.compile(b["start"], re.I), re.compile(b["end"], re.I) if b.get("end") else None,
                       b.get("max_lines")) for b in cfg["blocks"])


def _lines_with_offsets(text: str) -> list[tuple[int, int, str]]:
    out, pos = [], 0
    for m in re.finditer(r"[^\r\n]*(?:\r\n|\n|\r|$)", text):
        if m.start() == len(text) and not m.group():
            break
        line = m.group().rstrip("\r\n")
        out.append((m.start(), m.end(), line))
        pos = m.end()
        if pos >= len(text):
            break
    return out


def find_boilerplate(text: str, max_block_lines: int = 40) -> list[tuple[int, int, str]]:
    """Return (start, end, block_name) ranges of boilerplate in `text` (original offsets)."""
    lines = _lines_with_offsets(text)
    ranges: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        s, _, line = lines[i]
        hit = next((b for b in _blocks() if b.start.search(line)), None)
        if not hit:
            i += 1
            continue
        j = i
        limit = hit.max_lines or max_block_lines
        if hit.max_lines == 1 or hit.end is None:
            j = i + (hit.max_lines or 1) - 1
        else:
            j = i + 1
            while j < len(lines) and j - i < limit and not hit.end.search(lines[j][2]):
                j += 1
            j = min(j, len(lines) - 1)
        ranges.append((s, lines[j][1], hit.name))
        i = j + 1
    return ranges
