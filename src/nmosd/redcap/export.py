"""`nmosd export`: REDCap flat import CSV (study-ID space) from ACCEPTED or CORRECTED values only.

* record_id = study ID; redcap_repeat_instrument / redcap_repeat_instance for repeating instruments;
  instance numbers are a persisted, stable mapping from instance_key (never renumbered on re-export).
* Checkbox fields expand to `field___code` columns; dates are YYYY-MM-DD; UNK/NA allowed.
* Calc and descriptive fields are skipped; identifier fields (mrn, dob) are added only by `reidentify`.
* `<form>_complete` is never set to Complete (2).
* Validation against the dictionary: codes, date format, numeric ranges, branching-logic warnings.
* Completeness warnings (counts only): fields left out because they were never reviewed or are unresolved,
  and exported values with no note text behind them (no proposal evidence and no attached passage), except
  structured-only fields (ethnicity), which are never looked for in the notes.
* Only patients whose diagnosis check is INCLUDED (NMOSD confirmed with enough follow-up) are exported;
  excluded patients and patients whose check is not done are counted, never written.
* Format: REDCap's standard column order, or exactly the header of the project's own REDCap export
  (`--template` / paths.redcap_template; header line only) with its BOM, line endings and quoting.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import TextIO

from nmosd.extract.run import resolve_dictionary
from nmosd.redcap.csv_format import (
    REDCAP_SPECIAL,
    CsvStyle,
    format_date,
    read_template,
    standard_columns,
    write_csv,
)
from nmosd.redcap.dictionary import (
    MISSING_DATA_CODES,
    Dictionary,
    Field,
    default_dictionary_path,
    eval_branching,
    load_dictionary,
)
from nmosd.redcap.schemas import STRUCTURED_ONLY
from nmosd.redcap.sources import repeating_forms
from nmosd.review.store import INSTRUMENT_FIELDS, ReviewStore
from nmosd.safety import ensure_phi_output

MISSING = {"UNK", "NA"}
REPEATING = repeating_forms(load_dictionary(default_dictionary_path()))   # every form but demographics (16.11.26)
COMPLETE_STATUS = "0"   # Incomplete; never 2 (Complete)
EXPORT_NAME = "redcap_import_deidentified.csv"

INSTANCE_TABLE = """CREATE TABLE IF NOT EXISTS instance_numbers (
    study_id TEXT NOT NULL, instrument TEXT NOT NULL, instance_key TEXT NOT NULL, instance INTEGER NOT NULL,
    PRIMARY KEY (study_id, instrument, instance_key))"""


def stable_instances(store: ReviewStore, study_id: str, instrument: str, keys_in_order: list[str]) -> dict[str, int]:
    con = store.review
    con.execute(INSTANCE_TABLE)
    have = {r[0]: r[1] for r in con.execute(
        "SELECT instance_key, instance FROM instance_numbers WHERE study_id=? AND instrument=?", (study_id, instrument))}
    nxt = max(have.values(), default=0) + 1
    for k in keys_in_order:
        if k not in have:
            have[k] = nxt
            con.execute("INSERT INTO instance_numbers VALUES (?,?,?,?)", (study_id, instrument, k, nxt))
            nxt += 1
    con.commit()
    return have


_eval_branching = eval_branching   # moved to nmosd.redcap.dictionary (kept for callers)


def validate_value(f: Field, value: str) -> str | None:
    if value in MISSING:
        return None
    if f.is_date:
        try:
            date.fromisoformat(value)
        except ValueError:
            return "bad date"
        return None
    if f.ftype in ("radio", "dropdown", "yesno", "truefalse") and value not in f.choices:
        return "code not in dictionary"
    if f.validation in ("integer", "number", "number_1dp") and value:
        try:
            x = float(value)
        except ValueError:
            return "not a number"
        if (f.vmin and x < float(f.vmin)) or (f.vmax and x > float(f.vmax)):
            return "out of range"
    return None


GATE_ISSUE = {"excluded": "patients not exported: excluded at the diagnosis check",
              "pending": "patients not exported: diagnosis check not done (warning)",
              "unresolved": "patients not exported: diagnosis check unresolved (warning)"}


def build_rows(store: ReviewStore, dic: Dictionary) -> tuple[list[dict], list[str], Counter]:
    rows, issues = [], Counter()
    used_forms = set()
    for sid in store.patients():
        gate = store.gate(sid)
        if not gate.included:
            issues[GATE_ISSUE[gate.status]] += 1
            continue
        issues["patients exported"] += 1
        for inst in INSTRUMENT_FIELDS:
            for key in store.instances(sid, inst):
                for fs in store.fields(sid, inst, key):
                    if fs.status == "pending":
                        issues["not exported: never reviewed (warning)"] += 1
                    elif fs.status == "unresolved":
                        issues["not exported: marked unresolved (warning)"] += 1
        reviewed = store.reviewed_values(sid)
        for instrument, instances in reviewed.items():
            order = [k for k in store.instances(sid, instrument) if k in instances]
            numbers = stable_instances(store, sid, instrument, order) if instrument in REPEATING else {}
            for key in order:
                row = {"record_id": sid, "redcap_repeat_instrument": instrument if instrument in REPEATING else "",
                       "redcap_repeat_instance": str(numbers[key]) if instrument in REPEATING else ""}
                for fname, v in instances[key].items():
                    if fname not in dic:
                        issues["field not in dictionary"] += 1
                        continue
                    f = dic[fname]
                    if not f.is_exportable or f.identifier:
                        issues["skipped calc/descriptive/identifier field"] += 1
                        continue
                    val = v["value"]
                    if f.is_checkbox:
                        try:
                            codes = set(json.loads(val)) if val else set()
                        except json.JSONDecodeError:
                            codes = {val}
                        # missing-data codes (UNK/NA) have their own ___unk/___na columns, like REDCap's export
                        for c, col in f.checkbox_columns(MISSING_DATA_CODES).items():
                            row[col] = "1" if c in codes else "0"
                        bad = codes - set(f.choices) - set(MISSING_DATA_CODES)
                        if bad:
                            issues["checkbox code not in dictionary"] += 1
                        if codes and not v.get("note_evidence"):
                            issues[f"exported with no note evidence (warning): {fname}"] += 1
                        continue
                    if val in (None, ""):
                        continue
                    err = validate_value(f, str(val))
                    if err:
                        issues[err] += 1
                        continue
                    row[fname] = str(val)
                    if not v.get("note_evidence") and fname not in STRUCTURED_ONLY:
                        issues[f"exported with no note evidence (warning): {fname}"] += 1
                    est_field = dic.estimate_for(fname)
                    if est_field and v.get("estimate") and est_field not in instances[key]:
                        row[est_field] = v["estimate"]
                row[f"{dic.form_fields(instrument)[0].form if dic.form_fields(instrument) else instrument}_complete"] = COMPLETE_STATUS
                used_forms.add(instrument)
                for fname in list(row):
                    base = fname.split("___")[0]
                    if base in dic and dic[base].branching:
                        ok = _eval_branching(dic[base].branching, row)
                        if ok is False:
                            issues["value present but branching logic hides the field (warning)"] += 1
                rows.append(row)
    return rows, standard_columns(dic, used_forms), issues


def apply_template(rows: list[dict], columns: list[str], template: Path) -> tuple[list[dict], list[str], CsvStyle,
                                                                                  list[str], Counter]:
    """Use the template's header (exact order) and style. Returns rows, columns, style, the names of filled
    columns the template lacks (left out), and notes (counts only)."""
    tcols, style = read_template(template)
    if not tcols:
        raise SystemExit("The template has no header row.")
    notes = Counter()
    id_col = tcols[0]
    if id_col != "record_id":
        rows = [{(id_col if k == "record_id" else k): v for k, v in r.items()} for r in rows]
        notes[f"record ID column renamed to the template's first column ({id_col})"] += 1
    filled = {k for r in rows for k, v in r.items() if v not in (None, "")}
    dropped = sorted(c for c in filled if c not in tcols)
    if "redcap_event_name" in tcols:
        notes["template has redcap_event_name (longitudinal project): rows written with it blank (warning)"] += 1
    other = [c for c in tcols if c in REDCAP_SPECIAL - {"redcap_repeat_instrument", "redcap_repeat_instance",
                                                         "redcap_event_name"}]
    if other:
        notes[f"template columns left blank: {', '.join(other)}"] += 1
    notes["template columns we never fill (left blank)"] += len([c for c in tcols if c not in columns and c not in
                                                                REDCAP_SPECIAL and c != id_col])
    return rows, tcols, style, dropped, notes


def run_export(extract: Path, review: Path, dictionary: Path | None, out_dir: Path, deid: Path | None = None,
               settings=None, template: Path | None = None, date_format: str | None = None,
               stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    out_dir = Path(out_dir)
    ensure_phi_output(out_dir / EXPORT_NAME, "the REDCap export")
    dic_path = resolve_dictionary(dictionary, stdout)
    date_format = date_format or (settings.get("export.date_format") if settings else None) or "YYYY-MM-DD"
    if deid is None:
        if settings is None:
            raise SystemExit("--deid (or paths.deid_db in settings) is required")
        deid = settings.path("deid_db")
    dic = load_dictionary(dic_path)
    store = ReviewStore(review, extract, deid, dic_path)
    try:
        rows, columns, issues = build_rows(store, dic)
    finally:
        store.close()
    # values stay YYYY-MM-DD until here; write dates the way the project's REDCap export does
    date_cols = {n for n, f in dic.fields.items() if f.is_date}
    rows = [{k: (format_date(v, date_format) if k in date_cols else v) for k, v in r.items()} for r in rows]
    style, dropped = CsvStyle(), []
    if template:
        rows, columns, style, dropped, notes = apply_template(rows, columns, template)
        issues.update(notes)
        if dropped:
            issues[f"filled columns missing from the template, left out: {', '.join(dropped)}"] += 1
    write_csv(out_dir / EXPORT_NAME, columns, rows, style)
    by_inst = Counter(r["redcap_repeat_instrument"] or "demographics" for r in rows)
    # every key that is not a warning or a count of what was skipped is a validation error
    errors = sum(v for k, v in issues.items() if "warning" not in k and not k.startswith(("skipped", "patients",
                                                                                          "template", "record ID")))
    print(f"Wrote {len(rows)} rows ({dict(by_inst)}) to {EXPORT_NAME}"
          f"{' in the template layout (' + style.describe() + ')' if template else ''}, dates as {date_format}. "
          f"Validation errors: {errors}.", file=stdout)
    for k, v in sorted(issues.items()):
        print(f"  {k}: {v}", file=stdout)
    return 1 if errors else 0
