"""`nmosd evaluate`: compare the pipeline's REDCap export with existing REDCap abstraction (gold).

The gold export (identified, real dates) is de-identified with the same linkage key (record_id → EMPI →
study ID; each date shifted by its own precision) inside the PHI location, then compared in study-ID space.
Only AGGREGATE metrics are printed: no record IDs, study IDs, values or dates.
"""

from __future__ import annotations

import csv
import itertools
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import TextIO

from nmosd.deid.date_shift import Offsets, shift_by_precision
from nmosd.deid.linkage import get_passphrase, load_linkage
from nmosd.eval.report import render_report
from nmosd.redcap.csv_format import parse_date
from nmosd.redcap.reidentify import DATE_ESTIMATE
from nmosd.safety import ensure_phi_output
from nmosd.store.db import connect

SINGLE_FIELDS = ["sex", "date_nmo_diagnosis", "date_nmo_estimate", "otherdx", "dx_other", "living_deceased"]
EPISODE_DATE = {"relapse": "date_onset", "treatment": "date_starting_maint", "antibody_lab_data": None}
EPISODE_FIELDS = {
    "relapse": ["date_onset", "date_attack_estimate", "admission", "date_admission", "date_discharge", "mri_relapse"],
    "treatment": ["maintenance_therapy", "date_starting_maint", "date_maintenance_estimate", "still_on_therapy",
                  "date_stopping_maint", "reason_for_stopping_tx"],
    "antibody_lab_data": ["type_test", "aqp4_assay_date", "aqp4_assay_status", "mog_assay_date", "mog_test_positive"],
}
CHECKBOX_PREFIXES = {"relapse": ["relapse_phenotype", "acute_rx"]}
NE_VALUES = {"", "not_established", None}


def _read(path: Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def deidentify_gold(rows: list[dict], link: dict, record_map: dict[str, str],
                    date_format: str = "MM/DD/YYYY") -> list[dict]:
    """record_id → study ID; dates (YYYY-MM-DD or slashes in `date_format`'s order) shifted by precision and
    written as YYYY-MM-DD. Rows for unknown records are dropped; UNK/NA dates are kept as they are."""
    sid_by_empi = {v["empi"]: k for k, v in link["patients"].items()}
    out = []
    for r in rows:
        empi = record_map.get(r["record_id"])
        sid = sid_by_empi.get(empi) if empi else None
        if not sid:
            continue
        off = Offsets(int(link["patients"][sid]["offset_days"]))
        new = dict(r, record_id=sid)
        for f, est in DATE_ESTIMATE.items():
            d = parse_date(r.get(f), date_format)
            if d is not None:
                new[f] = shift_by_precision(d, off, r.get(est) if est else "1").isoformat()
        out.append(new)
    return out


def _date_err(p: str, g: str, est: str | None) -> float | None:
    if p in NE_VALUES or g in NE_VALUES:
        return None
    dp, dg = parse_date(p), parse_date(g)
    if dp is None or dg is None:
        return None
    if str(est) == "3":
        return 365.25 * abs(dp.year - dg.year)
    return float(abs((dp - dg).days))


def _match_episodes(pred: list[dict], gold: list[dict], instrument: str) -> list[tuple[dict, dict]]:
    key = EPISODE_DATE[instrument]
    pairs, used = [], set()
    cands = []
    for i, g in enumerate(gold):
        for j, p in enumerate(pred):
            if instrument == "treatment" and p.get("maintenance_therapy") != g.get("maintenance_therapy"):
                continue
            if instrument == "antibody_lab_data":
                if p.get("type_test") != g.get("type_test"):
                    continue
                f = "aqp4_assay_date" if g.get("type_test") == "aqp4" else "mog_assay_date"
                err = _date_err(p.get(f), g.get(f), "1")
            else:
                err = _date_err(p.get(key), g.get(key), g.get("date_attack_estimate") if instrument == "relapse" else
                                g.get("date_maintenance_estimate"))
            limit = 400 if instrument == "relapse" else 120
            if err is not None and err <= limit:
                cands.append((err, i, j))
    for _err, i, j in sorted(cands):
        if i in used or ("p", j) in used:
            continue
        used.update({i, ("p", j)})
        pairs.append((pred[j], gold[i]))
    return pairs


def _iso_dates(rows: list[dict], date_format: str) -> list[dict]:
    """Copies of the rows with every known date field as YYYY-MM-DD (REDCap exports may use slashes)."""
    out = []
    for r in rows:
        r = dict(r)
        for f in DATE_ESTIMATE:
            d = parse_date(r.get(f), date_format)
            if d is not None:
                r[f] = d.isoformat()
        out.append(r)
    return out


def evaluate(pred_rows: list[dict], gold_rows: list[dict], review_log: list[dict] | None = None,
             proposals: list[dict] | None = None, date_format: str = "MM/DD/YYYY") -> dict:
    pred_rows, gold_rows = _iso_dates(pred_rows, date_format), _iso_dates(gold_rows, date_format)
    by = lambda rows: defaultdict(list, {})  # noqa: E731
    pred, gold = by(pred_rows), by(gold_rows)
    for r in pred_rows:
        pred[(r["record_id"], r.get("redcap_repeat_instrument") or "demographics")].append(r)
    for r in gold_rows:
        gold[(r["record_id"], r.get("redcap_repeat_instrument") or "demographics")].append(r)
    # Field/episode metrics compare the records present in BOTH files. Patients excluded at the diagnosis check
    # (or not reviewed) are absent from the export; they are counted here instead of scored as misses.
    gold_ids, pred_ids = {r["record_id"] for r in gold_rows}, {r["record_id"] for r in pred_rows}
    records = sorted(gold_ids & pred_ids)
    m: dict = {"records": len(records), "fields": {}, "episodes": {}, "dates": {}, "checkbox": {},
               "patients": {"gold": len(gold_ids), "exported": len(pred_ids), "compared": len(records),
                            "gold_only": len(gold_ids - pred_ids), "export_only": len(pred_ids - gold_ids)}}
    # single-value fields
    acc = defaultdict(lambda: [0, 0])
    for rid in records:
        g = (gold[(rid, "demographics")] or [{}])[0]
        p = (pred[(rid, "demographics")] or [{}])[0]
        for f in SINGLE_FIELDS:
            if g.get(f) in NE_VALUES:
                continue
            acc[f][1] += 1
            if f.startswith("date_") and f != "date_nmo_estimate":
                e = _date_err(p.get(f), g.get(f), g.get("date_nmo_estimate"))
                acc[f][0] += int(e is not None and e <= 31)
            else:
                acc[f][0] += int(p.get(f) == g.get(f))
    m["fields"] = {f: {"accuracy": round(c / n, 3), "n": n} for f, (c, n) in acc.items()}
    # episodes
    date_errs = defaultdict(list)
    cb = defaultdict(Counter)
    for inst in ("relapse", "treatment", "antibody_lab_data"):
        tp = fp = fn = 0
        count_err = []
        field_acc = defaultdict(lambda: [0, 0])
        for rid in records:
            P, G = pred[(rid, inst)], gold[(rid, inst)]
            pairs = _match_episodes(P, G, inst)
            tp += len(pairs)
            fp += len(P) - len(pairs)
            fn += len(G) - len(pairs)
            count_err.append(len(P) - len(G))
            for p, g in pairs:
                for f in EPISODE_FIELDS[inst]:
                    if g.get(f) in NE_VALUES:
                        continue
                    field_acc[f][1] += 1
                    if f in DATE_ESTIMATE:
                        est = g.get(DATE_ESTIMATE[f]) if DATE_ESTIMATE[f] else "1"
                        e = _date_err(p.get(f), g.get(f), est)
                        if e is not None:
                            date_errs[(f, str(est or "1"))].append(e)
                        field_acc[f][0] += int(e is not None and e <= (31 if str(est) in ("1", "2") else 366))
                    else:
                        field_acc[f][0] += int(p.get(f) == g.get(f))
                for prefix in CHECKBOX_PREFIXES.get(inst, []):
                    for col in g:
                        if col.startswith(prefix + "___"):
                            gv, pv = g.get(col) == "1", p.get(col) == "1"
                            cb[prefix]["tp"] += gv and pv
                            cb[prefix]["fp"] += pv and not gv
                            cb[prefix]["fn"] += gv and not pv
        prec = tp / (tp + fp) if tp + fp else None
        rec = tp / (tp + fn) if tp + fn else None
        m["episodes"][inst] = {"gold": tp + fn, "predicted": tp + fp, "matched": tp,
                               "precision": round(prec, 3) if prec is not None else None,
                               "recall": round(rec, 3) if rec is not None else None,
                               "exact_count_records": sum(1 for c in count_err if c == 0),
                               "mean_abs_count_error": round(statistics.mean(abs(c) for c in count_err), 3) if count_err else None,
                               "field_accuracy": {f: round(c / n, 3) for f, (c, n) in field_acc.items() if n}}
    for (f, est), errs in sorted(date_errs.items()):
        m["dates"][f"{f} (estimate {est})"] = {"n": len(errs), "median_days": round(statistics.median(errs), 1),
                                               "mean_days": round(statistics.mean(errs), 1),
                                               "exact_share": round(sum(e == 0 for e in errs) / len(errs), 3)}
    for prefix, c in cb.items():
        p = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else None
        r = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else None
        m["checkbox"][prefix] = {"precision": round(p, 3) if p is not None else None,
                                 "recall": round(r, 3) if r is not None else None}
    if proposals:
        vals = [x["value"] for x in proposals]
        m["proposals"] = {"n": len(vals), "not_established_share": round(sum(v in NE_VALUES for v in vals) / len(vals), 3),
                          "conflict_share": round(sum(int(x["conflict_flag"] or 0) for x in proposals) / len(vals), 3)}
    if review_log:
        per = defaultdict(list)
        for r in review_log:
            per[r["study_id"]].append(datetime.fromisoformat(r["at"]))
        mins = []
        for ts in per.values():
            ts.sort()
            active = sum(min((b - a).total_seconds(), 900) for a, b in itertools.pairwise(ts))
            mins.append(active / 60)
        m["review_minutes_per_patient"] = {"patients": len(mins), "median": round(statistics.median(mins), 1),
                                           "max": round(max(mins), 1)}
    return m


def review_outcomes(actions: list[dict]) -> dict:
    """How the reviewer treated the model's proposals, per instrument (counts only).

    The last decision on each field counts: accepted unchanged, corrected, or left unresolved. Episodes the reviewer
    added were missed by the model; episodes deleted (and not restored) were wrongly proposed.
    """
    last: dict[tuple, str] = {}
    added, deleted = Counter(), {}
    for a in actions:
        key = (a["study_id"], a["instrument"], a["instance_key"])
        if a["action"] in ("accept", "correct", "unresolved") and a["field"]:
            last[(*key, a["field"])] = a["action"]
        elif a["action"] == "add_instance":
            added[a["instrument"]] += 1
        elif a["action"] in ("delete_instance", "restore_instance"):
            deleted[key] = a["action"] == "delete_instance"
    out: dict[str, dict] = {}
    for (_, inst, _, _), act in last.items():
        out.setdefault(inst, Counter())[act] += 1
    for inst, n in added.items():
        out.setdefault(inst, Counter())["episodes_added"] += n
    for (_, inst, _), gone in deleted.items():
        if gone:
            out.setdefault(inst, Counter())["episodes_deleted"] += 1
    return {inst: dict(c) for inst, c in sorted(out.items())}


def run_evaluate(gold: Path, predicted: Path, review: Path | None, linkage: Path | None, record_map: Path | None,
                 settings, out: Path | None = None, extract: Path | None = None, passphrase: str | None = None,
                 label: str = "", stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    gold_rows = _read(gold)
    fmt = (settings.get("export.date_format") if settings else None) or "MM/DD/YYYY"
    if linkage:
        if not record_map:
            raise SystemExit("--record-map (record_id ↔ empi) is required to de-identify the gold file")
        link = load_linkage(linkage, passphrase or get_passphrase(settings))
        rmap = {r["record_id"]: r["empi"] for r in _read(record_map)}
        gold_rows = deidentify_gold(gold_rows, link, rmap, fmt)
    pred_rows = _read(predicted)
    review_log, actions = None, None
    if review and Path(review).exists():
        con = connect(review, readonly=True)
        review_log = [dict(r) for r in con.execute("SELECT study_id, at FROM reviews")]
        actions = [dict(r) for r in con.execute("SELECT study_id, instrument, instance_key, field, action FROM reviews "
                                                "ORDER BY review_id")]
        con.close()
    proposals = None
    extract = extract or (settings.path("extract_db") if settings else None)
    if extract and Path(extract).exists():
        con = connect(extract, readonly=True)
        run = con.execute("SELECT MAX(run_id) FROM proposals").fetchone()[0]
        proposals = [dict(r) for r in con.execute("SELECT value, conflict_flag FROM proposals WHERE run_id=? AND "
                                                  "instrument != 'eligibility'", (run,))]
        con.close()
    metrics = evaluate(pred_rows, gold_rows, review_log, proposals, date_format=fmt)
    if actions:
        metrics["review_outcomes"] = review_outcomes(actions)
    text = render_report(metrics, label)
    print(text, file=stdout)
    if out:
        p = ensure_phi_output(out, "the evaluation report")
        p.write_text(text + "\n\n```json\n" + json.dumps(metrics, indent=1) + "\n```\n", encoding="utf-8")
    return 0
