"""`nmosd leak-test`: re-scan de-identified output. Prints counts by category only.

* Known identifiers: every variant of every patient's identifiers (and staff/organisations) is
  searched in every document, including other patients' documents. Must be 0, else exit code 1.
* Pattern and NER layers: residual candidates by category, for the human reviewer to look at.
  Ages under 90 are not candidates (they are kept on purpose), nor are hospital names when
  `deid.tokenise_hospitals` is off.
* Where each candidate and hit is (document, position, category; never the text) is saved to a JSON file in the
  PHI location, so the review report (`nmosd deid-report`) can highlight the spots to check.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TextIO

from nmosd.deid.known_identifiers import IdentifierSet, KnownMatcher
from nmosd.deid.linkage import get_passphrase, load_linkage
from nmosd.deid.ner_models import Ensemble, NERUnavailable, build_ensemble
from nmosd.deid.patterns import PatternLayer
from nmosd.deid.pipeline import is_allowlisted, report_progress
from nmosd.deid.surrogates import TOKEN_RX
from nmosd.safety import ensure_phi_output
from nmosd.store.db import connect

CANDIDATES_NAME = "leak_candidates.json"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _inside_token(text: str, s: int, e: int, tokens: list[tuple[int, int]]) -> bool:
    return any(ts <= s and e <= te for ts, te in tokens)


def _kept_on_purpose(label: str, span_text: str, tokenise_hospitals: bool) -> bool:
    if label == "AGE":
        m = re.search(r"\d+", span_text)
        return not (m and int(m.group()) >= 90)
    return label == "HOSPITAL" and not tokenise_hospitals


def scan(items: list[tuple[str, tuple[str, str] | None, str]], link: dict, patterns: PatternLayer,
         ensemble: Ensemble | None, stdout: TextIO | None = None, tokenise_hospitals: bool = True) -> dict:
    """`items`: (study_id, (doc_id, field) or None for structured rows, text)."""
    staff, orgs = link.get("staff", []), link.get("organizations", [])
    matchers = {sid: KnownMatcher(IdentifierSet.from_json(p.get("identifiers", {})), staff, orgs)
                for sid, p in link["patients"].items()}
    known, cross, residual, ner_res = Counter(), Counter(), Counter(), Counter()
    spots: list[tuple[str, str, int, int, str, str]] = []   # doc_id, field, start, end, category, source
    t_start = time.perf_counter()
    for n, (sid, ref, t) in enumerate(items, 1):
        if stdout is not None:
            report_progress(stdout, n, len(items), t_start, "texts")
        toks = [(m.start(), m.end()) for m in TOKEN_RX.finditer(t)]
        for other_sid, m in matchers.items():
            for h in m.find(t):
                if _inside_token(t, h.start, h.end, toks):
                    continue
                known[h.category] += 1
                if other_sid != sid:
                    cross[h.category] += 1
                if ref:
                    spots.append((*ref, h.start, h.end, h.category, "known identifier"))
        seen: set[tuple[int, int, str]] = set()
        for h in patterns.find(t):
            if (h.start, h.end, h.category) in seen or _inside_token(t, h.start, h.end, toks) \
                    or is_allowlisted(t[h.start:h.end]):
                continue
            seen.add((h.start, h.end, h.category))
            residual[h.category] += 1
            if ref:
                spots.append((*ref, h.start, h.end, h.category, "pattern"))
        if ensemble is not None:
            for sp in ensemble.predict(t).spans:
                frag = t[sp.start:sp.end]
                if sp.label == "DATE" or _inside_token(t, sp.start, sp.end, toks) or is_allowlisted(frag) \
                        or _kept_on_purpose(sp.label, frag, tokenise_hospitals):
                    continue
                ner_res[sp.label] += 1
                if ref:
                    spots.append((*ref, sp.start, sp.end, sp.label, "name recognition"))
    return {"known": known, "cross_patient": cross, "pattern_residual": residual, "ner_residual": ner_res,
            "spots": spots}


def save_candidates(path: Path, spots: list, hashes: dict[str, str], ner: bool) -> Path:
    """Positions only (document, field, start, end, category, source), with each document's text hash so a
    report made after a new de-identification run ignores them."""
    docs: dict[str, dict] = {}
    for doc_id, field, s, e, cat, src in spots:
        d = docs.setdefault(doc_id, {"text_hash": hashes.get(doc_id, ""), "spots": []})
        d["spots"].append([field, s, e, cat, src])
    out = ensure_phi_output(path, "the leak-test candidate positions")
    out.write_text(json.dumps({"created": datetime.now().isoformat(timespec="seconds"), "ner": ner,
                               "documents": docs}), encoding="utf-8")
    return out


def run_leak_test(deid: Path, linkage: Path, settings, use_ner: bool = True, passphrase: str | None = None,
                  ensemble: Ensemble | None = None, stdout: TextIO | None = None,
                  candidates_out: Path | None = None) -> int:
    stdout = stdout or sys.stdout
    link = load_linkage(linkage, passphrase or get_passphrase(settings))
    if use_ner and ensemble is None:
        try:
            ensemble = build_ensemble(settings)
        except NERUnavailable as e:
            print(f"NER layer unavailable ({e}); running known-identifier and pattern checks only.", file=stdout)
            ensemble = None
    if not use_ner:
        ensemble = None
    con = connect(deid, readonly=True)
    items: list[tuple[str, tuple[str, str] | None, str]] = []
    hashes: dict[str, str] = {}
    for r in con.execute("SELECT doc_id, study_id, text, report_description FROM documents"):
        items.append((r["study_id"], (r["doc_id"], "text"), r["text"]))
        items.append((r["study_id"], (r["doc_id"], "title"), r["report_description"] or ""))
        hashes[r["doc_id"]] = text_hash(r["text"])
    for r in con.execute("SELECT study_id, row_json FROM structured_rows"):
        items.append((r["study_id"], None, " | ".join(str(v) for v in json.loads(r["row_json"]).values())))
    con.close()
    tokenise_hospitals = bool(settings.get("deid.tokenise_hospitals", True))
    patterns = PatternLayer(tokenise_hospitals, tuple(settings.get("deid.mrn_digit_lengths", [7, 8, 9, 10])))
    res = scan(items, link, patterns, ensemble, stdout=stdout if ensemble is not None else None,
               tokenise_hospitals=tokenise_hospitals)
    n_known = sum(res["known"].values())
    print(f"# Leak test ({len(items)} texts, {len({sid for sid, _, _ in items})} patients)\n", file=stdout)
    print(f"Known-identifier hits: {n_known} {'(PASS)' if n_known == 0 else '(FAIL)'}", file=stdout)
    for k, v in sorted(res["known"].items()):
        print(f"  {k}: {v}" + (f" (of which in another patient's notes: {res['cross_patient'][k]})"
                               if res["cross_patient"][k] else ""), file=stdout)
    print("\nPattern-layer residual candidates (review these in the report):", file=stdout)
    for k, v in sorted(res["pattern_residual"].items()) or [("none", 0)]:
        print(f"  {k}: {v}", file=stdout)
    if ensemble is not None:
        print("\nNER residual candidates (excluding DATE, and ages under 90):", file=stdout)
        for k, v in sorted(res["ner_residual"].items()) or [("none", 0)]:
            print(f"  {k}: {v}", file=stdout)
    if candidates_out is not None:
        n_docs = len({s[0] for s in res["spots"]})
        save_candidates(candidates_out, res["spots"], hashes, ensemble is not None)
        print(f"\nSaved where the {len(res['spots'])} spots to check are ({n_docs} documents; positions only, no "
              "text). `nmosd deid-report` highlights them.", file=stdout)
    return 1 if n_known else 0
