"""Review data layer (UI-independent, so it is fully testable).

* Proposals come from the latest reconcile run in the extraction DB and are never modified.
* Every reviewer action is appended immediately to `reviews` (append-only, enforced by triggers):
  reviewer, timestamp, old value, new value, reason, optional attached evidence passage.
* The current value of a field is the newest review action's value, else the proposal's.
* Pages load from the databases only; the model is never re-run.
* The diagnosis check (GATE) comes first for every patient: other instruments stay locked until the reviewer
  confirms NMOSD with enough follow-up, and an excluded patient is never exported. Nothing is excluded without
  a reviewer's decision; a borderline case also needs a written reason.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from nmosd.config import load_rules
from nmosd.extract.quote_verify import locate_quote
from nmosd.redcap.dictionary import (
    Dictionary,
    branching_row,
    default_dictionary_path,
    eval_branching,
    load_dictionary,
)
from nmosd.redcap.sources import hospital_names_kept, review_fields
from nmosd.store.db import connect, get_meta

GATE = "eligibility"            # pseudo-instrument: the diagnosis check (not a REDCap form, never exported)
GATE_FIELDS = ["dx_category", "followup_12m"]


@lru_cache(maxsize=1)
def gate_rules() -> dict:
    return load_rules("eligibility.yaml")


def gate_labels() -> dict[str, dict[str, str]]:
    r = gate_rules()
    months = r.get("min_followup_months", 12)
    return {"dx_category": dict(r["categories"]),
            "followup_12m": {"1": f"Yes: at least {months:g} months of follow-up",
                             "0": f"No: less than {months:g} months"}}


# REDCap instruments reviewed after the diagnosis check, with every field the extractor proposes, in the codebook's
# order (16.11.26: all 12 instruments of the codebook; see nmosd.redcap.sources for where each field comes from).
@lru_cache(maxsize=2)
def instrument_fields(hospital_names: bool = False) -> dict[str, list[str]]:
    """hospital_names: the notes still name hospitals, so the hospital fields (hx_opcare, nr_hosp, nr_hosp_2) are
    reviewed too (18.11.26). A ReviewStore uses the list that matches its notes (`ReviewStore.instrument_fields`)."""
    return review_fields(load_dictionary(default_dictionary_path()), hospital_names)


# The instruments (the same either way) and their fields for de-identified notes with hospital names tokenised.
INSTRUMENT_FIELDS = instrument_fields(False)
INFO_FIELD = "_info"   # per-instance notes on how values were worked out (stage2_reconcile.INFO_FIELD)
ACTIONS = {"accept", "correct", "unresolved", "comment", "add_instance", "delete_instance", "restore_instance",
           "attach_evidence"}


@dataclass
class FieldState:
    study_id: str
    instrument: str
    instance_key: str
    field: str
    proposed: str | None
    value: str | None
    estimate_code: str | None
    status: str            # pending | accepted | corrected | unresolved
    conflict: str | None
    evidence_ids: list[str]
    anchor: str | None
    attached_evidence: bool = False   # the reviewer's correction carries a note passage


@dataclass
class GateState:
    status: str             # pending | included | excluded | unresolved
    dx: str | None          # current dx_category value
    followup: str | None    # current followup_12m value
    reason: str             # plain-language explanation for the reviewer

    @property
    def included(self) -> bool:
        return self.status == "included"


class ReviewStore:
    def __init__(self, review_db: Path, extract_db: Path, deid_db: Path, dictionary: Path | None = None):
        self.review = connect(review_db, "review")
        self.extract = connect(extract_db, readonly=True)
        self.deid = connect(deid_db, readonly=True)
        self.dictionary: Dictionary = load_dictionary(dictionary or default_dictionary_path())
        r = self.extract.execute("SELECT MAX(run_id) FROM proposals").fetchone()
        self.run_id = r[0] if r else None
        # the notes still name hospitals (kept as they are, or hospital names not tokenised): hospital fields reviewed
        self.hospital_names = hospital_names_kept(self._manifest())
        self.instrument_fields = instrument_fields(self.hospital_names)

    def close(self) -> None:
        for c in (self.review, self.extract, self.deid):
            c.close()

    def _fields_of(self, instrument: str) -> list[str]:
        return GATE_FIELDS if instrument == GATE else self.instrument_fields[instrument]

    # ---- read ---------------------------------------------------------------------------
    def _manifest(self) -> dict:
        try:
            return get_meta(self.deid, "manifest")
        except sqlite3.Error:
            return {}

    def notes_deidentified(self) -> bool:
        """False when step 5 kept the notes as they are (de-identification is optional)."""
        return bool(self._manifest().get("deidentified", True))

    def patients(self) -> list[str]:
        return [r[0] for r in self.deid.execute("SELECT study_id FROM patients ORDER BY study_id")]

    def _reviews(self, study_id: str) -> list[dict]:
        return [dict(r) for r in self.review.execute("SELECT * FROM reviews WHERE study_id=? ORDER BY review_id",
                                                     (study_id,))]

    def instances(self, study_id: str, instrument: str, include_deleted: bool = False) -> list[str]:
        keys = [r[0] for r in self.extract.execute(
            "SELECT instance_key FROM proposals WHERE run_id=? AND study_id=? AND instrument=? GROUP BY instance_key "
            "ORDER BY MIN(COALESCE(anchor_date, ''))", (self.run_id, study_id, instrument))]
        deleted = set()
        for r in self._reviews(study_id):
            if r["instrument"] != instrument:
                continue
            if r["action"] == "add_instance" and r["instance_key"] not in keys:
                keys.append(r["instance_key"])
            elif r["action"] == "delete_instance":
                deleted.add(r["instance_key"])
            elif r["action"] == "restore_instance":
                deleted.discard(r["instance_key"])
        return keys if include_deleted else [k for k in keys if k not in deleted]

    def fields(self, study_id: str, instrument: str, instance_key: str) -> list[FieldState]:
        props = {r["field"]: dict(r) for r in self.extract.execute(
            "SELECT * FROM proposals WHERE run_id=? AND study_id=? AND instrument=? AND instance_key=?",
            (self.run_id, study_id, instrument, instance_key))}
        last: dict[str, dict] = {}
        for r in self._reviews(study_id):
            if r["instrument"] == instrument and r["instance_key"] == instance_key and r["field"] and \
                    r["action"] in ("accept", "correct", "unresolved"):
                last[r["field"]] = r
        out = []
        for f in self._fields_of(instrument):
            if instrument != GATE and f not in self.dictionary:
                continue
            p = props.get(f, {})
            rv = last.get(f)
            status = {"accept": "accepted", "correct": "corrected", "unresolved": "unresolved"}[rv["action"]] if rv else "pending"
            value = rv["new_value"] if rv and rv["action"] == "correct" else p.get("value")
            est = rv["estimate_code"] if rv and rv["action"] == "correct" and rv["estimate_code"] else None
            out.append(FieldState(study_id, instrument, instance_key, f, p.get("value"), value, est, status,
                                  p.get("conflict_note"), json.loads(p["evidence_ids"]) if p.get("evidence_ids") else [],
                                  p.get("anchor_date"),
                                  bool(rv and rv["action"] == "correct" and rv.get("evidence_doc_id"))))
        return out

    def visible(self, fields: list[FieldState]) -> list[FieldState]:
        """The fields REDCap would show given the instance's current values (branching logic), plus any hidden one
        that has a value (so a proposal is never silently dropped)."""
        if not fields or fields[0].instrument == GATE:
            return fields
        row = branching_row(self.dictionary, {fs.field: fs.value for fs in fields})
        out = []
        for fs in fields:
            b = self.dictionary[fs.field].branching if fs.field in self.dictionary else ""
            if not b or fs.value not in (None, "", "[]") or eval_branching(b, row) is not False:
                out.append(fs)
        return out

    def info(self, study_id: str, instrument: str, instance_key: str) -> list[str]:
        """Notes on how this instance's values were worked out (not conflicts)."""
        r = self.extract.execute("SELECT value FROM proposals WHERE run_id=? AND study_id=? AND instrument=? AND "
                                 "instance_key=? AND field=?", (self.run_id, study_id, instrument, instance_key,
                                                                INFO_FIELD)).fetchone()
        try:
            return json.loads(r[0]) if r and r[0] else []
        except json.JSONDecodeError:
            return []

    # ---- diagnosis check -------------------------------------------------------------------
    def gate(self, study_id: str) -> GateState:
        """Included only when the reviewer has accepted/corrected an NMOSD diagnosis AND enough follow-up."""
        f = {fs.field: fs for fs in self.fields(study_id, GATE, GATE)}
        dx, fu = f["dx_category"], f["followup_12m"]
        decided = lambda fs: fs.status in ("accepted", "corrected")  # noqa: E731
        include = set(gate_rules().get("include", ["nmosd"]))
        labels = gate_labels()
        if decided(dx) and dx.value not in include:
            return GateState("excluded", dx.value, fu.value,
                             f"Diagnosis: {labels['dx_category'].get(dx.value, dx.value)}")
        if decided(fu) and fu.value == "0":
            return GateState("excluded", dx.value, fu.value, labels["followup_12m"]["0"] + " of follow-up")
        if decided(dx) and decided(fu) and fu.value == "1":
            return GateState("included", dx.value, fu.value, "NMOSD confirmed with enough follow-up")
        if "unresolved" in (dx.status, fu.status):
            return GateState("unresolved", dx.value, fu.value, "Diagnosis check marked unresolved")
        return GateState("pending", dx.value, fu.value, "Diagnosis check not done yet")

    def gate_borderline(self, study_id: str) -> list[str]:
        """Why this case is borderline (empty when clear-cut): every flag the reconciler raised on the diagnosis
        check, or no proposal at all. A borderline case is never excluded without a written reason."""
        f = {fs.field: fs for fs in self.fields(study_id, GATE, GATE)}
        out = [n for fs in f.values() for n in (fs.conflict or "").split("; ") if n]
        if f["dx_category"].proposed is None:
            out.append("no diagnosis-check proposal (re-run reconcile)")
        return list(dict.fromkeys(out))

    def _json_proposal(self, study_id: str, instrument: str, field: str, default):
        """A reviewer aid stored as a JSON proposal row (not a REDCap field)."""
        r = self.extract.execute("SELECT value FROM proposals WHERE run_id=? AND study_id=? AND instrument=? AND "
                                 "field=?", (self.run_id, study_id, instrument, field)).fetchone()
        try:
            return json.loads(r[0]) if r and r[0] else default
        except json.JSONDecodeError:
            return default

    def gate_summary(self, study_id: str) -> dict:
        return self._json_proposal(study_id, GATE, "summary", {})

    def conditions(self, study_id: str) -> list[dict]:
        """Every condition the notes list ({name, category, autoimmune, documents, last}), for the comorbidities."""
        return self._json_proposal(study_id, "demographics", "conditions", [])

    def progress(self, study_id: str) -> tuple[int, int]:
        """Fields reviewed / to review. An excluded patient only needs the diagnosis check."""
        insts = [GATE] if self.gate(study_id).status == "excluded" else [GATE, *INSTRUMENT_FIELDS]
        done = total = 0
        for inst in insts:
            for key in ([GATE] if inst == GATE else self.instances(study_id, inst)):
                for fs in self.visible(self.fields(study_id, inst, key)):
                    total += 1
                    done += fs.status in ("accepted", "corrected", "unresolved")
        return done, total

    def conflicts(self, study_id: str) -> list[tuple[str, str, str]]:
        return [(r["instrument"], r["instance_key"], r["conflict_note"]) for r in self.extract.execute(
            "SELECT DISTINCT instrument, instance_key, conflict_note FROM proposals WHERE run_id=? AND study_id=? "
            "AND conflict_flag=1", (self.run_id, study_id))]

    # Every evidence/document lookup is scoped to one patient: the review UI must never show (or attach)
    # text from another patient's notes, whatever stale state it holds.
    def evidence(self, evidence_ids: list[str], study_id: str, newest_first: bool = False) -> list[dict]:
        if not evidence_ids:
            return []
        q = ",".join("?" * len(evidence_ids))
        order = "doc_date DESC, char_start" if newest_first else "doc_date, char_start"
        rows = [dict(r) for r in self.extract.execute(
            f"SELECT * FROM evidence WHERE evidence_id IN ({q}) AND study_id=? ORDER BY {order}",
            [*evidence_ids, study_id])]
        seen, out = set(), []
        for r in rows:  # one row per item/quote
            k = (r["item_id"], r["quote"])
            if k not in seen:
                seen.add(k)
                out.append(r)
        return out

    def passage_count(self, evidence_ids: list[str], study_id: str) -> int:
        """How many passages the evidence panel will show for these IDs (one model answer gives one evidence row
        per field, all quoting the same passage)."""
        return len(self.evidence(evidence_ids, study_id))

    @staticmethod
    def highlight_span(evidence: dict, text: str) -> tuple[int, int] | None:
        """Where to highlight this evidence's quote in the note as it is now (None: the quote is not in it)."""
        return locate_quote(text, evidence.get("quote") or "", evidence.get("char_start"), evidence.get("char_end"))

    def document(self, doc_id: str, study_id: str) -> dict | None:
        r = self.deid.execute("SELECT doc_id, study_id, source_table, report_date, report_description, relevance_class, "
                              "text FROM documents WHERE doc_id=? AND study_id=?", (doc_id, study_id)).fetchone()
        return dict(r) if r else None

    def _doc_ids(self, study_id: str) -> set[str]:
        return {r[0] for r in self.deid.execute("SELECT doc_id FROM documents WHERE study_id=?", (study_id,))}

    def repeats_of(self, doc_id: str, start: int | None, end: int | None, study_id: str) -> list[dict]:
        """Other notes of the same patient containing the same copied-forward paragraph."""
        if start is None:
            return []
        own = self._doc_ids(study_id)
        if doc_id not in own:
            return []
        rows = self.extract.execute(
            "SELECT * FROM paragraph_repeats WHERE first_doc_id=? AND first_char_start<=? AND first_char_end>=?",
            (doc_id, start, end or start))
        return [dict(r) for r in rows if r["doc_id"] in own]

    def search(self, study_id: str, query: str, date_from: str | None = None, date_to: str | None = None,
               doc_types: list[str] | None = None, limit: int = 200) -> list[dict]:
        """Notes of this patient containing the query, oldest first. The words of the query match whatever spaces,
        line breaks or hyphens separate them in the note ("spinal stroke" finds "spinal\nstroke"); when no note has
        them together, notes containing every word anywhere are returned instead (`match` says which: "phrase" or
        "all words"; 16.11.26). Case is ignored; a word also matches longer words starting with it (stroke → strokes)."""
        words = re.findall(r"[\w'+./]+", query or "")
        phrase = re.compile(r"(?<!\w)" + r"[\s\-_/.,:;]*".join(re.escape(w) for w in words), re.I) if words else None
        each = [re.compile(r"(?<!\w)" + re.escape(w), re.I) for w in words]
        rows = []
        for r in self.deid.execute("SELECT doc_id, report_date, report_description, source_table, text FROM documents "
                                   "WHERE study_id=? ORDER BY report_date", (study_id,)):
            if date_from and (r["report_date"] or "") < date_from:
                continue
            if date_to and (r["report_date"] or "") > date_to:
                continue
            if doc_types and r["source_table"] not in doc_types and r["report_description"] not in doc_types:
                continue
            rows.append(r)

        def hit(r, spans, mode):
            meta = {k: v for k, v in dict(r).items() if k != "text"}
            spans = sorted(spans)[:20]
            return {**meta, "hits": spans, "match": mode,
                    "snippets": [r["text"][max(0, s - 80):e + 80] for s, e in spans[:3]]}

        if phrase is None:
            return [{**{k: v for k, v in dict(r).items() if k != "text"}, "hits": [], "match": ""} for r in rows][:limit]
        out = [hit(r, [m.span() for m in phrase.finditer(r["text"])], "phrase") for r in rows if phrase.search(r["text"])]
        if not out and len(each) > 1:
            out = [hit(r, [m.span() for rx in each for m in rx.finditer(r["text"])], "all words") for r in rows
                   if all(rx.search(r["text"]) for rx in each)]
        return out[:limit]

    def labels(self, field: str) -> dict[str, str]:
        if field in GATE_FIELDS:
            return gate_labels()[field]
        if self.dictionary and field in self.dictionary:
            return dict(self.dictionary[field].choices)
        return {}

    def history(self, study_id: str) -> list[dict]:
        return self._reviews(study_id)

    # ---- write (append-only) ----------------------------------------------------------------
    def record(self, reviewer: str, action: str, study_id: str, instrument: str, instance_key: str,
               field: str | None = None, new_value: str | None = None, estimate_code: str | None = None,
               reason: str | None = None, evidence_doc_id: str | None = None, evidence_start: int | None = None,
               evidence_end: int | None = None) -> int:
        if action not in ACTIONS:
            raise ValueError(action)
        if not reviewer or not reviewer.strip():
            raise ValueError("reviewer name required")
        if evidence_doc_id is not None and self.document(evidence_doc_id, study_id) is None:
            raise ValueError("attached evidence is not one of this patient's notes")
        old = None
        if field and (instrument in INSTRUMENT_FIELDS or instrument == GATE):
            cur = next((f for f in self.fields(study_id, instrument, instance_key) if f.field == field), None)
            old = cur.value if cur else None
            if action == "accept":
                new_value = old
            if instrument == GATE and action in ("accept", "correct"):
                excludes = (field == "dx_category" and new_value not in set(gate_rules().get("include", ["nmosd"]))) \
                    or (field == "followup_12m" and new_value == "0")
                if excludes and not (reason or "").strip() and self.gate_borderline(study_id):
                    raise ValueError("Borderline case: look at the notes yourself, then write a reason before "
                                     "excluding this patient.")
        cur = self.review.execute(
            "INSERT INTO reviews (reviewer, at, study_id, instrument, instance_key, field, action, old_value, new_value, "
            "estimate_code, reason, evidence_doc_id, evidence_start, evidence_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (reviewer.strip(), datetime.now().isoformat(timespec="seconds"), study_id, instrument, instance_key, field,
             action, old, new_value, estimate_code, reason, evidence_doc_id, evidence_start, evidence_end))
        self.review.commit()
        return cur.lastrowid

    def add_instance(self, reviewer: str, study_id: str, instrument: str, reason: str | None = None) -> str:
        key = "manual-" + uuid.uuid4().hex[:10]
        self.record(reviewer, "add_instance", study_id, instrument, key, reason=reason)
        return key

    def start_session(self, reviewer: str, study_id: str | None = None) -> int:
        cur = self.review.execute("INSERT INTO reviewer_sessions (reviewer, study_id, started_at) VALUES (?,?,?)",
                                  (reviewer, study_id, datetime.now().isoformat(timespec="seconds")))
        self.review.commit()
        return cur.lastrowid

    # ---- export view --------------------------------------------------------------------------
    def reviewed_values(self, study_id: str) -> dict[str, dict[str, dict]]:
        """{instrument: {instance_key: {field: {value, estimate, status, anchor, note_evidence}}}} for accepted/corrected
        fields of the REDCap instruments only (never the diagnosis check; the export checks `gate()` itself).
        note_evidence: some note text backs the value (proposal evidence or an attached passage)."""
        out: dict[str, dict[str, dict]] = defaultdict(dict)
        for inst in INSTRUMENT_FIELDS:
            for key in self.instances(study_id, inst):
                vals = {f.field: {"value": f.value, "estimate": f.estimate_code, "status": f.status, "anchor": f.anchor,
                                  "note_evidence": bool(f.evidence_ids) or f.attached_evidence}
                        for f in self.fields(study_id, inst, key) if f.status in ("accepted", "corrected")}
                if vals:
                    out[inst][key] = vals
        return out
