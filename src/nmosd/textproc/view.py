"""A filtered view of a document that remembers where every character came from.

Filtering steps (boilerplate removal, copy-forward de-duplication, section selection) never edit the
original text; they produce a TextView whose `text` is the concatenation of kept original slices.
`to_original(i)` maps an offset in the view back to the original document, so quotes found by the
LLM can be highlighted in the full note.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field


@dataclass
class TextView:
    original: str
    pieces: list[tuple[int, int]] = field(default_factory=list)  # kept (orig_start, orig_end), ordered
    separator: str = ""

    @classmethod
    def full(cls, text: str) -> TextView:
        return cls(text, [(0, len(text))] if text else [])

    def __post_init__(self):
        self._rebuild()

    def _rebuild(self) -> None:
        parts, starts, pos = [], [], 0
        for i, (s, e) in enumerate(self.pieces):
            if i and self.separator:
                pos += len(self.separator)
                parts.append(self.separator)
            starts.append(pos)
            parts.append(self.original[s:e])
            pos += e - s
        self._starts = starts
        self.text = "".join(parts)

    def to_original(self, i: int) -> int:
        """Map a view offset to an original offset (separator characters map to the next piece)."""
        if not self.pieces:
            return 0
        k = max(0, bisect.bisect_right(self._starts, i) - 1)
        s, e = self.pieces[k]
        return min(s + (i - self._starts[k]), e)

    def span_to_original(self, start: int, end: int) -> tuple[int, int]:
        return self.to_original(start), self.to_original(max(start, end - 1)) + 1 if end > start else self.to_original(start)

    def restrict(self, keep: list[tuple[int, int]]) -> TextView:
        """Intersect this view with original-offset ranges `keep`."""
        out = []
        for s, e in self.pieces:
            for ks, ke in keep:
                a, b = max(s, ks), min(e, ke)
                if a < b:
                    out.append((a, b))
        out.sort()
        merged: list[tuple[int, int]] = []
        for a, b in out:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        return TextView(self.original, merged, self.separator)

    def remove(self, drop: list[tuple[int, int]]) -> TextView:
        """Remove original-offset ranges `drop` from this view."""
        out = []
        drop = sorted(drop)
        for s, e in self.pieces:
            cur = s
            for ds, de in drop:
                if de <= cur or ds >= e:
                    continue
                if ds > cur:
                    out.append((cur, ds))
                cur = max(cur, de)
            if cur < e:
                out.append((cur, e))
        return TextView(self.original, out, self.separator)

    def __len__(self) -> int:
        return len(self.text)
