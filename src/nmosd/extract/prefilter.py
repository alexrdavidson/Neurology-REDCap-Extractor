"""Select candidate documents per field group: relevance class first, then keyword hits from the specs."""

from __future__ import annotations

import re
from functools import lru_cache

from nmosd.config import load_rules


@lru_cache(maxsize=1)
def group_specs() -> dict:
    return load_rules("fields/pilot_fields.yaml")["groups"]


def second_pass_groups() -> list[str]:
    """Groups that read only documents chosen from a first reconcile run (`select:`), e.g. the EDSS groups."""
    return [g for g, spec in group_specs().items() if spec.get("select")]


def _word_start(w: str) -> str:
    """A keyword matches from the start of a word (18.11.26): "cipro" is not found in "reciprocal", nor "numb" in
    "number"'s middle. Stems still work ("diagnos" finds "diagnosis"). A keyword that starts with a letter gets the
    rule; one that already begins with its own anchor or symbol (`\\bMS\\b`, `(?<!...)`, `20/`) is left as written."""
    return rf"(?<![A-Za-z])(?:{w})" if w[:1].isalpha() else f"(?:{w})"


@lru_cache(maxsize=64)
def keyword_rx(group: str) -> re.Pattern:
    g = group_specs()[group]
    words = list(g.get("prefilter_keywords", []))
    for spec in g.get("fields", {}).values():
        words += spec.get("prefilter_keywords", [])
    return re.compile("|".join(_word_start(w) for w in dict.fromkeys(words)), re.I)


@lru_cache(maxsize=64)
def field_keyword_rx(group: str, field: str) -> re.Pattern | None:
    """One field's own prefilter keywords (None when it has none): the wording a quote for it must contain."""
    words = group_specs()[group].get("fields", {}).get(field, {}).get("prefilter_keywords", [])
    return re.compile("|".join(_word_start(w) for w in words), re.I) if words else None


def is_candidate(group: str, relevance_class: str, text: str, skip_classes: set[str],
                 description: str | None = None) -> tuple[bool, str]:
    """A group's `include_descriptions` regex selects documents by their description whatever their class
    (e.g. Outreach notes, which are "low value" by class, for the follow-up group)."""
    g = group_specs()[group]
    inc = g.get("include_descriptions")
    by_description = bool(inc and description and re.search(inc, description, re.I))
    if relevance_class in skip_classes and not by_description:
        return False, "skipped_class"
    if relevance_class not in set(g.get("relevance", [])) and not by_description:
        return False, "irrelevant_class"
    if not keyword_rx(group).search(text):
        return False, "no_keyword"
    return True, "selected"
