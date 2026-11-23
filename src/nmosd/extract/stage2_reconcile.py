"""Stage 2: reconcile stage-1 evidence into per-patient REDCap proposals.

* Relapses: onset mentions clustered by date (±relapse_window_days, wider when either mention is low
  precision), most precise mention anchors the episode; admission/course/MRI evidence attaches by date or
  by position in the same document. Phenotype/date disagreements are flagged, not silently resolved.
* Treatments: episodes per drug from start/stop/ongoing evidence (plans, considerations and negated
  mentions never create episodes); structured administration records win for dates when present;
  switches and overlaps are noted. A drug with no dated start takes its start from an ongoing mention that
  says since when ("on rituximab since 2019").
* Single-value fields: most precise, then most frequent; conflicts flagged. With no diagnosis date stated,
  the date of the first note stating NMOSD is proposed as an estimate (code 4) and flagged.
* Structured Demographics (sex, race, ethnicity, living/deceased) map directly from the RPDR Demographics
  table and stay the proposal. Since 16.11.26 all four are structured only (STRUCTURED_ONLY, `note_check: false`):
  not checked against the notes. A value from a fallback column (Gender_Legal_Sex, Race_Group) or a missing one
  is flagged for the reviewer. A field with `note_check` on gets the `demographics` note mentions as its
  evidence (only mentions whose quote has the field's own wording), and a disagreement is noted.
* Comorbidities: the REDCap categories of the conditions the notes list, plus every named condition (with its
  category, also those outside the REDCap categories) for the reviewer.
* Diagnosis check (the `eligibility` pseudo-instrument, reviewed first; config/eligibility.yaml): the
  patient's current diagnosis from the most recent clinic letters and hospital notes (a copied-forward
  statement counts at the date of the latest note that repeats it), checked against the AQP4/MOG results,
  plus months of follow-up from the NMOSD diagnosis to the last clinical note.
Every proposal carries its evidence IDs and a stable instance_key, so re-runs do not duplicate episodes.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

from nmosd.config import load_rules
from nmosd.extract import stage2_instruments as s2
from nmosd.extract.checks import mark_unnamed, says_first_dose
from nmosd.extract.prefilter import field_keyword_rx
from nmosd.redcap.dictionary import Dictionary, default_dictionary_path, load_dictionary
from nmosd.redcap.schemas import (
    NE,
    NMOSD_RELATED,
    NO_CATEGORY,
    STRUCTURED_ONLY,
    STRUCTURED_SOURCES,
    anchor_keys,
    structured_values,
)
from nmosd.redcap.sources import HOSPITAL_GROUP, hospital_names_kept
from nmosd.store.db import connect, get_meta
from nmosd.textproc.dates import date_and_estimate

GATE = "eligibility"   # pseudo-instrument for the diagnosis check (never exported)
INFO_FIELD = "_info"   # per-instance proposal row: JSON list of notes on how values were derived (not a REDCap field)

DRUG_WORDS = {"rituximab": "1", "rituxan": "1", "mycophenolate": "2", "cellcept": "2", "eculizumab": "3", "soliris": "3",
              "ravulizumab": "4", "ultomiris": "4", "inebilizumab": "5", "uplizna": "5", "satralizumab": "6",
              "enspryng": "6", "azathioprine": "7", "imuran": "7", "tocilizumab": "8", "actemra": "8",
              "methotrexate": "13", "cyclophosphamide": "14"}


@dataclass
class Item:
    item_id: str
    group: str
    doc_id: str
    doc_date: str | None
    char_start: int | None
    fields: dict = field(default_factory=dict)
    event_date: str | None = None
    estimate: str | None = None
    negated: bool = False
    hypothetical: bool = False
    evidence_ids: list[str] = field(default_factory=list)
    field_evidence: dict[str, str] = field(default_factory=dict)   # field -> its evidence_id
    quote: str | None = None
    unnamed: bool = False     # a treatment mention whose passage does not name its drug (nmosd.extract.checks)

    @property
    def d(self) -> date | None:
        return date.fromisoformat(self.event_date) if self.event_date and self.event_date != NE else None

    @property
    def precision_rank(self) -> int:
        return int(self.estimate) if self.estimate and self.estimate.isdigit() else 5


def load_items(con, study_id: str) -> list[Item]:
    latest = {}
    for r in con.execute("SELECT run_id, config_json, started_at FROM runs ORDER BY started_at"):
        latest[json.loads(r["config_json"])["group"]] = r["run_id"]
    items: dict[str, Item] = {}
    rank: dict[str, int] = {}
    for r in con.execute("SELECT * FROM evidence WHERE study_id = ? ORDER BY doc_date, char_start", (study_id,)):
        if latest.get(r["field_group"]) not in (None, r["run_id"]):
            continue
        it = items.get(r["item_id"])
        if it is None:
            it = items[r["item_id"]] = Item(r["item_id"], r["field_group"], r["doc_id"], r["doc_date"], r["char_start"],
                                            negated=bool(r["negated"]), hypothetical=bool(r["hypothetical"]),
                                            quote=r["quote"])
        v = r["value"]
        it.fields[r["field"]] = json.loads(v) if v and v.startswith("[") else v
        it.evidence_ids.append(r["evidence_id"])
        it.field_evidence[r["field"]] = r["evidence_id"]
        # the item's date: its group's first anchor key present (e.g. nr_admission before nr_discharge)
        anchors = anchor_keys(r["field_group"])
        k = anchors.index(r["field"]) if r["field"] in anchors else len(anchors)
        if (k < len(anchors) and k < rank.get(it.item_id, len(anchors) + 1)) or (it.event_date is None and r["event_date"]):
            it.event_date, it.estimate = r["event_date"], r["estimate_code"]
            rank[it.item_id] = k
    return list(items.values())


def instance_key(study_id: str, instrument: str, anchor: str) -> str:
    return hashlib.sha256(f"{study_id}|{instrument}|{anchor}".encode()).hexdigest()[:12]


def _days(a: date, b: date) -> int:
    return abs((a - b).days)


def _most_common(values: list):
    c = Counter(json.dumps(v, sort_keys=True) for v in values if v not in (None, NE, "", []))
    if not c:
        return None, False
    top = c.most_common()
    return json.loads(top[0][0]), len(top) > 1


# ------------------------------------------------------------------------------------------
# relapses
# ------------------------------------------------------------------------------------------
@dataclass
class Episode:
    anchor: Item
    members: list[Item] = field(default_factory=list)
    attached: list[Item] = field(default_factory=list)


def reconcile_relapses(items: list[Item], window: int, wide_window: int) -> list[dict]:
    rel = [i for i in items if i.group == "relapse" and not i.negated and not i.hypothetical]
    onset = [i for i in rel if i.fields.get("date_onset") and i.d]
    rest = [i for i in rel if i not in onset]
    onset.sort(key=lambda i: (i.precision_rank, i.d))
    eps: list[Episode] = []
    for it in onset:
        best, best_gap = None, None
        for ep in eps:
            w = wide_window if max(it.precision_rank, ep.anchor.precision_rank) >= 3 else window
            gap = _days(it.d, ep.anchor.d)
            if gap <= w and (best_gap is None or gap < best_gap):
                best, best_gap = ep, gap
        if best is None:
            eps.append(Episode(it, [it]))
        else:
            best.members.append(it)
    eps.sort(key=lambda e: e.anchor.d)
    # attach dated non-onset items (admission/discharge, radiology) and dateless items by document position
    for it in rest:
        adm = it.fields.get("date_admission")
        target = None
        if adm and adm != NE:
            a = date.fromisoformat(adm)
            cands = [e for e in eps if -3 <= (a - e.anchor.d).days <= 30]
            target = min(cands, key=lambda e: _days(a, e.anchor.d)) if cands else None
        elif "mri_relapse" in it.fields and it.doc_date:
            dd = date.fromisoformat(it.doc_date)
            cands = [e for e in eps if -14 <= (dd - e.anchor.d).days <= 60]
            target = min(cands, key=lambda e: _days(dd, e.anchor.d)) if cands else None
        else:
            same_doc = [(m, e) for e in eps for m in e.members if m.doc_id == it.doc_id and m.char_start is not None
                        and it.char_start is not None and m.char_start <= it.char_start]
            if same_doc:
                target = max(same_doc, key=lambda me: me[0].char_start)[1]
        if target:
            target.attached.append(it)
    out = []
    for ep in eps:
        allm = ep.members + ep.attached
        phen_sets = [set(m.fields.get("relapse_phenotype") or []) for m in ep.members if m.fields.get("relapse_phenotype")]
        union = sorted(set().union(*phen_sets)) if phen_sets else []
        notes = []
        if any(not (a & b) for i, a in enumerate(phen_sets) for b in phen_sets[i + 1:]):
            notes.append("phenotypes differ between mentions")
        far = [m for m in ep.members if _days(m.d, ep.anchor.d) > (window if m.precision_rank < 3 else 0)
               and m.precision_rank < 3]
        if far:
            notes.append(f"{len(far)} mention(s) with dates >{window} days from the anchor")
        precise = [m for m in ep.members if m.precision_rank <= ep.anchor.precision_rank]
        if len({m.event_date for m in precise}) > 1:
            notes.append("equally precise mentions give different onset dates")
        adm, adm_conf = _most_common([m.fields.get("admission") for m in allm])
        dadm, dadm_conf = _most_common([m.fields.get("date_admission") for m in allm])
        ddis, ddis_conf = _most_common([m.fields.get("date_discharge") for m in allm])
        if dadm and adm in (None, "0"):
            adm_conf = adm_conf or adm == "0"
            adm = "1"
        mri_vals = [m.fields.get("mri_relapse") for m in allm if m.fields.get("mri_relapse") not in (None, NE)]
        mri = "1" if "1" in mri_vals else ("0" if mri_vals else None)
        acute = sorted({c for m in allm for c in (m.fields.get("acute_rx") or [])})
        for flag, label in ((adm_conf, "admission"), (dadm_conf, "admission date"), (ddis_conf, "discharge date"),
                            (len(set(mri_vals)) > 1, "MRI")):
            if flag:
                notes.append(f"conflicting {label} evidence")
        out.append({"anchor": ep.anchor.event_date, "values": {
            "date_onset": ep.anchor.event_date, "date_attack_estimate": ep.anchor.estimate or NE,
            "relapse_phenotype": union, "admission": adm, "date_admission": dadm if adm == "1" else None,
            "date_discharge": ddis if adm == "1" else None, "acute_rx": acute, "mri_relapse": mri},
            "evidence": [e for m in allm for e in m.evidence_ids], "conflict": "; ".join(notes), "members": allm})
    return out


# ------------------------------------------------------------------------------------------
# treatments
# ------------------------------------------------------------------------------------------
# "since" directly followed by a date ("since 2019", "since early 2019", "since 3/2018"), not "since her last visit"
SINCE_RX = re.compile(r"(?i)\bsince\s+(?:(?:early|late|mid|around|about|approximately|~)[\s-]*)?"
                      r"(?=\d|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|spring|summer|fall|autumn|winter))")


def since_start(i: Item) -> Item | None:
    """'has been on rituximab since 2019': an ongoing (or undated start) mention whose quote says since when.
    Returns a copy dated at that time (estimate code from how precisely it is written), else None."""
    m = SINCE_RX.search(i.quote or "")
    if not m:
        return None
    pivot = int(i.doc_date[:4]) if i.doc_date else date.today().year
    found = date_and_estimate(i.quote[m.end():m.end() + 40], pivot, max_start=0)
    if not found:
        return None
    iso, est = found
    if i.doc_date and iso > i.doc_date[:10]:
        if iso[:4] > i.doc_date[:4]:
            return None              # a start after the note that reports it: not a since-date
        iso = i.doc_date[:10]        # "since 2019" in a note of March 2019: the middle of the year is too late
    return replace(i, event_date=iso, estimate=est)


# Repeated doses of one drug (an infusion every 8 weeks, every 6 months) are one course: courses of the same drug
# are merged unless a stop is documented between them or the drug goes unmentioned for longer than this (16.11.26:
# 12 ravulizumab "courses" for one patient). Still on therapy follows the most recent notes that mention the drug.
MAX_COURSE_GAP_DAYS = 365
NOT_RECENT_DAYS = 365


ADMIN_GROUP = "treatment_admin"   # administration records read with text patterns (nmosd.extract.admin_records)


def _dose_number(i: Item) -> float | None:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(i.fields.get("maintenance_tx_dosing") or ""))
    return float(m.group(0).replace(",", "")) if m else None


def _doc_d(i: Item) -> date | None:
    return date.fromisoformat(i.doc_date[:10]) if i.doc_date else None


def _mention_d(i: Item) -> date | None:
    return max(x for x in (i.d if i.fields.get("event") != "ongoing" else None, _doc_d(i)) if x) \
        if (i.d or i.doc_date) else None


def reconcile_treatments(items: list[Item], structured_starts: dict[str, list[date]] | None = None) -> list[dict]:
    tx = [i for i in items if i.group in ("treatment", ADMIN_GROUP) and not i.negated and not i.hypothetical
          and i.fields.get("event") in ("start", "stop", "ongoing") and i.fields.get("maintenance_therapy") not in (None, NE, "UNK")]
    by_drug = defaultdict(list)
    for i in tx:
        by_drug[i.fields["maintenance_therapy"]].append(i)
    # Code checks (18.11.26, nmosd.extract.checks). A mention whose passage does not name the drug is left out when
    # other mentions do name it; a drug that no passage names is still proposed, marked for the reviewer.
    unnamed_only, left_out = set(), Counter()
    for drug, its in list(by_drug.items()):
        named = [i for i in its if not i.unnamed]
        if not named:
            unnamed_only.add(drug)
        elif len(named) < len(its):
            left_out[drug] = len(its) - len(named)
            by_drug[drug] = named
    # ... and "infusion stopped at 14:05" in an infusion note is the end of that infusion, not of the course
    infusion_ends = Counter()
    for drug, its in list(by_drug.items()):
        ends = [i for i in its if i.fields["event"] == "stop" and i.d and _doc_d(i) == i.d
                and any(j.doc_id == i.doc_id and j.fields["event"] == "start" and j.d == i.d for j in its)]
        if ends:
            infusion_ends[drug] = len(ends)
            by_drug[drug] = [i for i in its if not any(i is e for e in ends)]
    # Administration records (text patterns, `treatment_admin`): when the first recorded dose differs from the
    # usual later dose it is the loading dose, so that day is the start of the course.
    loading: dict[str, date] = {}
    for drug, its in list(by_drug.items()):
        admins = sorted([i for i in its if i.group == ADMIN_GROUP and i.d and _dose_number(i)], key=lambda i: i.d)
        if len(admins) < 2:
            continue
        usual = Counter(_dose_number(i) for i in admins[1:]).most_common(1)[0][0]
        first = admins[0]
        if _dose_number(first) != usual and all(i.d > first.d for i in admins[1:]):
            loading[drug] = first.d
            if not any(i.fields["event"] == "start" and i.d for i in its):
                by_drug[drug] = [replace(i, fields={**i.fields, "event": "start"}) if i is first else i for i in its]
    latest_any = max((d for its in by_drug.values() for i in its for d in [_mention_d(i)] if d), default=None)
    episodes = []
    for drug, its in by_drug.items():
        starts = sorted([i for i in its if i.fields["event"] == "start" and i.d], key=lambda i: (i.precision_rank, i.d))
        if not starts:   # no dated start: "on X since <date>" gives it
            since = [s for i in its if i.fields["event"] == "ongoing" or (i.fields["event"] == "start" and not i.d)
                     for s in [since_start(i)] if s]
            starts = sorted(since, key=lambda i: (i.precision_rank, i.d))
        clusters: list[list[Item]] = []
        for s in starts:
            w = 120 if s.precision_rank >= 3 else 45
            hit = next((c for c in clusters if _days(s.d, c[0].d) <= w), None)
            (hit.append(s) if hit else clusters.append([s]))
        for st in structured_starts.get(drug, []) if structured_starts else []:
            hit = next((c for c in clusters if _days(st, c[0].d) <= 45), None)
            synth = Item("structured", "treatment", "structured", st.isoformat(), None,
                         {"event": "start", "maintenance_therapy": drug}, st.isoformat(), "1")
            if hit:
                hit.insert(0, synth)
            else:
                clusters.append([synth])
        eps = sorted(({"drug": drug, "start": c[0], "starts": list(c), "stops": [], "ongoing": [], "merged": 0}
                      for c in clusters), key=lambda e: e["start"].d)
        stop_dates = sorted(i.d for i in its if i.fields["event"] == "stop" and i.d)
        mentions = sorted({d for i in its for d in [_mention_d(i)] if d})
        merged: list[dict] = []
        for e in eps:
            if merged:
                a, b = merged[-1]["start"].d, e["start"].d
                inside = [m for m in mentions if a < m < b]
                biggest = max(((y - x).days for x, y in zip([a, *inside], [*inside, b], strict=True)), default=0)
                if not any(a <= s < b for s in stop_dates) and biggest <= MAX_COURSE_GAP_DAYS:
                    merged[-1]["starts"] += e["starts"]
                    merged[-1]["merged"] += 1
                    continue
            merged.append(e)
        eps = merged
        if not eps:
            first = min((i for i in its if (i.d or (i.doc_date and date.fromisoformat(i.doc_date)))),
                        key=lambda i: i.d or date.fromisoformat(i.doc_date), default=None)
            if first is None:
                continue
            eps = [{"drug": drug, "start": None, "starts": [], "stops": [], "ongoing": [], "anchor_doc": first,
                    "merged": 0}]
        for i in its:
            ev = i.fields["event"]
            ref = i.d if ev == "stop" else (date.fromisoformat(i.doc_date) if i.doc_date else None)
            if ev == "start" or ref is None:
                continue
            prior = [e for e in eps if e["start"] is None or e["start"].d <= ref]
            if not prior:
                continue
            (prior[-1]["stops"] if ev == "stop" else prior[-1]["ongoing"]).append(i)
        episodes += eps
    out = []
    for e in episodes:
        notes, info = [], []
        start = e["start"]
        stops = sorted(e["stops"], key=lambda i: (i.precision_rank, i.d))
        stop = stops[0] if stops else None
        if stops and len({s.event_date for s in stops if s.precision_rank == stop.precision_rank}) > 1:
            notes.append("stop dates disagree")
        if e["merged"]:
            info.append(f"{e['merged'] + 1} start mentions (repeat doses) merged into one course")
        if e["drug"] in unnamed_only:
            notes.append("none of the quoted passages names this drug: check that it is the right drug")
        if left_out[e["drug"]]:
            info.append(f"{left_out[e['drug']]} mention(s) left out: the quoted passage does not name the drug")
        if infusion_ends[e["drug"]]:
            info.append(f"{infusion_ends[e['drug']]} 'stop' mention(s) read as the end of that day's infusion, not of "
                        "the course")
        first_rec = min((i.d for i in by_drug.get(e["drug"], []) if i.group == ADMIN_GROUP and i.d), default=None)
        if start is not None and first_rec and (start.d - first_rec).days > 7:
            notes.append(f"an administration of this drug is recorded on {first_rec.isoformat()}, before the proposed "
                         "start date: the course began earlier")
        if start is not None and loading.get(e["drug"]) == start.d:
            info.append("start: the first recorded administration, at a dose different from the later ones (the "
                        "loading dose)")
        elif start is not None and start.doc_id != "structured" and _doc_d(start) == start.d \
                and not says_first_dose(start.quote):
            notes.append("the start date is the date of the first administration found in these notes, and that "
                         "note does not say the drug was being started: the course may have begun earlier")
        # still on therapy: the most recent note that mentions the course decides
        pos = [d for i in e["starts"] + e["ongoing"] for d in [_mention_d(i)] if d]
        neg = [d for i in e["stops"] for d in [_doc_d(i) or i.d] if d]
        last_pos, last_neg = max(pos, default=None), max(neg, default=None)
        if last_neg and (last_pos is None or last_neg >= last_pos):
            still = "0"
        elif last_pos:
            still = "1"
            if stop:
                notes.append(f"a stop is documented ({stop.event_date}) but later notes ({last_pos}) say it continues: "
                             "the most recent notes were followed")
            if latest_any and (latest_any - last_pos).days > NOT_RECENT_DAYS:
                still = "0"
                notes.append(f"not mentioned since {last_pos} although later notes discuss other treatment: proposed as "
                             "stopped (stop date unknown)")
        else:
            still = None
        reason, rconf = _most_common([s.fields.get("reason_for_stopping_tx") for s in stops])
        if rconf:
            notes.append("conflicting stop reasons")
        if start is None:
            notes.append("no start date found (only ongoing/stop mentions)")
        detail, _ = _most_common([s.fields.get("reason_for_stopping_therap") for s in stops])
        other_stop, _ = _most_common([s.fields.get("other_stopping") for s in stops])
        other_name, _ = _most_common([i.fields.get("other_maintenance_tx") for i in e["starts"] + e["ongoing"] + e["stops"]])
        anchor = start.event_date if start else f"nostart:{e['drug']}:{e.get('anchor_doc').doc_id if e.get('anchor_doc') else ''}"
        stopped = still == "0" and stop is not None
        out.append({"anchor": f"{e['drug']}|{anchor}", "drug": e["drug"], "start_date": start.d if start else None,
                    "stop_date": stop.d if stopped else None, "values": {
                        "maintenance_therapy": e["drug"],
                        "other_maintenance_tx": other_name if e["drug"] == "15" else None,
                        "date_starting_maint": start.event_date if start else None,
                        "date_maintenance_estimate": (start.estimate if start else None) or None,
                        "still_on_therapy": still, "date_stopping_maint": stop.event_date if stopped else None,
                        "date_stopping_estimate": stop.estimate if stopped else None,
                        "reason_for_stopping_tx": reason if stopped else None,
                        "other_stopping": other_stop if stopped and reason == "7" else None,
                        "reason_for_stopping_therap": detail if stopped else None},
                    "evidence": list(dict.fromkeys(x for i in e["starts"] + e["stops"] + e["ongoing"]
                                                   for x in i.evidence_ids)),
                    "conflict": "; ".join(notes), "info": info})
    out.sort(key=lambda e: (e["start_date"] or date.max))
    for a in out:
        for b in out:
            if a is b or not (a["start_date"] and b["start_date"]) or b["start_date"] <= a["start_date"]:
                continue
            if a["stop_date"] is None or a["stop_date"] > b["start_date"]:
                overlap = (a["stop_date"] or date.max) - b["start_date"]
                if a["stop_date"] is None and a["values"]["still_on_therapy"] == "0":
                    continue   # stopped on an unknown date: no overlap can be shown
                if a["stop_date"] is None or overlap.days > 30:
                    a["conflict"] = "; ".join(x for x in [a["conflict"], f"overlaps with drug {b['drug']}"] if x)
    return out


# ------------------------------------------------------------------------------------------
# antibodies, diagnosis
# ------------------------------------------------------------------------------------------
TITER_RX = re.compile(r"1\s*:\s*([\d,]+)")


def titer_code(choices: dict, text: str | None) -> str | None:
    """'1:100' → the titer choice whose label is 1:100 (or '1:10000 or greater' for any higher titer)."""
    m = TITER_RX.search(text or "")
    if not m:
        return None
    n = int(m.group(1).replace(",", ""))
    for code, label in choices.items():
        lm = TITER_RX.search(label)
        if lm and (int(lm.group(1).replace(",", "")) == n or ("greater" in label.lower()
                                                              and n >= int(lm.group(1).replace(",", "")))):
            return code
    return None


def reconcile_antibodies(items: list[Item], dic: Dictionary | None = None) -> list[dict]:
    """One instance per antibody and test date. A result with no test date joins the earliest dated test of the
    same antibody with the same result, else it becomes an undated instance (16.11.26: an undated positive AQP4 was
    dropped). AQP4 is serum only, so a CSF AQP4 result is left out."""
    dic = dic or load_dictionary(default_dictionary_path())
    ab = [i for i in items if i.group in ("antibody", "labs_table") and not i.negated and not i.hypothetical
          and i.fields.get("type_test") in ("aqp4", "mog")
          and not (i.fields.get("type_test") == "aqp4" and i.fields.get("specimen") == "2")]
    clusters: list[list[Item]] = []
    for it in sorted([i for i in ab if i.d], key=lambda i: (i.precision_rank, i.d)):
        hit = next((c for c in clusters if c[0].fields["type_test"] == it.fields["type_test"]
                    and _days(c[0].d, it.d) <= (60 if it.precision_rank >= 2 else 14)), None)
        (hit.append(it) if hit else clusters.append([it]))
    undated: dict[str, list[Item]] = defaultdict(list)
    for it in (i for i in ab if not i.d):
        same = sorted([c for c in clusters if c[0].fields["type_test"] == it.fields["type_test"]
                       and _most_common([x.fields.get("result") for x in c])[0] == it.fields.get("result")],
                      key=lambda c: c[0].d)
        if same and it.fields.get("result") not in (None, NE):
            same[0].append(it)
        else:
            undated[it.fields["type_test"]].append(it)
    groups = [(c, False) for c in clusters] + [(c, True) for c in undated.values()]
    out = []
    for c, no_date in sorted(groups, key=lambda g: (g[1], g[0][0].d or date.max, g[0][0].fields["type_test"])):
        t = c[0].fields["type_test"]
        res, conf = _most_common([i.fields.get("result") for i in c])
        method, _ = _most_common([i.fields.get("assay_method") for i in c])
        lab, _ = _most_common([i.fields.get("lab") for i in c])
        lab_other, _ = _most_common([i.fields.get("lab_other") for i in c])
        titer_txt, _ = _most_common([i.fields.get("titer") for i in c])
        tab = {str(i.fields["titer"]).replace(" ", "") for i in c if i.group == "labs_table" and i.fields.get("titer")}
        txt = {m.group(0).replace(" ", "") for i in c if i.group != "labs_table"
               for m in [TITER_RX.search(str(i.fields.get("titer") or ""))] if m}
        titer_differs = bool(tab and txt and not (tab & txt))
        if tab:
            titer_txt = sorted(tab)[0]
        specimen, _ = _most_common([i.fields.get("specimen") for i in c])
        when = None if no_date else c[0].event_date
        vals = {"type_test": t}
        if t == "aqp4":
            vals |= {"aqp4_assay_date": when, "aqp4_assay_status": {"positive": "1", "negative": "2"}.get(res),
                     "aqp4_assay_type": method, "aqp4_test_location": lab,
                     "aqp4_test_location_other": lab_other if lab == "5" else None,
                     "aqp4_assay_titer": titer_code(dic["aqp4_assay_titer"].choices, titer_txt)
                     if "aqp4_assay_titer" in dic else None}
        else:
            vals |= {"mog_assay_date": when, "date_mog_estimate": None if no_date else c[0].estimate,
                     "mog_test_positive": {"positive": "1", "negative": "0"}.get(res), "mog_assay_specimen": specimen,
                     "mog_assay_location": lab, "mog_assay_location_other": lab_other if lab == "5" else None,
                     "titer": titer_code(dic["titer"].choices, titer_txt) if "titer" in dic else None}
        notes = ["conflicting results"] if conf else []
        if titer_differs:
            notes.append(f"titre: the lab table says {', '.join(sorted(tab))}, the notes read by the model say "
                         f"{', '.join(sorted(txt))}: the table's value is proposed")
        if no_date:
            notes.append("no test date found in the notes: enter it if known")
        out.append({"anchor": f"{t}|{when or 'undated'}", "date": None if no_date else c[0].d,
                    "values": {k: v for k, v in vals.items() if v not in (None, NE, "")},
                    "evidence": [e for i in c for e in i.evidence_ids], "conflict": "; ".join(notes)})
    return out


PRIOR_DX_RX = re.compile(r"(?i)\b(initial(?:ly)?|original(?:ly)?|previous(?:ly)?|prior|first present|"
                         r"at (?:the time of )?(?:initial |first )?presentation|was (?:thought|felt|believed|presumed)|"
                         r"misdiagnos|differential|\bddx\b|labell?ed|treated (?:as|for) (?:MS|multiple sclerosis))")


def reconcile_diagnosis(items: list[Item]) -> dict:
    dx = [i for i in items if i.group == "diagnosis" and not i.negated and not i.hypothetical]
    vals, ev, notes = {}, [], []
    dated = sorted([i for i in dx if i.fields.get("date_nmo_diagnosis") and i.d], key=lambda i: (i.precision_rank, i.d))
    if dated:
        best_rank = dated[0].precision_rank
        top = [i for i in dated if i.precision_rank == best_rank]
        choice, _ = _most_common([i.event_date for i in top])
        vals["date_nmo_diagnosis"], vals["date_nmo_estimate"] = choice, str(best_rank) if best_rank <= 4 else None
        spread = max(_days(i.d, date.fromisoformat(choice)) for i in dated if i.precision_rank <= 2) if any(
            i.precision_rank <= 2 for i in dated) else 0
        if spread > 31:
            notes.append("diagnosis dates disagree by >1 month")
        ev += [e for i in dated for e in i.evidence_ids]
    # otherdx: a diagnosis other than NMOSD given or considered at the initial presentation (16.11.26). A
    # considered one counts, so hypothetical mentions are kept; a mention counts only from a note written before
    # the diagnosis date or one that says it was the initial/previous diagnosis.
    dx_day = date.fromisoformat(vals["date_nmo_diagnosis"]) if vals.get("date_nmo_diagnosis") else None
    prior = [i for i in items if i.group == "diagnosis" and not i.negated
             and i.fields.get("otherdx") not in (None, NE, "UNK", "")
             and (PRIOR_DX_RX.search(i.quote or "") or (dx_day and i.doc_date and i.doc_date[:10] < dx_day.isoformat()))]
    counts = Counter(i.fields["otherdx"] for i in prior)
    if counts:
        specific = [c for c, _ in counts.most_common() if c in ("1", "2", "3")]
        od = specific[0] if specific else counts.most_common(1)[0][0]
        vals["otherdx"] = od
        if len(counts) > 1:
            notes.append("prior diagnoses differ (" + ", ".join(f"{c}: {n}" for c, n in counts.most_common()) + ")")
        ev += [e for i in prior for e in i.evidence_ids]
        if od == "4":
            dxo, _ = _most_common([i.fields.get("dx_other") for i in prior if i.fields.get("otherdx") == "4"])
            if dxo:
                vals["dx_other"] = dxo
    date_ev = [i.field_evidence["date_nmo_diagnosis"] for i in dated if "date_nmo_diagnosis" in i.field_evidence]
    return {"values": vals, "evidence": list(dict.fromkeys(ev)), "conflict": "; ".join(notes), "date_evidence": date_ev}


# ------------------------------------------------------------------------------------------
# diagnosis check: does the patient have NMOSD, with enough follow-up?
# ------------------------------------------------------------------------------------------
@dataclass
class DocMeta:
    date: date | None
    relevance: str | None
    description: str | None = None


def _as_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def _months(a: date, b: date) -> float:
    return round((b - a).days / 30.4375, 1)


def _shift_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, 28))


def reconcile_eligibility(items: list[Item], dx_values: dict, antibodies: list[dict], docs: dict[str, DocMeta],
                          repeats: dict[str, list[tuple[int, int, str]]], rules: dict) -> dict:
    """Propose the diagnosis-check answers for one patient.

    docs: this patient's documents; repeats: first_doc_id -> [(first_start, first_end, repeating doc_id)].
    Returns dx_category / followup_12m proposals with their evidence and conflict notes, plus a summary for the
    review screen (de-identified dates and counts only).
    """
    primary = set(rules.get("primary_relevance", ["clinical_note"]))
    window = int(rules.get("recent_window_months", 24))
    min_fu = float(rules.get("min_followup_months", 12))
    end_classes = set(rules.get("followup_end_relevance", ["clinical_note"]))
    # NMOSD and the diagnoses that REPLACE it compete; anything else ("other") may co-exist with NMOSD
    # (patients often carry several diagnoses), so it never outranks them.
    core = {"nmosd", *rules.get("alternatives", ["mogad", "ms", "ae", "sc_infarct"])}

    mentions, ruled_out = [], []
    for i in items:
        cat = i.fields.get("dx_category")
        if i.group != "diagnosis" or cat in (None, "", NE, "UNK") or "dx_category" not in i.field_evidence:
            continue
        copies = {d for s, e, d in repeats.get(i.doc_id, []) if i.char_start is not None and s <= i.char_start < e}
        stating = ({i.doc_id} | copies) & set(docs)
        dated = {d: docs[d].date for d in stating if docs[d].date}
        m = {"cat": cat, "hyp": i.hypothetical, "docs": stating,
             "primary": {d for d in stating if docs[d].relevance in primary},
             "dates": dated, "own_date": docs[i.doc_id].date if i.doc_id in docs else None,
             "evidence": i.field_evidence["dx_category"]}
        (ruled_out if i.negated else mentions).append(m)   # "not NMOSD" is a statement too

    stated = [m for m in mentions if not m["hyp"]]
    pool, basis = [m for m in stated if m["primary"]], "clinic letters and hospital notes"
    if not pool:
        pool, basis = stated, ("other documents only" if stated else "none")
    notes: list[str] = []
    per_cat: dict[str, dict] = {}
    for m in pool:
        docs_used = m["primary"] if basis.startswith("clinic") else m["docs"]
        c = per_cat.setdefault(m["cat"], {"docs": set(), "dates": [], "own": []})
        c["docs"] |= docs_used
        c["dates"] += [m["dates"][d] for d in docs_used if d in m["dates"]]
        if m["own_date"]:
            c["own"].append(m["own_date"])
    proposal = "unclear"
    last = {k: max(c["dates"], default=date.min) for k, c in per_cat.items()}
    anchor = max((d for c in per_cat.values() for d in c["dates"]), default=None)
    coexisting: list[str] = []
    if per_cat:
        # NMOSD-spectrum statements first; then the most recent statement wins; on the same date, text newly
        # written in that note beats a copied-forward paragraph; then the number of documents.
        proposal = max(per_cat, key=lambda k: (k in core, last[k], max(per_cat[k]["own"], default=date.min),
                                               len(per_cat[k]["docs"])))
        if anchor:
            lo = _shift_months(anchor, window)
            recent = {k for k, c in per_cat.items() if k != proposal and any(d >= lo for d in c["dates"])}
            rivals = sorted(recent & core)
            if rivals:
                notes.append(f"recent notes also state {', '.join(rivals)}")
            newer = sorted(k for k in recent - core if proposal in core and last[k] > last[proposal])
            if newer:
                notes.append(f"the most recent notes state another diagnosis ({', '.join(newer)}): check whether it "
                             "co-exists with or replaces " + proposal)
            coexisting = sorted(recent - core - set(newer))
            if any(m["cat"] == proposal and any(d >= lo for d in m["dates"].values()) for m in ruled_out):
                notes.append(f"a recent note says the patient does not have {proposal}")
        if proposal not in core:
            notes.append("no NMOSD-spectrum diagnosis stated, only other diagnoses")
        if basis == "other documents only":
            notes.append("no clinic letter or hospital note states the diagnosis")
    elif mentions:
        notes.append("only possible/suspected diagnoses found")
    else:
        notes.append("no diagnosis statement found in the notes")

    ab = {"aqp4": Counter(), "mog": Counter()}
    for a in antibodies:
        v = a["values"]
        if v.get("type_test") == "aqp4" and v.get("aqp4_assay_status") in ("1", "2"):
            ab["aqp4"]["positive" if v["aqp4_assay_status"] == "1" else "negative"] += 1
        elif v.get("type_test") == "mog" and v.get("mog_test_positive") in ("1", "0"):
            ab["mog"]["positive" if v["mog_test_positive"] == "1" else "negative"] += 1
    if ab["aqp4"]["positive"] and proposal != "nmosd":
        notes.append("AQP4-IgG positive result found (usually diagnostic of NMOSD)")
    if proposal == "nmosd" and not ab["aqp4"]["positive"]:
        if ab["mog"]["positive"]:
            notes.append("MOG-IgG positive and no positive AQP4-IgG result: check for MOGAD")
        elif ab["aqp4"]["negative"]:
            notes.append("AQP4-IgG negative only (seronegative): confirm the diagnostic criteria are met")
        else:
            notes.append("no AQP4-IgG result found")

    # the first note stating NMOSD (a copied-forward statement counts from its first note)
    nm = [d for m in stated if m["cat"] == "nmosd" for d in m["dates"].values()]
    first_nmosd = {"date": min(nm).isoformat(), "evidence": list(dict.fromkeys(
        m["evidence"] for m in stated if m["cat"] == "nmosd" and min(nm) in m["dates"].values()))} if nm else None
    # follow-up: diagnosis date -> the last follow-up contact
    fu_notes: list[str] = []
    start, start_src = _as_date(dx_values.get("date_nmo_diagnosis")), "diagnosis date"
    if start is None:
        start, start_src = (min(nm), "first note stating NMOSD") if nm else (None, "")
    if start is None:
        clin = [d.date for d in docs.values() if d.relevance in primary and d.date]
        start, start_src = (min(clin), "first clinical note") if clin else (None, "")
    if start_src != "diagnosis date":
        fu_notes.append("no diagnosis date: follow-up counted from the " + (start_src or "(nothing)"))
    # Contacts: clinical notes (incl. infusion notes), patient messages and outreach notes, and the infusion /
    # appointment dates those messages give (part of the patient's treatment, confirmed 10.11.26).
    msg_rx = re.compile(rules["followup_message_descriptions"], re.I) if rules.get("followup_message_descriptions") \
        else None
    contacts: list[tuple[date, str, str | None]] = []
    for d in docs.values():
        if not d.date:
            continue
        if d.relevance in primary and d.relevance in end_classes:
            contacts.append((d.date, "clinical note", None))
        elif d.relevance in end_classes or (msg_rx and d.description and msg_rx.search(d.description)):
            contacts.append((d.date, "patient message", None))
    for i in items:
        v = _as_date(i.fields.get("visit_date")) if i.group == "followup" else None
        sent = _as_date(i.doc_date)
        if v is None or i.negated or i.hypothetical or (sent and (v - sent).days > 548):
            continue   # cancelled, tentative, or implausibly far ahead of the message
        kind = i.fields.get("visit_type") if i.fields.get("visit_type") not in (None, NE) else "visit"
        contacts.append((v, f"{kind} date given in a patient message", i.field_evidence.get("visit_date")))
    end, end_src, end_ev = max(contacts, key=lambda c: c[0]) if contacts else (None, "", None)
    months = _months(start, end) if start and end and end >= start else None
    followup = None if months is None else ("1" if months >= min_fu else "0")
    # Clearly short or clearly long follow-up needs no flag; within the margin either side it is borderline.
    margin = float(rules.get("borderline_followup_margin_months", 3))
    if months is None:
        fu_notes.append("follow-up could not be computed")
    elif abs(months - min_fu) < margin:
        side = "under" if months < min_fu else "over"
        fu_notes.append(f"just {side} the {min_fu:g}-month threshold ({months:g} months): check the dates yourself")
    latest_doc = max((d.date for d in docs.values() if d.date), default=None)
    if followup == "1" and end_ev and latest_doc and end > latest_doc:
        before = max((c[0] for c in contacts if c[0] <= latest_doc), default=None)
        if before is None or before < start or _months(start, before) < min_fu:
            fu_notes.append(f"{min_fu:g} months are reached only through a date given in a patient message, after "
                            "the last note: check it took place")

    # every statement is evidence (newest first): possible/suspected ones, other documents and "not X" too
    ordered = sorted(mentions + ruled_out, key=lambda m: max(m["dates"].values(), default=date.min), reverse=True)
    summary = {
        "basis": basis,
        "statements": sorted(({"category": k, "documents": len(c["docs"]),
                               "first": min(c["dates"]).isoformat() if c["dates"] else None,
                               "last": max(c["dates"]).isoformat() if c["dates"] else None}
                              for k, c in per_cat.items()), key=lambda s: s["last"] or "", reverse=True),
        "coexisting": coexisting,
        "ruled_out": sorted({m["cat"] for m in ruled_out}),
        "possible_only": sorted({m["cat"] for m in mentions if m["hyp"]} - set(per_cat)),
        "recent_window_months": window,
        "antibodies": {k: dict(v) for k, v in ab.items()},
        "followup": {"from": start.isoformat() if start else None, "from_source": start_src,
                     "to": end.isoformat() if end else None, "to_source": end_src, "months": months,
                     "min_months": min_fu},
    }
    return {"dx_category": proposal, "dx_evidence": list(dict.fromkeys(m["evidence"] for m in ordered)),
            "dx_conflict": "; ".join(notes), "followup_12m": followup, "followup_conflict": "; ".join(fu_notes),
            "followup_evidence": [end_ev] if end_ev else [], "summary": summary, "first_nmosd": first_nmosd}


# ------------------------------------------------------------------------------------------
# structured demographics, checked against the notes
# ------------------------------------------------------------------------------------------
def reconcile_structured(items: list[Item], struct: dict[str, str], source_notes: dict[str, str] | None = None,
                         structured_only: frozenset[str] = STRUCTURED_ONLY) -> dict[str, dict]:
    """{field: {value, evidence, conflict}} for the structured Demographics fields.

    The structured code stays the proposal. `structured_only` fields (since 16.11.26 all four, see
    `note_check` in config/fields/pilot_fields.yaml) use the structured value alone. For the others, each field's
    evidence is the note mentions of that field only, and only those whose quote has the field's own wording (its
    prefilter keywords: "woman", "Race:", ...), so a model answer quoting a medication list is not evidence; when
    the structured value is missing or unmapped, the most frequent note value is proposed instead.
    `source_notes` (from `structured_values`) say when a value came from a fallback column, or why there is none;
    they are flagged for the reviewer.
    """
    demo = [i for i in items if i.group == "demographics" and not i.negated and not i.hypothetical]
    source_notes = source_notes or {}
    out = {}
    for f in STRUCTURED_SOURCES:
        sv, notes = struct.get(f), []
        src_note = source_notes.get(f) or ("" if sv else f"{f}: no usable structured value")
        if f in structured_only:
            out[f] = {"value": sv, "evidence": [], "conflict": src_note}
            continue
        if sv is not None and src_note:
            notes.append(src_note)
        rx = field_keyword_rx("demographics", f)
        said = [i for i in demo if i.fields.get(f) not in (None, "", NE, "UNK") and f in i.field_evidence
                and (rx is None or i.quote is None or rx.search(i.quote))]
        counts = Counter(str(i.fields[f]) for i in said)
        if sv is None:
            value = counts.most_common(1)[0][0] if counts else None
            notes.append(src_note + ("; proposed from the notes" if value else ""))
            if len(counts) > 1:
                notes.append(f"{f}: notes disagree ({_tally(counts)})")
        else:
            value = sv
            other = Counter({c: n for c, n in counts.items() if c != sv})
            if other:
                notes.append(f"{f}: notes say {_tally(other)}, structured data says {sv}")
        out[f] = {"value": value, "evidence": list(dict.fromkeys(i.field_evidence[f] for i in said)),
                  "conflict": "; ".join(notes)}
    return out


def _tally(counts: Counter) -> str:
    return ", ".join(f"{c} ({n} mention{'s' if n > 1 else ''})" for c, n in counts.most_common())


# ------------------------------------------------------------------------------------------
# comorbidities
# ------------------------------------------------------------------------------------------
def reconcile_comorbidities(items: list[Item]) -> dict:
    """The REDCap `comorbidities` checkbox (and `autoimmune` when an autoimmune disease is ticked) from the
    conditions the notes list, and every named condition for the reviewer: its usual category (also
    "no_category" for conditions outside the REDCap categories), number of notes and latest note date.

    A condition counts under the category most notes give it; a condition given different categories is noted.
    9 (None) is proposed only when a note says there is no medical history and no condition is found.
    """
    named: dict[str, dict] = {}
    none_said, evidence = False, []
    for i in items:
        if i.group != "comorbidity" or i.negated or i.hypothetical:
            continue
        cat = str(i.fields.get("comorbidities") or "")
        cat = cat if cat.isdigit() or cat in (NO_CATEGORY, NMOSD_RELATED) else ""
        name = re.sub(r"\s+", " ", str(i.fields.get("condition") or "")).strip(" .;,:-")
        if cat == "9":
            none_said = True
            evidence += i.evidence_ids
            continue
        if not name or not cat:
            continue
        evidence += i.evidence_ids
        c = named.setdefault(name.lower(), {"names": Counter(), "cats": Counter(), "auto": Counter(), "docs": set(),
                                            "last": None})
        c["names"][name] += 1
        c["cats"][cat] += 1
        auto = str(i.fields.get("autoimmune") or "")
        if cat == "1" and auto.isdigit():
            c["auto"][auto] += 1
        c["docs"].add(i.doc_id)
        if i.doc_date and (c["last"] is None or i.doc_date[:10] > c["last"]):
            c["last"] = i.doc_date[:10]
    conditions, notes = [], []
    for c in named.values():
        name, cat = c["names"].most_common(1)[0][0], c["cats"].most_common(1)[0][0]
        if len(c["cats"]) > 1:
            notes.append(f"{name}: notes give different categories ({_tally(c['cats'])})")
        conditions.append({"name": name, "category": cat,
                           "autoimmune": c["auto"].most_common(1)[0][0] if cat == "1" and c["auto"] else None,
                           "documents": len(c["docs"]), "last": c["last"]})
    conditions.sort(key=lambda c: (not c["category"].isdigit(), c["category"] == NMOSD_RELATED,
                                   int(c["category"]) if c["category"].isdigit() else 0, c["name"].lower()))
    cats = sorted({c["category"] for c in conditions if c["category"].isdigit()}, key=int)
    autos = sorted({c["autoimmune"] for c in conditions if c["autoimmune"]}, key=int)
    if none_said and cats:
        notes.append("a note says there is no past medical history, others list conditions")
    values = {"comorbidities": cats or (["9"] if none_said else None), "autoimmune": autos or None}
    return {"values": values, "evidence": list(dict.fromkeys(evidence)), "conflict": "; ".join(notes),
            "conditions": conditions}


def problem_list_check(dx: dict, items: list[Item]) -> dict:
    """Reader against model (18.11.26): the notes state a diagnosis date, but the problem list or visit diagnoses
    (`dx_dates`) give NMOSD from more than a month earlier. The stated date stays the proposal; the reviewer is told."""
    stated = dx["values"].get("date_nmo_diagnosis")
    rows = sorted((i for i in items if i.group == "dx_dates" and i.d), key=lambda i: i.d)
    if not stated or not rows or (date.fromisoformat(stated[:10]) - rows[0].d).days <= 31:
        return dx
    note = (f"the problem list or visit diagnoses give NMOSD from {rows[0].event_date}, earlier than the diagnosis date "
            "the notes state: check which is right")
    return {**dx, "evidence": list(dict.fromkeys(dx["evidence"] + rows[0].evidence_ids)),
            "conflict": "; ".join(x for x in (dx["conflict"], note) if x)}


def problem_list_fallback(dx: dict, items: list[Item], first_nmosd: dict | None) -> dict:
    """No diagnosis date stated in the notes, but the problem list or a visit-diagnosis row names NMOSD with a date
    (the `dx_dates` group, read with text patterns): propose the earliest such date, as an estimate (code 4: the
    problem was entered then, so the diagnosis was on or before it). Only when it is earlier than the first note
    that states NMOSD, which `diagnosis_date_fallback` would otherwise propose."""
    rows = sorted((i for i in items if i.group == "dx_dates" and i.d), key=lambda i: i.d)
    if dx["values"].get("date_nmo_diagnosis") or not rows:
        return dx
    first = rows[0]
    if first_nmosd and first_nmosd["date"] <= first.event_date:
        return dx
    note = ("no diagnosis date in the notes: proposed the earliest date NMOSD is given in the problem list or visit "
            "diagnoses (the diagnosis was on or before it)")
    return {**dx, "values": {**dx["values"], "date_nmo_diagnosis": first.event_date, "date_nmo_estimate": "4"},
            "evidence": list(dict.fromkeys(dx["evidence"] + first.evidence_ids)),
            "conflict": "; ".join(x for x in (dx["conflict"], note) if x)}


def diagnosis_date_fallback(dx: dict, first_nmosd: dict | None) -> dict:
    """No diagnosis date stated in the notes: propose the date of the first note that states NMOSD, as an estimate
    (code 4: the diagnosis was on or before it), with that statement as evidence, and say so."""
    if dx["values"].get("date_nmo_diagnosis") or not first_nmosd:
        return dx
    note = ("no diagnosis date in the notes: proposed the date of the first note that states NMOSD (the diagnosis "
            "was on or before it)")
    return {**dx, "values": {**dx["values"], "date_nmo_diagnosis": first_nmosd["date"], "date_nmo_estimate": "4"},
            "evidence": list(dict.fromkeys(dx["evidence"] + first_nmosd["evidence"])),
            "conflict": "; ".join(x for x in (dx["conflict"], note) if x)}


# ------------------------------------------------------------------------------------------
def _structured_starts(con_d, study_id: str) -> dict[str, list[date]]:
    from nmosd.textproc.dates import parse_datetime

    out = defaultdict(list)
    for r in con_d.execute("SELECT row_json FROM structured_rows WHERE study_id=? AND table_name='Medications'", (study_id,)):
        row = json.loads(r["row_json"])
        name = (row.get("Medication") or "").lower()
        code = next((c for w, c in DRUG_WORDS.items() if w in name), None)
        dt = parse_datetime(row.get("Medication_Date"))
        if code and dt:
            out[code].append(dt.date())
    return {k: sorted(v)[:1] for k, v in out.items()}


def run_reconcile(extract: Path, deid: Path, settings, stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    window = int(settings.get("extraction.relapse_window_days", 30))
    wide = int(settings.get("extraction.relapse_window_days_low_precision", 120))
    con = connect(extract, "extract")
    con_d = connect(deid, readonly=True)
    patients = [dict(r) for r in con_d.execute("SELECT * FROM patients ORDER BY study_id")]
    run_id = "rec-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    rows, summary = [], Counter()
    rules = load_rules("eligibility.yaml")
    dic = load_dictionary(default_dictionary_path())
    docs_by_patient: dict[str, dict[str, DocMeta]] = defaultdict(dict)
    for r in con_d.execute("SELECT doc_id, study_id, report_date, relevance_class, report_description FROM documents"):
        docs_by_patient[r["study_id"]][r["doc_id"]] = DocMeta(_as_date(r["report_date"]), r["relevance_class"],
                                                              r["report_description"])
    data_end = max((m.date for ds in docs_by_patient.values() for m in ds.values() if m.date), default=None)
    repeats: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for r in con.execute("SELECT doc_id, first_doc_id, first_char_start, first_char_end FROM paragraph_repeats"):
        if r["first_char_start"] is not None and r["first_char_end"] is not None:
            repeats[r["first_doc_id"]].append((r["first_char_start"], r["first_char_end"], r["doc_id"]))
    # Hospital names: only from notes that still name hospitals. Mentions found in an earlier run on the original
    # notes are never used once these notes are de-identified with hospital names tokenised.
    try:
        hospital_names = hospital_names_kept(get_meta(con_d, "manifest"))
    except sqlite3.Error:
        hospital_names = False
    for p in patients:
        sid = p["study_id"]
        items = load_items(con, sid)
        if not hospital_names:
            items = [i for i in items if i.group != HOSPITAL_GROUP]
        # code checks: does the passage of each treatment mention name its drug?
        tx_docs = {i.doc_id for i in items if i.group in ("treatment", "treatment_detail")}
        texts = {r["doc_id"]: r["text"] for r in con_d.execute("SELECT doc_id, text FROM documents WHERE study_id=?",
                                                               (sid,)) if r["doc_id"] in tx_docs}
        summary["unnamed_drug"] += mark_unnamed(items, texts)

        def add(instrument, key, values, evidence, conflict, anchor=None, sid=sid):
            for f, v in values.items():
                val = json.dumps(v) if isinstance(v, list) else v
                pid = hashlib.sha256(f"{run_id}|{sid}|{instrument}|{key}|{f}".encode()).hexdigest()[:16]
                rows.append((pid, sid, instrument, key, f, val, None, json.dumps(evidence), int(bool(conflict)),
                             conflict or None, run_id, anchor))

        dx = reconcile_diagnosis(items)
        antibodies = reconcile_antibodies(items, dic)
        el = reconcile_eligibility(items, dx["values"], antibodies, docs_by_patient[sid], repeats, rules)
        add(GATE, GATE, {"dx_category": el["dx_category"]}, el["dx_evidence"], el["dx_conflict"])
        add(GATE, GATE, {"followup_12m": el["followup_12m"]}, list(dict.fromkeys(dx["date_evidence"]
                                                                                    + el["followup_evidence"])),
            el["followup_conflict"])
        add(GATE, GATE, {"summary": json.dumps(el["summary"])}, [], "")
        summary[f"dx:{el['dx_category']}"] += 1
        summary["followup_short"] += el["followup_12m"] == "0"
        sv = structured_values(p)
        # Structured fields (sex, race, ...): value from the patients table (the Demographics table); for fields
        # still checked against the notes, evidence from that field's own note mentions only; conflict note per field.
        struct = {f: code for f, (code, _) in sv.items() if code is not None}
        for f, c in reconcile_structured(items, struct, {f: note for f, (_, note) in sv.items() if note}).items():
            add("demographics", "demographics", {f: c["value"]}, c["evidence"], c["conflict"])
        # after the diagnosis check, which counts follow-up from a stated diagnosis date only
        dx = problem_list_check(dx, items)
        dx = problem_list_fallback(dx, items, el["first_nmosd"])
        dx = diagnosis_date_fallback(dx, el["first_nmosd"])
        add("demographics", "demographics", dx["values"], dx["evidence"], dx["conflict"])
        cm = reconcile_comorbidities(items)
        add("demographics", "demographics", cm["values"], cm["evidence"], cm["conflict"])
        # every named condition, for the reviewer (not a REDCap field, never exported)
        add("demographics", "demographics", {"conditions": json.dumps(cm["conditions"])}, [], "")
        def add_inst(instrument: str, inst: dict, single: bool = False, sid=sid) -> None:
            """An instance with per-field evidence (stage2_instruments)."""
            key = instrument if single else instance_key(sid, instrument, inst["anchor"])
            anchor = inst["date"].isoformat() if inst.get("date") else (None if single else inst["anchor"])
            for f, v in inst["values"].items():
                if v in (None, "", []):
                    continue
                add(instrument, key, {f: v}, inst["evidence"].get(f, []), inst["conflict"], anchor)
            if inst.get("info"):   # how values were worked out: shown to the reviewer, not a conflict, never exported
                add(instrument, key, {INFO_FIELD: inst["info"]}, [], "", anchor)
            summary[instrument] += 0 if single else 1

        # attacks, admissions, treatment courses and everything derived from them (16.11.26: all 303 fields)
        living = struct.get("living_deceased")
        dx_day = s2._d(dx["values"].get("date_nmo_diagnosis"))
        rel = s2.merge_relapse_details(reconcile_relapses(items, window, wide), items, dic, window, wide)
        nr = s2.route_admissions(rel, items, dic)
        courses = reconcile_treatments(items, _structured_starts(con_d, sid))
        labs = s2.lab_results(items, docs_by_patient[sid])
        s2.derive_relapse(rel, courses, labs)
        s2.derive_hospitalizations(nr, courses)
        edss = s2.reconcile_edss(items, dic, courses, con_d, sid)
        for inst in rel:
            add_inst("relapse", s2.finish(inst, dic))
        for inst in nr:
            add_inst("non_relapse_hospitalization", s2.finish(inst, dic))
        for inst in s2.merge_treatment_details(courses, items, dic, rel):
            add_inst("treatment", inst)
        for inst in s2.reconcile_imaging(items, dic, docs_by_patient[sid], rel):
            add_inst("imaging", inst)
        cd, ig = s2.reconcile_labs(labs, dic)
        for inst in cd:
            add_inst("cd1920_counts", inst)
        for inst in ig:
            add_inst("immunoglobulin_levels", inst)
        for inst in edss:
            add_inst("edss", inst)
        for inst in s2.reconcile_ivig(items, dic):
            add_inst("ivig_for_hypogam", inst)
        for inst in s2.reconcile_infections(items, dic):
            add_inst("infections_requiring_antibiotics", inst)
        for ep in antibodies:
            add_inst("antibody_lab_data", {"anchor": ep["anchor"], "date": ep.get("date"), "values": ep["values"],
                                           "evidence": {f: ep["evidence"] for f in ep["values"]},
                                           "conflict": ep["conflict"]})
        last_contact = (el["summary"].get("followup") or {}).get("to")
        add_inst("demographics", s2.demographics_extra(
            items, dic, living=living, dx_date=dx_day, relapse_insts=rel, nr_insts=nr, courses=courses,
            antibodies=antibodies, edss=edss, last_contact=last_contact, gate_dx=el["dx_category"],
            comorbidities=cm["values"].get("comorbidities")), single=True)
        last_clinical = max((m.date for m in docs_by_patient[sid].values() if m.date and m.relevance == "clinical_note"),
                            default=None)
        lfu = s2.last_follow_up(dic, last_contact, last_clinical, data_end, cm["conditions"])
        if lfu:
            add_inst("last_follow_up", lfu)
    con.executemany("INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    conflicts = len({(r[1], r[2], r[3]) for r in rows if r[8]})
    con.close()
    con_d.close()
    dx_counts = ", ".join(f"{k[3:]} {v}" for k, v in sorted(summary.items()) if k.startswith("dx:"))
    counts = ", ".join(f"{summary[k]} {k}" for k in ("relapse", "non_relapse_hospitalization", "imaging", "treatment",
                                                     "cd1920_counts", "immunoglobulin_levels", "edss", "ivig_for_hypogam",
                                                     "infections_requiring_antibiotics", "antibody_lab_data",
                                                     "last_follow_up"))
    print(f"Reconciled {len(patients)} patients: {counts} instances; {conflicts} instances flagged for review. "
          f"run {run_id}", file=stdout)
    print(f"Code checks: {summary['unnamed_drug']} treatment mention(s) whose passage does not name the drug.",
          file=stdout)
    print(f"Diagnosis check proposals: {dx_counts or 'none'}; under {rules.get('min_followup_months', 12)} months of "
          f"follow-up: {summary['followup_short']}. Confirm each in the review app before anything else.", file=stdout)
    return 0
