"""`nmosd accuracy`: how accurate the extraction was, judged by the reviewer's decisions (18.11.26, for a paper).

The review already keeps every decision for good (review.sqlite is append-only): accept, correct (with the old and
new value), unresolved, add or delete an instance. This command turns the latest decision on each field into
accuracy tables. The reference standard is the reviewer's final value.

Field level: every REDCap field the review shows (branching logic applied) in each instance the extractor proposed
and the reviewer kept, for patients included at the diagnosis check:

* correct: a value was proposed and kept (accepted, or corrected to the same value);
* correct_empty: nothing was proposed and nothing was needed (accepted empty);
* wrong_value: a value was proposed and corrected to a different one;
* wrongly_filled: a value was proposed but the field should be empty (corrected to empty, UNK or NA);
* missed: nothing was proposed but the reviewer entered a value;
* unresolved / not_reviewed: counted, but left out of the rates.

UNK and NA count as no value; text is compared ignoring case and spacing, dates as dates, checkboxes as sets of codes.
accuracy = (correct + correct_empty) / reviewed; precision = correct / values proposed; recall (sensitivity) =
correct / values needed. 95% intervals are Wilson score intervals; they treat fields as independent, so for an
analysis by patient use the per-decision file.

Episodes (repeating instruments: attacks, admissions, MRIs, ...): proposed instances confirmed by a decision,
deleted (wrongly proposed), and added by the reviewer (missed). Fields of added or deleted instances count only here.
Also: the diagnosis check (per patient), date errors in days, and checkbox codes (each code right, extra or missing).

Output: a new dated folder each run, so the figures in a paper can be traced to a snapshot. The report and the
accuracy_*.csv / episodes.csv tables hold counts only and are safe to share. `decisions_with_study_ids.csv` has one
row per field decision, with study IDs but no values or dates, and stays in the PHI location. stdout: the report.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

from nmosd.redcap.dictionary import Dictionary
from nmosd.redcap.sources import _note_fields, form_label, repeating_forms, source
from nmosd.review.store import GATE, ReviewStore
from nmosd.safety import ensure_phi_output

NO_VALUE = {"", "[]", "UNK", "NA", "not_established", "None", "null"}
OUTCOMES = ["correct", "correct_empty", "wrong_value", "wrongly_filled", "missed", "unresolved", "not_reviewed"]
REVIEWED = OUTCOMES[:5]
SOURCES = {"notes": "Read from the notes (model)", "derived": "Worked out from other values",
           "table": "Demographics table", "gate": "Diagnosis check (per patient)"}
DECISION_COLS = ["study_id", "instrument", "instance", "field", "number", "source", "field_group", "status", "outcome",
                 "proposal_present", "final_present", "date_error_days", "code_tp", "code_fp", "code_fn"]
STAT_COLS = ["fields", *OUTCOMES, "reviewed", "accuracy", "accuracy_ci_low", "accuracy_ci_high", "precision",
             "recall", "f1", "date_pairs", "date_exact", "date_within_30_days", "date_within_365_days",
             "date_median_error_days", "code_tp", "code_fp", "code_fn", "code_precision", "code_recall"]
EPISODE_COLS = ["instrument", "label", "proposed", "confirmed", "deleted", "not_reviewed", "added", "precision",
                "recall"]
_DATE_RX = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


# ------------------------------------------------------------------------------------------
# comparing a proposal with the final value
# ------------------------------------------------------------------------------------------
def as_date(v) -> date | None:
    m = _DATE_RX.fullmatch(str(v).strip()[:10]) if v else None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except ValueError:
        return None


def _code_key(c: str):
    return (not c.isdigit(), int(c) if c.isdigit() else 0, c)


def norm(v, checkbox: bool = False):
    """A value in the form it is compared in: None (no value), a tuple of codes, an ISO date or folded text."""
    if v is None:
        return None
    s = str(v).strip()
    if s in NO_VALUE:
        return None
    if s.startswith("["):
        try:
            codes = json.loads(s)
        except json.JSONDecodeError:
            codes = None
        if isinstance(codes, list):
            out = tuple(sorted({str(c).strip() for c in codes} - NO_VALUE, key=_code_key))
            return out or None
    if checkbox:
        return (s,)
    d = as_date(s)
    if d is not None and len(s) <= 19:   # a date, possibly with a time
        return d.isoformat()
    return " ".join(s.split()).casefold()


def outcome(status: str, proposed, final) -> str:
    """proposed / final: normalised values (`norm`)."""
    if status == "pending":
        return "not_reviewed"
    if status == "unresolved":
        return "unresolved"
    if proposed is None:
        return "missed" if final is not None else "correct_empty"
    if final is None:
        return "wrongly_filled"
    return "correct" if proposed == final else "wrong_value"


# ------------------------------------------------------------------------------------------
# collecting the decisions
# ------------------------------------------------------------------------------------------
def _decision(store: ReviewStore, fs, instance: int) -> dict:
    dic = store.dictionary
    f = dic.get(fs.field) if fs.instrument != GATE else None
    if fs.instrument == GATE:
        kind, group = "gate", "diagnosis"
    else:
        kind = source(dic, fs.field, store.hospital_names)[0]
        group = _note_fields().get(fs.field) if kind == "notes" else None
    cb = bool(f is not None and f.is_checkbox)
    p, v = norm(fs.proposed, cb), norm(fs.value, cb)
    out = outcome(fs.status, p, v)
    row = {"study_id": fs.study_id, "instrument": fs.instrument, "instance": instance, "field": fs.field,
           "number": f.number if f is not None else "", "source": kind, "field_group": group or "",
           "status": fs.status, "outcome": out, "proposal_present": int(p is not None),
           "final_present": int(v is not None), "date_error_days": "", "code_tp": "", "code_fp": "", "code_fn": ""}
    if out in ("correct", "wrong_value") and f is not None and f.is_date:
        dp, dv = as_date(p), as_date(v)
        if dp and dv:
            row["date_error_days"] = abs((dv - dp).days)
    if cb and out in REVIEWED:
        pc, tc = set(p or ()), set(v or ())
        row.update(code_tp=len(pc & tc), code_fp=len(pc - tc), code_fn=len(tc - pc))
    return row


def collect(store: ReviewStore) -> tuple[list[dict], list[dict], Counter]:
    """(field decisions, episodes, patients by diagnosis-check status)."""
    decisions, episodes, patients = [], [], Counter()
    repeating = repeating_forms(store.dictionary)
    for sid in store.patients():
        for fs in store.fields(sid, GATE, GATE):
            decisions.append(_decision(store, fs, 1))
        gate = store.gate(sid)
        patients[gate.status] += 1
        if not gate.included:   # the instruments stay locked until the diagnosis check includes the patient
            continue
        for inst in store.instrument_fields:
            proposed = {r[0] for r in store.extract.execute(
                "SELECT DISTINCT instance_key FROM proposals WHERE run_id=? AND study_id=? AND instrument=?",
                (store.run_id, sid, inst))}
            live = set(store.instances(sid, inst))
            for n, key in enumerate(store.instances(sid, inst, include_deleted=True), 1):
                fields = store.visible(store.fields(sid, inst, key))
                origin = "proposed" if key in proposed else "added"
                deleted = key not in live
                if inst in repeating:
                    episodes.append({"instrument": inst, "origin": origin, "deleted": deleted,
                                     "reviewed": any(fs.status != "pending" for fs in fields)})
                if origin == "proposed" and not deleted:
                    decisions += [_decision(store, fs, n) for fs in fields]
    return decisions, episodes, patients


# ------------------------------------------------------------------------------------------
# statistics
# ------------------------------------------------------------------------------------------
def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float | None, float | None]:
    if not n:
        return None, None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _ratio(a: int, b: int) -> float | None:
    return a / b if b else None


def summarise(rows: list[dict]) -> dict:
    c = Counter(r["outcome"] for r in rows)
    reviewed = sum(c[o] for o in REVIEWED)
    ok = c["correct"] + c["correct_empty"]
    prec = _ratio(c["correct"], c["correct"] + c["wrong_value"] + c["wrongly_filled"])
    rec = _ratio(c["correct"], c["correct"] + c["wrong_value"] + c["missed"])
    f1 = (2 * prec * rec / (prec + rec) if prec + rec else 0.0) if prec is not None and rec is not None else None
    lo, hi = wilson(ok, reviewed)
    days = [r["date_error_days"] for r in rows if r["date_error_days"] != ""]
    tp, fp, fn = (sum(r[k] for r in rows if r[k] != "") for k in ("code_tp", "code_fp", "code_fn"))
    return {"fields": len(rows), **{o: c[o] for o in OUTCOMES}, "reviewed": reviewed, "accuracy": _ratio(ok, reviewed),
            "accuracy_ci_low": lo, "accuracy_ci_high": hi, "precision": prec, "recall": rec, "f1": f1,
            "date_pairs": len(days), "date_exact": sum(d == 0 for d in days),
            "date_within_30_days": sum(d <= 30 for d in days), "date_within_365_days": sum(d <= 365 for d in days),
            "date_median_error_days": statistics.median(days) if days else None,
            "code_tp": tp, "code_fp": fp, "code_fn": fn, "code_precision": _ratio(tp, tp + fp),
            "code_recall": _ratio(tp, tp + fn)}


def episode_stats(episodes: list[dict], instrument: str) -> dict:
    es = [e for e in episodes if e["instrument"] == instrument]
    prop = [e for e in es if e["origin"] == "proposed"]
    confirmed = sum(not e["deleted"] and e["reviewed"] for e in prop)
    deleted = sum(e["deleted"] for e in prop)
    added = sum(e["origin"] == "added" and not e["deleted"] for e in es)
    return {"instrument": instrument, "proposed": len(prop), "confirmed": confirmed, "deleted": deleted,
            "not_reviewed": sum(not e["deleted"] and not e["reviewed"] for e in prop), "added": added,
            "precision": _ratio(confirmed, confirmed + deleted), "recall": _ratio(confirmed, confirmed + added)}


def tables(store: ReviewStore, decisions: list[dict], episodes: list[dict]) -> dict:
    dic: Dictionary = store.dictionary
    fields_rows = [r for r in decisions if r["source"] != "gate"]
    overall = [{"group": "All REDCap fields", **summarise(fields_rows)}]
    overall += [{"group": SOURCES[k], **summarise([r for r in decisions if r["source"] == k])}
                for k in ("notes", "derived", "table", "gate") if any(r["source"] == k for r in decisions)]
    by_inst = [{"instrument": inst, "label": form_label(dic, inst),
                **summarise([r for r in fields_rows if r["instrument"] == inst])}
               for inst in store.instrument_fields if any(r["instrument"] == inst for r in fields_rows)]
    by_field_rows: dict[tuple, list[dict]] = defaultdict(list)
    for r in decisions:
        by_field_rows[(r["instrument"], r["field"])].append(r)
    by_field = []
    for (inst, name), rows in by_field_rows.items():
        f = dic.get(name) if inst != GATE else None
        by_field.append({"instrument": inst, "number": f.number if f is not None else "", "field": name,
                         "label": f.clean_label if f is not None else "diagnosis check",
                         "source": rows[0]["source"], "field_group": rows[0]["field_group"], **summarise(rows)})
    by_field.sort(key=lambda r: (r["instrument"] != GATE, r["number"] if r["number"] != "" else 0, r["field"]))
    eps = [{**episode_stats(episodes, inst), "label": form_label(dic, inst)} for inst in store.instrument_fields
           if any(e["instrument"] == inst for e in episodes)]
    return {"overall": overall, "by_instrument": by_inst, "by_field": by_field, "episodes": eps}


# ------------------------------------------------------------------------------------------
# report and files
# ------------------------------------------------------------------------------------------
def _pct(v) -> str:
    return "–" if v is None else f"{100 * v:.1f}%"


def _acc(s: dict) -> str:
    if s["accuracy"] is None:
        return "–"
    return f"{_pct(s['accuracy'])} ({100 * s['accuracy_ci_low']:.1f}–{100 * s['accuracy_ci_high']:.1f})"


def _stat_row(name: str, s: dict) -> str:
    return (f"| {name} | {s['reviewed']} | {s['correct']} | {s['correct_empty']} | {s['wrong_value']} | "
            f"{s['wrongly_filled']} | {s['missed']} | {_acc(s)} | {_pct(s['precision'])} | {_pct(s['recall'])} | "
            f"{s['unresolved']} | {s['not_reviewed']} |")


STAT_HEADER = ("| {first} | Reviewed | Correct | Correctly empty | Wrong value | Filled, should be empty | Missed | "
               "Accuracy (95% CI) | Precision | Recall | Unresolved | Not reviewed |\n|---|---|---|---|---|---|---|---|---|"
               "---|---|---|")


def render(t: dict, meta: dict) -> str:
    pt = meta["patients"]
    label = f" — {meta['label']}" if meta.get("label") else ""
    lines = [f"# Extraction accuracy{label} (reviewer's decisions; counts only)", "",
             f"Model: {', '.join(meta['models']) or 'unknown'} · notes: {meta['notes']} · reviewers: {meta['reviewers']}",
             f"Patients: {sum(pt.values())} · included at the diagnosis check {pt.get('included', 0)} · excluded "
             f"{pt.get('excluded', 0)} · check not done or unresolved {pt.get('pending', 0) + pt.get('unresolved', 0)}. "
             "Fields count for included patients only.", "",
             "Reference standard: the reviewer's final value. The reviewer saw each proposal before deciding, which can "
             "make agreement higher than with an independent abstraction (`nmosd evaluate` compares with REDCap "
             "entries made without the tool). Accuracy = (correct + correctly empty) / reviewed; precision = correct / "
             "values proposed; recall = correct / values needed. 95% Wilson intervals, fields treated as independent.",
             "", "## Overall", "", STAT_HEADER.format(first="Fields")]
    lines += [_stat_row(r["group"], r) for r in t["overall"]]
    lines += ["", "## By instrument", "", STAT_HEADER.format(first="Instrument")]
    lines += [_stat_row(r["label"], r) for r in t["by_instrument"]]
    if t["episodes"]:
        lines += ["", "## Episodes (repeating instruments)", "",
                  "Did the tool find the right attacks, admissions, MRIs, courses, ...? Confirmed = proposed and kept "
                  "by the reviewer; deleted = wrongly proposed; added = missed by the tool. Fields of deleted or added "
                  "episodes are counted here only.", "",
                  "| Instrument | Proposed | Confirmed | Deleted | Not reviewed | Added (missed) | Precision | Recall |",
                  "|---|---|---|---|---|---|---|---|"]
        lines += [f"| {r['label']} | {r['proposed']} | {r['confirmed']} | {r['deleted']} | {r['not_reviewed']} | "
                  f"{r['added']} | {_pct(r['precision'])} | {_pct(r['recall'])} |" for r in t["episodes"]]
    dated = [r for r in t["by_instrument"] if r["date_pairs"]]
    if dated:
        lines += ["", "## Dates (proposed and final date both present)", "",
                  "| Instrument | Pairs | Exact | Within 30 days | Within 1 year | Median error (days) |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {r['label']} | {r['date_pairs']} | {r['date_exact']} | {r['date_within_30_days']} | "
                  f"{r['date_within_365_days']} | {r['date_median_error_days']} |" for r in dated]
    coded = [r for r in t["by_field"] if r["code_tp"] or r["code_fp"] or r["code_fn"]]
    if coded:
        lines += ["", "## Checkbox fields (each code)", "",
                  "| Field | Codes right | Extra codes | Missing codes | Precision | Recall |", "|---|---|---|---|---|---|"]
        lines += [f"| {r['number']}. {r['field']} | {r['code_tp']} | {r['code_fp']} | {r['code_fn']} | "
                  f"{_pct(r['code_precision'])} | {_pct(r['code_recall'])} |" for r in coded]
    shown = [r for r in t["by_field"] if r["reviewed"] or r["unresolved"]]
    lines += ["", "## By field (fields with at least one decision)", "", STAT_HEADER.format(first="Field")]
    lines += [_stat_row(f"{r['number']}. {r['field']}" if r["number"] != "" else f"Diagnosis check: {r['field']}", r)
              for r in shown]
    return "\n".join(lines)


# the files of an export that hold counts only (safe to share); decisions_with_study_ids.csv is never among them
SHAREABLE = ("accuracy_report.md", "accuracy_overall.csv", "accuracy_by_instrument.csv", "accuracy_by_field.csv",
             "episodes.csv")


def snapshots(out_dir: Path) -> list[Path]:
    """Finished exports in `out_dir`, oldest first (each run writes a new dated folder)."""
    d = Path(out_dir)
    done = [p for p in d.glob("accuracy_*") if (p / "accuracy_report.md").exists()] if d.exists() else []
    return sorted(done, key=lambda p: (p / "accuracy_report.md").stat().st_mtime)


def shareable_zip(folder: Path) -> bytes:
    """The counts-only files of one export, zipped (for the guided app's download button)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in SHAREABLE:
            if (folder / name).exists():
                z.write(folder / name, f"{folder.name}/{name}")
    return buf.getvalue()


def _write_csv(path: Path, rows: list[dict], cols: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else ("" if v is None else v)) for k, v in r.items()})


def _models(store: ReviewStore) -> list[str]:
    latest: dict[str, str] = {}
    for r in store.extract.execute("SELECT config_json FROM runs ORDER BY started_at"):
        try:
            cfg = json.loads(r[0])
        except (json.JSONDecodeError, TypeError):
            continue
        if cfg.get("group") and cfg.get("model"):
            latest[cfg["group"]] = cfg["model"]
    return sorted(set(latest.values()))


def run_accuracy(review: Path, extract: Path, deid: Path, out_dir: Path, dictionary: Path | None = None,
                 label: str = "", stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    for p, what in ((extract, "extraction database (run step 7)"), (deid, "notes database (step 5)"),
                    (review, "review database (review some patients in step 8)")):
        if not Path(p).exists():
            print(f"No {what} yet: nothing to measure.", file=stdout)
            return 1
    store = ReviewStore(review, extract, deid, dictionary)
    try:
        n_decisions = store.review.execute("SELECT COUNT(*) FROM reviews WHERE action IN ('accept','correct',"
                                           "'unresolved')").fetchone()[0]
        if not n_decisions:
            print("No review decisions yet: review some patients first (step 8).", file=stdout)
            return 1
        decisions, episodes, patients = collect(store)
        t = tables(store, decisions, episodes)
        meta = {"label": label, "models": _models(store), "patients": dict(patients),
                "notes": "de-identified" if store.notes_deidentified() else "original (not de-identified)",
                "reviewers": store.review.execute("SELECT COUNT(DISTINCT reviewer) FROM reviews").fetchone()[0]}
    finally:
        store.close()
    text = render(t, meta)
    print(text, file=stdout)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder, n = Path(out_dir) / f"accuracy_{stamp}", 1
    while folder.exists():   # each run its own snapshot
        n += 1
        folder = Path(out_dir) / f"accuracy_{stamp}_{n}"
    ensure_phi_output(folder / "accuracy_report.md", "the accuracy tables")
    (folder / "accuracy_report.md").write_text(text + "\n", encoding="utf-8")
    _write_csv(folder / "accuracy_overall.csv", t["overall"], ["group", *STAT_COLS])
    _write_csv(folder / "accuracy_by_instrument.csv", t["by_instrument"], ["instrument", "label", *STAT_COLS])
    _write_csv(folder / "accuracy_by_field.csv", t["by_field"],
               ["instrument", "number", "field", "label", "source", "field_group", *STAT_COLS])
    _write_csv(folder / "episodes.csv", t["episodes"], EPISODE_COLS)
    _write_csv(folder / "decisions_with_study_ids.csv", decisions, DECISION_COLS)
    print(f"\nSaved in {folder}: accuracy_report.md, accuracy_overall.csv, accuracy_by_instrument.csv, "
          "accuracy_by_field.csv and episodes.csv (counts only, safe to share), and decisions_with_study_ids.csv "
          "(one row per decision with study IDs: keep it private).", file=stdout)
    return 0
