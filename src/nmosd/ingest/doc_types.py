"""Relevance class per document from config/doc_types.yaml, and the hospital site letter."""

from __future__ import annotations

import re
from functools import lru_cache

from nmosd.config import load_rules


@lru_cache(maxsize=1)
def _cfg():
    cfg = load_rules("doc_types.yaml")
    rules = [(r["class"], set(r.get("source_tables") or []), re.compile(r["match"], re.I)) for r in cfg["rules"]]
    return cfg, rules


def site_of(report_type: str, report_description: str) -> str:
    cfg, _ = _cfg()
    letters = set(cfg.get("site_letters", []))
    for v in (report_type or "", report_description or ""):
        v = v.strip()
        if v and v[0].upper() in letters:
            return v[0].upper()
    return "U"


def relevance_class(source_table: str, report_description: str, report_type: str) -> str:
    cfg, rules = _cfg()
    desc = (report_description or "").strip()
    rtype = (report_type or "").strip()
    if cfg.get("strip_site_prefix"):
        letters = set(cfg.get("site_letters", []))
        if len(rtype) > 1 and rtype[0] in letters and rtype[1].isupper():
            rtype = rtype[1:]
    hay = f"{desc} | {rtype}"
    for cls, tables, rx in rules:
        if tables and source_table not in tables:
            continue
        if rx.search(hay):
            return cls
    return cfg.get("default", "clinical_note")
