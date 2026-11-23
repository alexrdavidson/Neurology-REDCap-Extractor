"""Verify LLM quotes against the source text.

1. whitespace- and case-normalised exact match (fast path);
2. tolerant fuzzy match (difflib ratio over sliding windows) with a threshold.
Returns offsets in the ORIGINAL document text. Unverified evidence is kept but flagged.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from nmosd.textproc.view import TextView


@dataclass
class QuoteMatch:
    verified: bool
    start: int | None
    end: int | None
    score: float
    method: str  # exact | fuzzy | none


def _normalise_with_map(text: str) -> tuple[str, list[int]]:
    out, idx, prev_space = [], [], False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            out.append(" ")
            idx.append(i)
            prev_space = True
        else:
            low = ch.lower()
            out.append(low if len(low) == 1 else ch)   # 'İ'.lower() is 2 characters: keep the map one-to-one
            idx.append(i)
            prev_space = False
    return "".join(out), idx


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower().strip(" .;,\"'")


def verify_quote(quote: str, view: TextView, threshold: float = 0.85) -> QuoteMatch:
    q = _norm(quote or "")
    if len(q) < 4:
        return QuoteMatch(False, None, None, 0.0, "none")
    norm, idx = _normalise_with_map(view.text)
    pos = norm.find(q)
    if pos >= 0:
        s = view.to_original(idx[pos])
        e = view.to_original(idx[pos + len(q) - 1]) + 1
        return QuoteMatch(True, s, e, 1.0, "exact")
    best, best_pos, best_len = 0.0, -1, 0
    n = len(q)
    step = max(1, n // 6)
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(q)
    for w in (n, int(n * 0.9), int(n * 1.1)):
        w = max(4, w)
        for i in range(0, max(1, len(norm) - w + 1), step):
            sm.set_seq1(norm[i:i + w])
            if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                continue
            r = sm.ratio()
            if r > best:
                best, best_pos, best_len = r, i, w
    if best >= threshold:
        s = view.to_original(idx[best_pos])
        e = view.to_original(idx[min(len(idx) - 1, best_pos + best_len - 1)]) + 1
        return QuoteMatch(True, s, e, round(best, 3), "fuzzy")
    return QuoteMatch(False, None, None, round(best, 3), "none")


def _whole_words(text: str, s: int, e: int) -> tuple[int, int]:
    """Widen a span that starts or ends inside a word to the whole word ('blet' → 'tablet')."""
    while s > 0 and text[s - 1].isalnum() and text[s].isalnum():
        s -= 1
    while e < len(text) and text[e - 1].isalnum() and text[e].isalnum():
        e += 1
    return s, e


def locate_quote(text: str, quote: str, start: int | None = None, end: int | None = None) -> tuple[int, int] | None:
    """Where `quote` is in `text` now, for highlighting: the stored span when it still holds the quote, else the
    occurrence nearest to it (the note may have been re-de-identified since, or a fuzzy match drifted), else a
    fuzzy match. Always whole words. None when the quote is not in the text."""
    q = _norm(quote or "")
    if len(q) < 4:
        return None
    if start is not None and end is not None and 0 <= start < end <= len(text) and _norm(text[start:end]) == q:
        return _whole_words(text, start, end)
    norm, idx = _normalise_with_map(text)
    hits, pos = [], norm.find(q)
    while pos >= 0:
        hits.append((idx[pos], idx[pos + len(q) - 1] + 1))
        pos = norm.find(q, pos + 1)
    if hits:
        s, e = min(hits, key=lambda h: abs(h[0] - (start or 0)))
        return _whole_words(text, s, e)
    m = verify_quote(quote, TextView.full(text))
    return _whole_words(text, m.start, m.end) if m.verified else None
