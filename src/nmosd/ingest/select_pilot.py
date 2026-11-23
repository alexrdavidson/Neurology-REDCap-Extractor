"""`nmosd select-pilot`: per-patient volume and keyword table to help choose 5 varied pilot patients.

Run it on a staging DB built with `nmosd stage --all-patients`. The per-patient table (with EMPIs)
is written to the PHI location only; stdout shows aggregate distributions only.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import TextIO

from nmosd.extract.dedupe import dedupe_patient
from nmosd.extract.ollama_client import estimate_tokens
from nmosd.safety import ensure_phi_output
from nmosd.store.db import connect

KEYWORDS = {
    "aqp4": r"aquaporin|\baqp-?4\b|\bnmo[- ]?igg\b",
    "mog": r"\bmog(ad)?\b|myelin oligodendrocyte",
    "rituximab": r"rituximab|rituxan|truxima|ruxience|riabni",
    "inebilizumab": r"inebilizumab|uplizna",
    "eculizumab": r"eculizumab|soliris",
    "ravulizumab": r"ravulizumab|ultomiris",
    "satralizumab": r"satralizumab|enspryng",
    "tocilizumab": r"tocilizumab|actemra",
    "mycophenolate": r"mycophenolate|cellcept|\bmmf\b",
    "azathioprine": r"azathioprine|imuran",
    "pregnancy": r"pregnan|postpartum|post-partum|\bivf\b",
    "cd19_cd20": r"\bcd ?19\b|\bcd ?20\b",
}
KW = {k: re.compile(v, re.I) for k, v in KEYWORDS.items()}


def _dist(values: list[int]) -> str:
    if not values:
        return "–"
    v = sorted(values)
    return f"min {v[0]:,} · median {int(statistics.median(v)):,} · max {v[-1]:,}"


def select_pilot(staging: Path, out: Path, settings=None, stdout: TextIO | None = None,
                 skip_classes: tuple[str, ...] = ("low_value",), target_tokens: int = 150_000) -> list[dict]:
    stdout = stdout or sys.stdout
    out = ensure_phi_output(out, "the pilot-selection table")
    src = connect(staging, readonly=True)
    docs = defaultdict(list)
    for r in src.execute("SELECT doc_pk, empi, source_table, report_datetime, relevance_class, text "
                         "FROM documents ORDER BY empi, report_datetime"):
        docs[r["empi"]].append(dict(r))
    src.close()
    rows = []
    for empi, ds in docs.items():
        before = sum(estimate_tokens(d["text"]) for d in ds)
        kept = [d for d in ds if d["relevance_class"] not in skip_classes]
        dd = dedupe_patient([(str(d["doc_pk"]), d["text"]) for d in kept])
        after = sum(estimate_tokens(v.text) for v in dd.views.values())
        alltext = "\n".join(d["text"] for d in ds)
        rows.append({
            "empi": empi, "n_documents": len(ds), "tokens_before": before, "tokens_after": after,
            "n_neuro_mri": sum(d["relevance_class"] == "neuro_mri" for d in ds),
            "counts_by_class": dict(Counter(d["relevance_class"] for d in ds)),
            "counts_by_table": dict(Counter(d["source_table"] for d in ds)),
            "keyword_flags": {k: bool(rx.search(alltext)) for k, rx in KW.items()},
        })
    if out.exists():
        out.unlink()
    con = connect(out, "pilot_selection")
    con.executemany("INSERT INTO pilot_candidates VALUES (?,?,?,?,?,?,?,?)",
                    [(r["empi"], r["n_documents"], r["tokens_before"], r["tokens_after"], r["n_neuro_mri"],
                      json.dumps(r["counts_by_class"]), json.dumps(r["counts_by_table"]),
                      json.dumps(r["keyword_flags"])) for r in rows])
    con.commit()
    con.close()
    print(f"# Pilot selection summary ({len(rows)} patients; per-patient table written to the PHI location)\n",
          file=stdout)
    print(f"- Documents per patient: {_dist([r['n_documents'] for r in rows])}", file=stdout)
    print(f"- Est. tokens before filtering: {_dist([r['tokens_before'] for r in rows])}", file=stdout)
    print(f"- Est. tokens after filtering (no {', '.join(skip_classes)}; boilerplate and copy-forward removed): "
          f"{_dist([r['tokens_after'] for r in rows])}", file=stdout)
    print(f"- Patients at or under ~{target_tokens:,} tokens after filtering: "
          f"{sum(r['tokens_after'] <= target_tokens for r in rows)}", file=stdout)
    print(f"- Neuro MRIs per patient: {_dist([r['n_neuro_mri'] for r in rows])}", file=stdout)
    print("\n| Keyword | Patients |\n|---|---|", file=stdout)
    for k in KEYWORDS:
        print(f"| {k} | {sum(r['keyword_flags'][k] for r in rows)} |", file=stdout)
    return rows
