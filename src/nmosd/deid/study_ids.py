"""Random study and document identifiers (cryptographically random; no relation to source IDs)."""

from __future__ import annotations

import secrets

# No 0/O, 1/I/L, 2/Z, 5/S, 8/B: easy to read aloud and to transcribe.
ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"


def _token(n: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def new_study_id(existing: set[str] | None = None) -> str:
    existing = existing or set()
    while True:
        sid = f"NMO-{_token(6)}"
        if sid not in existing:
            return sid


def new_doc_id(existing: set[str] | None = None) -> str:
    existing = existing or set()
    while True:
        did = f"DOC-{_token(10)}"
        if did not in existing:
            return did


def draw_offset_days(min_days: int, max_days: int) -> int:
    """Uniform over ±[min_days, max_days], never 0."""
    rng = secrets.SystemRandom()
    return rng.choice((-1, 1)) * rng.randint(int(min_days), int(max_days))
