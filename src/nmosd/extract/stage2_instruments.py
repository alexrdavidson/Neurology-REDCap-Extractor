"""Stage 2 for the instruments added for full codebook coverage (16.11.26), and the derived fields.

Every function takes one patient's stage-1 items (`stage2_reconcile.Item`) and returns instances as
{"anchor": str, "date": date | None, "values": {field: value}, "evidence": {field: [evidence ids]}, "conflict": str}.
Values use REDCap codes; checkbox values are lists of codes. A field the notes do not answer is left out (the reviewer
sees it empty). Each instance is cleaned with the dictionary's branching logic, so a value REDCap would hide (e.g. the
rehab dates of a patient not sent to rehab) is not proposed.

* relapse details and admissions are merged into the relapse instances of `reconcile_relapses` by date; an admission
  that is not for an attack becomes a non-relapse hospitalization; when the notes name hospitals, the `hospital`
  group's mentions give each stay its hospital (nr_hosp, or nr_hosp_2 for an attack) and the demographics hx_opcare;
* imaging: the three imaging groups' items, merged by study date (MRIs within 14 days are one instance, dated by the
  earliest, as the codebook asks);
* treatment details and reasons are merged into the treatment courses of `reconcile_treatments`;
* labs give the CD19/CD20 and immunoglobulin instances (and the counts before an attack);
* EDSS: one instance per examination note chosen by `nmosd.extract.targets` (treatment start, stop, last follow-up).
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date

from nmosd.extract.targets import TIMING, exam_notes, pick_assessment_notes
from nmosd.redcap.dictionary import Dictionary, branching_row, eval_branching
from nmosd.redcap.schemas import NE, NMOSD_RELATED, NO_CATEGORY
from nmosd.redcap.sources import HOSPITAL_GROUP, OTHER_CODE, PHENOTYPE_TO_LOCATION, map_drug

EMPTY = (None, "", NE, "UNK", [])


def _v(i, f):
    v = i.fields.get(f)
    return None if v in EMPTY else v


def _d(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s and s != NE else None
    except ValueError:
        return None


def _item_date(i, use_doc: bool = True) -> date | None:
    return i.d or (_d(i.doc_date) if use_doc else None)


def new_instance(anchor: str, when: date | None) -> dict:
    """notes: conflicts the reviewer must resolve (flagged); info: how a value was worked out (shown, not flagged)."""
    return {"anchor": anchor, "date": when, "values": {}, "evidence": defaultdict(list), "notes": [], "info": []}


def put(inst: dict, field: str, items: list, dic: Dictionary, label: str | None = None, mode: str = "common",
        transform=None) -> None:
    """Combine the items' answers for one field into the instance: checkbox → union of codes; otherwise the most
    common answer (mode="common"), the first (mode="first", items in priority order) or "any1" (1 if any item says
    1, else 0 if any says 0). Disagreement is noted for the reviewer."""
    vals = [(i, transform(_v(i, field)) if transform else _v(i, field)) for i in items]
    vals = [(i, v) for i, v in vals if v not in EMPTY]
    if not vals:
        return
    target = label or field
    f = dic.get(target)
    if f is not None and f.is_checkbox:
        have = inst["values"].get(target) or []
        codes = sorted({str(c) for _, v in vals for c in (v if isinstance(v, list) else [v])} | {str(c) for c in have},
                       key=lambda c: (not c.isdigit(), int(c) if c.isdigit() else 0, c))
        none = f.none_of_the_above
        if none and none in codes and len(codes) > 1:
            codes = [c for c in codes if c != none]
            inst["notes"].append(f"{target}: some notes say none of these, others list some")
        value = codes
    elif mode == "any1":
        got = {str(v) for _, v in vals}
        value = "1" if "1" in got else sorted(got)[0]
        if len(got) > 1:
            inst["notes"].append(f"{target}: notes disagree")
    elif mode == "first":
        value = vals[0][1]
    else:
        c = Counter(json.dumps(v) for _, v in vals)
        top = c.most_common()
        value = json.loads(top[0][0])
        if len(top) > 1 and (f is None or f.is_coded):
            inst["notes"].append(f"{target}: notes disagree ({', '.join(str(json.loads(k)) for k, _ in top[:3])})")
    inst["values"][target] = value
    for i, _ in vals:
        inst["evidence"][target].append(i.field_evidence.get(field) or (i.evidence_ids[0] if i.evidence_ids else None))


def derive(inst: dict, field: str, value, evidence: list[str] | None = None, note: str | None = None) -> None:
    if value in EMPTY:
        return
    inst["values"][field] = value
    if evidence:
        inst["evidence"][field] += [e for e in evidence if e]
    if note:
        inst["info"].append(note)


def clean_branching(inst: dict, dic: Dictionary, context: dict | None = None) -> None:
    """Drop values REDCap would hide given the instance's other values and `context` (values of the same record
    proposed elsewhere, e.g. living/deceased from the Demographics table). Repeat until stable."""
    for _ in range(4):
        row = branching_row(dic, {**(context or {}), **inst["values"]})
        hidden = [f for f in inst["values"] if f in dic and dic[f].branching and eval_branching(dic[f].branching, row) is False]
        if not hidden:
            return
        for f in hidden:
            inst["values"].pop(f, None)
            inst["evidence"].pop(f, None)


def finish(inst: dict, dic: Dictionary, context: dict | None = None) -> dict:
    clean_branching(inst, dic, context)
    ev = {f: list(dict.fromkeys(e for e in es if e)) for f, es in inst["evidence"].items()}
    return {"anchor": inst["anchor"], "date": inst["date"], "values": inst["values"], "evidence": ev,
            "conflict": "; ".join(dict.fromkeys(inst["notes"])), "info": list(dict.fromkeys(inst.get("info", [])))}


def cluster(items: list, window: int, use_doc: bool = True) -> list[list]:
    """Group items whose dates are within `window` days of the group's first (earliest, most precise) date."""
    dated = sorted([i for i in items if _item_date(i, use_doc)], key=lambda i: (_item_date(i, use_doc), i.precision_rank))
    groups: list[list] = []
    for i in dated:
        d = _item_date(i, use_doc)
        hit = next((g for g in groups if abs((d - _item_date(g[0], use_doc)).days) <= window), None)
        (hit.append(i) if hit else groups.append([i]))
    return groups


def _best_date(items: list) -> tuple[str | None, str | None]:
    """The most precise (then earliest) anchor date of the items, with its estimate code."""
    ds = [(i.precision_rank, i.event_date, i.estimate) for i in items if i.d]
    if not ds:
        return None, None
    _rank, iso, est = min(ds)
    return iso, (est if est and est != NE else None)


# ------------------------------------------------------------------------------------------
# treatment courses: active at / started after a date
# ------------------------------------------------------------------------------------------
def active_on(treatments: list[dict], when: date, slack: int = 0) -> list[dict]:
    out = []
    for t in treatments:
        s, e = t.get("start_date"), t.get("stop_date")
        if s and s <= when and (e is None or e >= when) and not (e is None and t["values"].get("still_on_therapy") == "0"):
            out.append(t)
    return out


def started_between(treatments: list[dict], a: date, b: date) -> list[dict]:
    return [t for t in treatments if t.get("start_date") and a <= t["start_date"] <= b]


def drug_codes(field: str, treatments: list[dict]) -> tuple[list[str], list[str]]:
    """(codes of `field` for these courses, names for its 'Other' text)."""
    codes, others = set(), []
    for t in treatments:
        c = map_drug(field, t["drug"])
        if c is None:
            continue
        codes.add(c)
        if c == OTHER_CODE.get(field):
            others.append(t["values"].get("other_maintenance_tx") or t.get("drug_label") or "other drug")
    return sorted(codes, key=int), list(dict.fromkeys(others))


def _tx_evidence(treatments: list[dict]) -> list[str]:
    return [e for t in treatments for e in t.get("evidence", [])[:3]]


# ------------------------------------------------------------------------------------------
# relapse details, admissions → relapse and non-relapse hospitalization
# ------------------------------------------------------------------------------------------
ADMISSION_TO_RELAPSE = {"nr_admission": "date_admission", "nr_discharge": "date_discharge", "nr_icu": "icu_rh",
                        "acute_rx": "acute_rx", "other_acute_rx": "other_acute_rx",
                        "nr_adverse_events": "adverse_events_hosp", "nr_infection_adverse": "infection_hosp",
                        "nr_organism_adverse": "infection_organism", "adverse_events": "adverse_events",
                        "adverse_events_other": "adverse_events_other", "nr_rehab": "rehab",
                        "nr_rehab_admit": "rehab_admit", "nr_rehab_discharge": "rehab_discharge",
                        "nr_mrs_adm": "mrs_adm", "nr_mrs_disch": "mrs_disch"}
NR_FIELDS = ["nr_admission", "nr_discharge", "nr_cause", "nr_infection_detail", "nr_cause_other", "nr_organism_cause",
             "bleeding_detail", "nr_icu", "nr_adverse_events", "nr_infection_adverse", "nr_organism_adverse",
             "nr_adverse_other", "nr_rehab", "nr_rehab_admit", "nr_rehab_discharge", "nr_mrs_adm", "nr_mrs_disch"]
RELAPSE_DETAIL_FIELDS = ["clinical_confirm", "trigger", "gad_relapse", "let", "noncore_phenotype"]


def _admission_date(i) -> date | None:
    return _d(_v(i, "nr_admission")) or _d(_v(i, "nr_discharge")) or _d(i.doc_date)


def _stay_start(stay: list) -> date | None:
    """A stay's admission date: its first item's. A stay started by an item with no admission date (18.11.26: before,
    a second undated item then failed on None) is dated by that item's discharge or note date."""
    return _d(_v(stay[0], "nr_admission")) or _admission_date(stay[0])


def admissions(items: list) -> list[list]:
    """Items of the two admission groups, one list per hospital stay (admission dates within 3 days; items without
    an admission date join a stay within 14 days of their discharge date or note date)."""
    # "admission_dates": the "Admit date:" / "Discharge date:" header lines read with text patterns; they come first,
    # so on a tie their dates are the ones proposed
    adm = [i for i in items if i.group in ("admission", "admission_course", "admission_dates") and not i.negated
           and not i.hypothetical]
    adm.sort(key=lambda i: i.group != "admission_dates")
    dated = sorted([i for i in adm if _d(_v(i, "nr_admission"))], key=lambda i: _d(_v(i, "nr_admission")))
    stays: list[list] = []
    for i in dated:
        d = _d(_v(i, "nr_admission"))
        hit = next((s for s in stays if abs((d - _stay_start(s)).days) <= 3), None)
        (hit.append(i) if hit else stays.append([i]))
    for i in adm:
        if _d(_v(i, "nr_admission")):
            continue
        d = _admission_date(i)
        if d is None:
            continue
        if i.group == "admission_dates":
            # a header with a discharge date only: the latest stay that began before it (a stay can be long); it
            # never makes a stay of its own, as its date is not an admission date
            before = [s for s in stays if 0 <= (d - _stay_start(s)).days <= 120]
            if before:
                max(before, key=_stay_start).insert(0, i)
            continue
        hit = min((s for s in stays if -3 <= (d - _stay_start(s)).days <= 14),
                  key=lambda s: abs((d - _stay_start(s)).days), default=None)
        if hit is not None:
            hit.append(i)
        elif i.group == "admission":
            stays.append([i])
    return stays


def _hospital_name(v) -> str | None:
    return None if v in EMPTY else " ".join(str(v).split())


# window (days) around a stay's admission date for a hospital mention's admission date, by its precision
# (estimate code): 1 exact, 2 month (stored as the 15th), 3/4 year (stored as 1 July)
_HOSPITAL_WINDOW = {1: 3, 2: 31, 3: 183, 4: 183}


def stay_hospitals(stays: list[list], items: list) -> list[list]:
    """The `hospital` group's admission mentions (nr_hosp) of each stay of `admissions()`, in the same order. They
    never make a stay of their own and never change its dates: the admission groups decide what was an admission.
    A dated mention joins the stay nearest its admission date: within 3 days for an exact date; for a month or a
    year only when that is the one stay within the month or year. An undated mention joins the stay its note was
    written during (or up to 14 days after)."""
    out: list[list] = [[] for _ in stays]
    starts = [_stay_start(s) for s in stays]
    ends = [max([_d(_v(i, "nr_discharge")) for i in s if _d(_v(i, "nr_discharge"))] + [st]) for s, st in
            zip(stays, starts, strict=True)]
    for i in items:
        if i.group != HOSPITAL_GROUP or i.negated or i.hypothetical or not _v(i, "nr_hosp"):
            continue
        d = _d(_v(i, "nr_admission"))
        if d is not None:
            window = _HOSPITAL_WINDOW.get(i.precision_rank, 3)
            near = [k for k, st in enumerate(starts) if abs((d - st).days) <= window]
            if window > 3 and len(near) > 1:
                continue   # a month or a year with more than one stay in it: not placed
        else:
            d = _d(i.doc_date)
            if d is None:
                continue
            near = [k for k, (st, en) in enumerate(zip(starts, ends, strict=True))
                    if (d - st).days >= -3 and (d - en).days <= 14]
        if near:
            out[min(near, key=lambda k: abs((d - starts[k]).days))].append(i)
    return out


def merge_relapse_details(relapses: list[dict], items: list, dic: Dictionary, window: int, wide: int) -> list[dict]:
    """Attack details and admissions into the relapse instances (new instances when an attack is found only there).
    `relapses` are reconcile_relapses' instances; returns them as instances of this module (values + evidence)."""
    out = []
    for r in relapses:
        inst = new_instance(r["anchor"], _d(r["anchor"]))
        for f, v in r["values"].items():
            if v not in EMPTY:
                inst["values"][f] = v
                inst["evidence"][f] += list(r["evidence"])
        if r.get("conflict"):
            inst["notes"].append(r["conflict"])
        inst["_items"] = list(r.get("members", []))
        out.append(inst)
    # attack details come with the relapse group's own items (16.11.26); a `relapse_detail` group, if configured,
    # adds its items by date
    det = [i for i in items if i.group == "relapse_detail" and not i.negated and not i.hypothetical and i.d]
    for i in sorted(det, key=lambda i: (i.precision_rank, i.d)):
        w = wide if i.precision_rank >= 3 else window
        best = min((x for x in out if x["date"] and abs((i.d - x["date"]).days) <= w),
                   key=lambda x: abs((i.d - x["date"]).days), default=None)
        if best is None:
            best = new_instance(i.event_date, i.d)
            best["_items"] = []
            best["values"]["date_onset"] = i.event_date
            if i.estimate and i.estimate != NE:
                best["values"]["date_attack_estimate"] = i.estimate
            best["evidence"]["date_onset"] += i.evidence_ids
            best["notes"].append("attack found only by the attack-details reading: check it is not a duplicate")
            out.append(best)
        best["_items"].append(i)
    for inst in out:
        its = inst.pop("_items")
        for f in RELAPSE_DETAIL_FIELDS:
            put(inst, f, its, dic)
        inst["_first"] = [i for i in its if _v(i, "firstrelapsestate")]
    out.sort(key=lambda x: x["date"] or date.max)
    # the state of the FIRST attack only (#47): the earliest attack's own answer, else any attack's
    first_state = [i for inst in out for i in inst["_first"]]
    for inst in out:
        inst.pop("_first", None)
    if out and first_state:
        put(out[0], "firstrelapsestate", sorted(first_state, key=lambda i: i.d or date.max)[:1], dic)
    return out


def _put_hospital(inst: dict, mentions: list, dic: Dictionary, label: str, field: str = "nr_hosp") -> None:
    """The hospital named most often (the latest on a tie); when the notes give more than one name (a transfer, or
    one hospital written two ways), the reviewer is told and sees every mention in the evidence."""
    put(inst, field, list(reversed(mentions)), dic, label=label, transform=_hospital_name)
    names = {_hospital_name(_v(i, field)).lower() for i in mentions}
    if len(names) > 1:
        inst["info"].append(f"{label}: the notes name {len(names)} different hospitals (a transfer, or the same "
                            "hospital written two ways): the one named most often is proposed; see the evidence")


HEADER_GROUP = "admission_dates"   # "Admit date:" / "Discharge date:" lines read with text patterns (note_facts)


def header_dates(inst: dict, stay: list, fields: dict[str, str]) -> None:
    """Reader against model (18.11.26). `fields`: {the stay items' key: the instance's field}. When the summary header
    gives a date, it is the one proposed; if the notes read by the model (or the attack's own mention) give another
    date more than a day away, the reviewer is told both."""
    for src, dst in fields.items():
        head = [i for i in stay if i.group == HEADER_GROUP and _d(_v(i, src))]
        if not head:
            continue
        h = _d(_v(head[0], src))
        others = {_d(_v(i, src)) for i in stay if i.group != HEADER_GROUP and _d(_v(i, src))}
        cur = _d(inst["values"].get(dst))
        if cur:
            others.add(cur)
        differ = sorted(d for d in others if abs((d - h).days) > 1)
        if differ:
            inst["notes"].append(f"{dst}: the summary header says {h.isoformat()}, the notes read by the model say "
                                 f"{', '.join(d.isoformat() for d in differ)}: the header's date is proposed")
        inst["values"][dst] = h.isoformat()
        ev = [e for i in head for e in [i.field_evidence.get(src)] if e]
        inst["evidence"][dst] = ev + [e for e in inst["evidence"].get(dst, []) if e not in ev]


def route_admissions(relapse_insts: list[dict], items: list, dic: Dictionary) -> list[dict]:
    """Each hospital stay: for an attack → into the relapse instance it belongs to (or a new one); otherwise a
    non-relapse hospitalization instance (returned)."""
    nr_out = []
    stays = admissions(items)
    for stay, hosp in zip(stays, stay_hospitals(stays, items), strict=True):
        adm_d = min((_d(_v(i, "nr_admission")) for i in stay if _d(_v(i, "nr_admission"))), default=None) \
            or min(_admission_date(i) for i in stay)
        votes = Counter(str(_v(i, "for_attack")) for i in stay if _v(i, "for_attack") is not None)
        for_attack = votes.most_common(1)[0][0] if votes else None
        rel = min((r for r in relapse_insts if r["date"] and -3 <= (adm_d - r["date"]).days <= 60),
                  key=lambda r: abs((adm_d - r["date"]).days), default=None)
        same_adm = next((r for r in relapse_insts if _d(r["values"].get("date_admission"))
                         and abs((_d(r["values"]["date_admission"]) - adm_d).days) <= 3), None)
        if for_attack == "1" or same_adm is not None:
            target = same_adm or rel
            if target is None:
                target = new_instance(adm_d.isoformat(), adm_d)
                target["values"]["date_onset"] = adm_d.isoformat()
                target["values"]["date_attack_estimate"] = "2"
                target["notes"].append("attack found from its admission only: onset date set to the admission date")
                relapse_insts.append(target)
            if for_attack != "1":
                target["notes"].append("an admission for this attack is described as being for another reason")
            target["values"]["admission"] = "1"
            for src, dst in ADMISSION_TO_RELAPSE.items():
                if dst not in target["values"] or (dst in dic and dic[dst].is_checkbox):
                    put(target, src, stay, dic, label=dst)
            header_dates(target, stay, {"nr_admission": "date_admission", "nr_discharge": "date_discharge"})
            if "nr_hosp_2" not in target["values"]:
                _put_hospital(target, hosp, dic, "nr_hosp_2")
            if target["values"].get("adverse_events"):
                derive(target, "adverse_event", "1")
            continue
        inst = new_instance(f"adm|{adm_d.isoformat()}", adm_d)
        if for_attack is None:
            inst["notes"].append("the notes do not say whether this admission was for an NMOSD attack")
        for f in NR_FIELDS:
            put(inst, f, stay, dic)
        header_dates(inst, stay, {"nr_admission": "nr_admission", "nr_discharge": "nr_discharge"})
        _put_hospital(inst, hosp, dic, "nr_hosp")
        if "nr_admission" not in inst["values"]:
            derive(inst, "nr_admission", adm_d.isoformat(), note="admission date taken from the note's date")
        nr_out.append(inst)
    relapse_insts.sort(key=lambda x: x["date"] or date.max)
    return nr_out


def derive_relapse(relapse_insts: list[dict], treatments: list[dict], labs: list[dict]) -> None:
    for r in relapse_insts:
        when = r["date"]
        if not when:
            continue
        on = active_on(treatments, when)
        codes, others = drug_codes("dmt_relapse", on)
        derive(r, "dmt_relapse", codes or ["1"], _tx_evidence(on))
        if others:
            derive(r, "dmt_relapse_other", "; ".join(others))
        new = started_between(treatments, when, date.fromordinal(when.toordinal() + 90))
        ncodes, nothers = drug_codes("new_dmt", new)
        if ncodes:
            derive(r, "dmt_change", "1", _tx_evidence(new))
            derive(r, "new_dmt", ncodes, _tx_evidence(new))
            if nothers:
                derive(r, "dmt_change_other", "; ".join(nothers))
        for test, field in (("cd19_abs", "cd19"), ("cd20_abs", "cd20"), ("alc", "alc")):
            prior = [x for x in labs if x["test"] == test and x["date"] and 0 <= (when - x["date"]).days <= 90]
            if prior:
                last = max(prior, key=lambda x: x["date"])
                derive(r, field, last["value"], last["evidence"])


def derive_hospitalizations(nr_insts: list[dict], treatments: list[dict]) -> None:
    for h in nr_insts:
        a = h["date"]
        if not a:
            continue
        on = active_on(treatments, a)
        codes, others = drug_codes("nr_dmt", on)
        derive(h, "nr_dmt", codes or ["1"], _tx_evidence(on))
        if others:
            derive(h, "nr_dmt_other", "; ".join(others))
        dis = _d(h["values"].get("nr_discharge")) or a
        new = started_between(treatments, a, date.fromordinal(dis.toordinal() + 30))
        ncodes, nothers = drug_codes("nr_dmt_discharge", new)
        if ncodes:
            derive(h, "nr_dmt_change", "1", _tx_evidence(new))
            derive(h, "nr_dmt_discharge", ncodes, _tx_evidence(new))
            if nothers:
                derive(h, "nr_dmt_discharge_other", "; ".join(nothers))


# ------------------------------------------------------------------------------------------
# imaging
# ------------------------------------------------------------------------------------------
IMAGING_GROUPS = ("imaging", "imaging_brain", "imaging_spine")


def reconcile_imaging(items: list, dic: Dictionary, docs: dict, relapse_insts: list[dict]) -> list[dict]:
    its = [i for i in items if i.group in IMAGING_GROUPS and not i.negated and not i.hypothetical]
    # an undated MRI in a radiology report is dated by the report; one only mentioned in a clinic note is not placed
    usable = [i for i in its if i.d or (docs.get(i.doc_id) and docs[i.doc_id].relevance == "neuro_mri")]
    out = []
    form_fields = [n for n in dic.forms.get("imaging", []) if n in dic]
    for grp in cluster(usable, 14):
        earliest = min(grp, key=lambda i: (_item_date(i), i.precision_rank))
        first = _item_date(earliest)
        inst = new_instance(f"mri|{first.isoformat()}", first)
        # the earliest MRI of the set (codebook #116); a radiology report without a written date is dated by itself
        est = earliest.estimate if earliest.d and earliest.estimate not in (None, NE) else "1"
        derive(inst, "date_of_study", first.isoformat(), earliest.evidence_ids[:1])
        derive(inst, "date_mri_estimate", est)
        for f in form_fields:
            if f in ("date_of_study", "date_mri_estimate"):
                continue
            put(inst, f, grp, dic, mode="any1" if f in ("did_this_mri_show_radiolog", "mri_optic_abn", "mri_brain_abnl",
                                                        "supratentorial", "infratentorial", "mri_c_abn", "mri_t_abn")
                else "common")
        types = set(inst["values"].get("mri_type") or [])
        types |= {"1"} if any(i.group == "imaging_brain" for i in grp) else set()
        types |= {"3"} if any(_v(i, "mri_c_abn") is not None or _v(i, "cervical_features") for i in grp) else set()
        types |= {"4"} if any(_v(i, "mri_t_abn") is not None or _v(i, "thoracic_features") for i in grp) else set()
        if types:
            inst["values"]["mri_type"] = sorted(types, key=int)
        kinds = {"1" if docs.get(i.doc_id) and docs[i.doc_id].relevance == "neuro_mri" else "3" for i in grp}
        derive(inst, "mri_available_info", sorted(kinds))
        rel = min((n for n, r in enumerate(relapse_insts, 1) if r["date"] and -30 <= (first - r["date"]).days <= 60),
                  key=lambda n: abs((first - relapse_insts[n - 1]["date"]).days), default=None)
        if rel:
            derive(inst, "mri_relapse_number", str(rel), note=f"MRI placed with attack #{rel} by date")
        out.append(finish(inst, dic))
    return out


# ------------------------------------------------------------------------------------------
# treatment details, reasons (into reconcile_treatments' courses)
# ------------------------------------------------------------------------------------------
REASON_SUBFIELD = {  # start_reason → code of the sub-question REDCap asks for each reason_maintenance
    "maintenance_restart": {"accidental_stop": "1", "symptoms": "2", "attack": "3", "preference": "4", "other": "5"},
    "maintenance_add": {"symptoms": "1", "attack": "2", "preference": "3", "toxicity": "4", "steroid_sparing": "5",
                        "pregnancy": "6", "other": "7"},
    "maintenance_change": {"attack": "1", "symptoms": "2", "toxicity": "3", "preference": "4", "cost": "5",
                           "titration": "6", "steroid_sparing": "7"},
}


def reason_category(course: dict, courses: list[dict]) -> tuple[str | None, str]:
    """reason_maintenance (#150) from the order of the courses, following the codebook: no earlier course → 1;
    every earlier course stopped more than 30 days before → 2 (restart); an earlier course stopping within 30 days
    of this start → 5 (change); one continuing more than 30 days after it → 3 (addition)."""
    s = course.get("start_date")
    if not s:
        return None, ""
    earlier = [c for c in courses if c is not course and c.get("start_date") and c["start_date"] < s]
    if not earlier:
        return "1", "no earlier maintenance course found"
    if any(c["drug"] == course["drug"] and c.get("values", {}).get("reason_for_stopping_tx") == "5" and c.get("stop_date")
           and abs((s - c["stop_date"]).days) <= 30 for c in earlier):
        return "4", "the same drug's previous course stopped for a dose change"
    if any(c.get("stop_date") is None or (c["stop_date"] - s).days > 30 for c in earlier):
        return "3", "an earlier course continues more than 30 days after this start"
    if any(abs((s - c["stop_date"]).days) <= 30 for c in earlier):
        return "5", "an earlier course stopped within 30 days of this start"
    return "2", "every earlier course stopped more than 30 days before this start"


def _dose_key(v) -> tuple[str, str] | None:
    """('3000', 'mg') for "3,000 mg", "3000mg IV", ...: doses written differently compare equal. None: no dose read."""
    m = re.search(r"(?i)(\d[\d,]*(?:\.\d+)?)\s*(mg|mcg|g|units?|mL)\b", str(v or ""))
    return (f"{float(m.group(1).replace(',', '')):g}", m.group(2).lower().rstrip("s")) if m else None


def merge_treatment_details(courses: list[dict], items: list, dic: Dictionary, relapse_insts: list[dict]) -> list[dict]:
    det = [i for i in items if i.group in ("treatment_detail", "treatment_admin") and not i.negated and not i.hypothetical
           and _v(i, "maintenance_therapy") and not getattr(i, "unnamed", False)]   # code check: drug named
    out = []
    by_course: dict[int, list] = defaultdict(list)
    for i in det:
        when = _d(i.doc_date)
        mine = [n for n, c in enumerate(courses) if c["drug"] == str(_v(i, "maintenance_therapy"))]
        if not mine:
            continue
        inside = [n for n in mine if when and courses[n].get("start_date") and courses[n]["start_date"] <= when
                  and (courses[n].get("stop_date") is None or courses[n]["stop_date"] >= when)]
        pick = inside[-1] if inside else min(mine, key=lambda n: abs(((courses[n].get("start_date") or when or date.min)
                                                                       - (when or date.min)).days))
        by_course[pick].append(i)
    for n, c in enumerate(courses):
        inst = new_instance(c["anchor"], c.get("start_date"))
        for f, v in c["values"].items():
            if v not in EMPTY:
                inst["values"][f] = v
                inst["evidence"][f] += list(c["evidence"])
        if c.get("conflict"):
            inst["notes"].append(c["conflict"])
        inst["info"] += c.get("info", [])
        its = by_course.get(n, [])
        recs = [i for i in its if i.group == "treatment_admin"]
        read = [i for i in its if i.group != "treatment_admin"]
        for f in ("maintenance_tx_dosing", "maintenance_tx_freq", "other_frequency_detail", "maint_tox",
                  "detail_toxicities_describe"):
            # reader against model (18.11.26): the administration records' value is proposed when there is one
            put(inst, f, recs if any(_v(i, f) for i in recs) else its, dic)
        for f, label, norm in (("maintenance_tx_dosing", "dose", _dose_key), ("maintenance_tx_freq", "frequency", str)):
            a_vals = {norm(_v(i, f)) for i in recs if _v(i, f) and norm(_v(i, f))}
            m_vals = {norm(_v(i, f)) for i in read if _v(i, f) and norm(_v(i, f))}
            if a_vals and m_vals and not (a_vals & m_vals):
                show = lambda vs: ", ".join(sorted(" ".join(v) if isinstance(v, tuple) else str(v) for v in vs))  # noqa: E731
                inst["notes"].append(f"{label}: the administration records say {show(a_vals)}, the notes read by the "
                                     f"model say {show(m_vals)}: the records' value is proposed")
        if c["drug"] == "15":
            put(inst, "other_maintenance_tx", its, dic)
        cat, why = reason_category(c, courses)
        if cat:
            derive(inst, "reason_maintenance", cat, note=f"reason_maintenance {cat}: {why}")
        s = c.get("start_date")
        early = [i for i in its if s and _d(i.doc_date) and -30 <= (_d(i.doc_date) - s).days <= 120]
        reasons = Counter(_v(i, "start_reason") for i in early if _v(i, "start_reason"))
        reason = reasons.most_common(1)[0][0] if reasons else None
        prior = [(k, r) for k, r in enumerate(relapse_insts, 1) if s and r["date"] and 0 <= (s - r["date"]).days <= 180]
        if reason is None and prior:
            reason = "attack"
            inst["info"].append(f"started within 6 months after attack #{prior[-1][0]}: reason proposed as that attack")
        if cat == "1" and s:
            if prior and reason == "attack":
                derive(inst, "maintenance_start", "1" if prior[-1][0] == 1 else "2")
            elif reason == "symptoms":
                derive(inst, "maintenance_start", "3")
        sub = {"2": "maintenance_restart", "3": "maintenance_add", "4": "maintenance_change", "5": "maintenance_change"}.get(cat or "")
        if sub and reason in REASON_SUBFIELD[sub]:
            derive(inst, sub, REASON_SUBFIELD[sub][reason], [i.field_evidence.get("start_reason") for i in early])
        if prior and reason == "attack":
            derive(inst, "relapse_number_maintenance", str(prior[-1][0]))
        out.append(finish(inst, dic))
    return out


# ------------------------------------------------------------------------------------------
# labs: CD19/CD20, immunoglobulins, lymphocytes
# ------------------------------------------------------------------------------------------
NUM_RX = re.compile(r"^\s*([<>]=?)?\s*(\d+(?:\.\d+)?)\s*$")


def _lab_value(test: str, value: str, unit: str) -> str | None:
    m = NUM_RX.match(str(value or "").replace(",", ""))
    if not m:
        return None
    op, x = m.group(1), float(m.group(2))
    u = (unit or "").lower().replace("µ", "u").replace(" ", "")
    if test in ("igg", "igm", "iga"):
        if u in ("g/l", "gm/l"):
            x *= 100.0
        elif u in ("mg/l",):
            x /= 10.0
        return f"{x:g}" if not op else None
    if test.startswith("cd") and test.endswith("_abs"):     # also the flow-panel subsets (cd19p20n_abs, ...)
        if op and op.startswith("<") and x <= 5:
            return "0"
        return str(round(x)) if not op else None
    if test == "alc":
        if u in ("k/ul", "x10^3/ul", "10^3/ul", "thou/ul", "x10e3/ul"):
            x *= 1000.0
        return f"{x:g}" if not op else None
    return (f"{op}{x:g}" if op else f"{x:g}")


def lab_results(items: list, docs: dict) -> list[dict]:
    out = []
    for i in items:
        # "labs_table": table rows read with text patterns (nmosd.extract.lab_tables); they also carry antibody rows
        if i.group not in ("labs", "labs_table") or i.negated or i.hypothetical or not _v(i, "test"):
            continue
        test = str(_v(i, "test"))
        val = _lab_value(test, _v(i, "value") or "", _v(i, "unit") or "")
        if val is None:
            continue
        when = i.d or _d(i.doc_date)
        out.append({"test": test, "value": val, "date": when, "dated_by_note": not i.d, "item": i,
                    "table": i.group == "labs_table",
                    "evidence": [i.field_evidence.get("value") or (i.evidence_ids[0] if i.evidence_ids else None)]})
    return out


CD_TESTS = ("cd19_abs", "cd19_pct", "cd20_abs", "cd20_pct", "cd19_unspecified", "cd20_unspecified",
            "cd19p20n_abs", "cd19p20n_pct", "cd19p20p_abs", "cd19p20p_pct", "cd19p20n_unspecified",
            "cd19p20p_unspecified")


def _subset_total(by: dict, kind: str, inst: dict) -> tuple[str | None, list]:
    """Total CD19+ (`kind` "abs" or "pct") as CD19+CD20- plus CD19+CD20+, only when both rows come from the same
    note and date (one specimen, one panel, so the same unit and denominator) and both are plain numbers. Says so
    in the instance; one subset alone, or a "<" percentage, gives nothing but a note."""
    neg, pos = by.get(f"cd19p20n_{kind}", []), by.get(f"cd19p20p_{kind}", [])
    if not neg and not pos:
        return None, []
    pair = next(((x, y) for x in neg for y in pos if x["item"].doc_id == y["item"].doc_id and x["date"] == y["date"]),
                None)
    label = "count" if kind == "abs" else "percentage"
    if pair is None:
        inst["notes"].append(f"only one of the CD19+CD20- and CD19+CD20+ rows was found for the same sample: total "
                             f"CD19 {label} not worked out")
        return None, []
    try:
        total = float(pair[0]["value"]) + float(pair[1]["value"])
    except ValueError:      # "<1": below the reporting limit, cannot be added
        inst["notes"].append(f"CD19+CD20- and CD19+CD20+ {label}s are below the reporting limit: total not worked out")
        return None, []
    inst["info"].append(f"CD19 {label}: CD19+CD20- plus CD19+CD20+ of the same flow panel (no total was reported)")
    return f"{total:g}", [e for r in pair for e in r["evidence"]]


def reconcile_labs(results: list[dict], dic: Dictionary) -> tuple[list[dict], list[dict]]:
    cd, ig = [], []
    for tests, form, out in ((CD_TESTS, "cd", cd), (("igg", "igm", "iga"), "ig", ig)):
        rs = sorted([r for r in results if r["test"] in tests and r["date"]], key=lambda r: r["date"])
        groups: list[list[dict]] = []
        for r in rs:
            hit = next((g for g in groups if abs((r["date"] - g[0]["date"]).days) <= 3), None)
            (hit.append(r) if hit else groups.append([r]))
        for g in groups:
            d0 = g[0]["date"]
            inst = new_instance(f"{form}|{d0.isoformat()}", d0)
            if any(r["dated_by_note"] for r in g):
                inst["info"].append("no test date in the note: dated by the note")
            by = defaultdict(list)
            for r in g:
                by[r["test"]].append(r)

            def val(test, by=by, inst=inst):
                rs = sorted(by.get(test, []), key=lambda r: not r.get("table"))   # a table row before a sentence
                if len({r["value"] for r in rs}) > 1:
                    inst["notes"].append(f"{test}: notes give different values")
                return (rs[0]["value"], [e for r in rs for e in r["evidence"]]) if rs else (None, [])

            if form == "cd":
                derive(inst, "date_cd", d0.isoformat(), [e for r in g for e in r["evidence"]])
                for n in ("19", "20"):
                    a, ae = val(f"cd{n}_abs")
                    p, pe = val(f"cd{n}_pct")
                    raw = by.get(f"cd{n}_unspecified", [])
                    if n == "19":
                        # no total reported: the two CD19+ subsets of one flow panel add up to it (clinician, 18.11.26)
                        if a is None:
                            a, ae = _subset_total(by, "abs", inst)
                        if p is None:
                            p, pe = _subset_total(by, "pct", inst)
                    else:
                        both = by.get("cd19p20p_abs", []) + by.get("cd19p20p_pct", [])
                        if a is None and p is None and both:
                            inst["notes"].append(
                                "CD19+CD20+ is reported (" + ", ".join(dict.fromkeys(
                                    f"{r['value']}{'%' if r['test'].endswith('_pct') else ''}" for r in both))
                                + "), but no total CD20+: not entered as CD20, since it may leave out CD19-CD20+ cells")
                            inst["evidence"]["cd20_yn"] += [e for r in both for e in r["evidence"]]
                    if raw:
                        inst["notes"].append(f"CD{n} = {', '.join(dict.fromkeys(r['value'] for r in raw))} on a row that "
                                             "does not say count or percent: not entered; check the note")
                    if a is None and p is None and not raw:
                        continue
                    derive(inst, f"cd{n}_yn", "1", ae + pe + [e for r in raw for e in r["evidence"]])
                    if a is None and p is None:
                        continue
                    derive(inst, f"cd{n}_abs_yn", "1" if a is not None else "0", ae + pe)
                    derive(inst, f"cd{n}_abs", a, ae)
                    derive(inst, f"cd{n}_percent", p, pe)
            else:
                derive(inst, "date_igg", d0.isoformat(), [e for r in g for e in r["evidence"]])
                for t in tests:
                    v, e = val(t)
                    derive(inst, t, v, e)
            out.append(finish(inst, dic))
    return cd, ig


# ------------------------------------------------------------------------------------------
# immunoglobulin replacement, infections
# ------------------------------------------------------------------------------------------
def reconcile_ivig(items: list, dic: Dictionary) -> list[dict]:
    its = sorted([i for i in items if i.group == "ivig" and not i.negated and not i.hypothetical
                  and str(_v(i, "for_hypogam")) == "1" and _v(i, "event") in ("start", "stop", "ongoing")],
                 key=lambda i: _item_date(i) or date.max)
    its = [i for i in its if _item_date(i)]
    if not its:
        return []
    courses, cur = [], []
    for i in its:
        if cur and _v(cur[-1], "event") == "stop" and _v(i, "event") in ("start", "ongoing") \
                and (_item_date(i) - _item_date(cur[-1])).days > 60:
            courses.append(cur)
            cur = []
        cur.append(i)
    courses.append(cur)
    out = []
    for c in courses:
        starts = [i for i in c if _v(i, "event") == "start" and i.d]
        first = min(starts, key=lambda i: (i.d, i.precision_rank)) if starts else c[0]
        d0 = first.d or _item_date(first)
        inst = new_instance(f"ivig|{d0.isoformat()}", d0)
        derive(inst, "date_starting_ivig", d0.isoformat(), first.evidence_ids[:1],
               None if starts else "no start found: dated by the first mention")
        last = c[-1]
        if _v(last, "event") == "stop":
            derive(inst, "ivig_yn", "0", last.evidence_ids[:1])
            derive(inst, "date_stopping_ivig", (last.d or _item_date(last)).isoformat(), last.evidence_ids[:1])
        else:
            derive(inst, "ivig_yn", "1", last.evidence_ids[:1], "still on: from the most recent mention")
        for f in ("ivig_dose", "other_dose", "frequency"):
            put(inst, f, list(reversed(c)), dic)
        out.append(finish(inst, dic))
    return out


def reconcile_infections(items: list, dic: Dictionary) -> list[dict]:
    its = [i for i in items if i.group == "infection" and not i.negated and not i.hypothetical]
    out = []
    for grp in cluster(its, 14):
        d0 = min(_item_date(i) for i in grp)
        inst = new_instance(f"inf|{d0.isoformat()}", d0)
        iso, _ = _best_date(grp)
        derive(inst, "infection_date", iso or d0.isoformat(), [e for i in grp for e in i.evidence_ids[:1]],
               None if iso else "no onset date in the notes: dated by the note")
        for f in ("care_setting", "infection_type", "infection_other"):
            put(inst, f, grp, dic)
        out.append(finish(inst, dic))
    return out


# ------------------------------------------------------------------------------------------
# EDSS
# ------------------------------------------------------------------------------------------
EDSS_GROUPS = ("edss_ambulation", "edss_vision_brainstem", "edss_pyramidal_cerebellar", "edss_sensory_bowel_cerebral")


def reconcile_edss(items: list, dic: Dictionary, courses: list[dict], con_d, study_id: str) -> list[dict]:
    its = [i for i in items if i.group in EDSS_GROUPS and not i.negated]
    if not its:
        return []
    by_doc = defaultdict(list)
    for i in its:
        by_doc[i.doc_id].append(i)
    anchors = []
    for c in courses:
        if c.get("start_date"):
            anchors.append((c["start_date"], "start", f"start of treatment {c['drug']}"))
        if c.get("stop_date"):
            anchors.append((c["stop_date"], "stop", f"stop of treatment {c['drug']}"))
    notes = exam_notes(con_d, study_id).get(study_id, [])
    out, seen = [], set()
    fields = [n for n in dic.forms.get("edss", []) if n in dic and not dic[n].is_calc and not dic[n].is_descriptive
              and not n.endswith("_complete") and n not in ("date_of_assessment", "timing")]
    for a in pick_assessment_notes(anchors, notes):
        if a.doc_id not in by_doc or (a.doc_id, a.timing) in seen:
            continue
        seen.add((a.doc_id, a.timing))
        inst = new_instance(f"edss|{a.doc_id}|{a.timing}", a.doc_date)
        derive(inst, "date_of_assessment", a.doc_date.isoformat(), [by_doc[a.doc_id][0].evidence_ids[0]])
        derive(inst, "timing", a.timing, note=f"examination note for the {a.label}")
        for f in fields:
            put(inst, f, by_doc[a.doc_id], dic)
        out.append(finish(inst, dic))
    out.sort(key=lambda x: (x["date"] or date.max, x["values"].get("timing", "")))
    return out


# ------------------------------------------------------------------------------------------
# demographics: background, death, vitals, derived; last follow-up
# ------------------------------------------------------------------------------------------
LONG_TERM_EFFECTS = [  # (pattern in a condition's name, code of has_your_patient_experienc #299)
    (r"osteopor", "2"), (r"avascular necrosis|osteonecrosis", "3"), (r"diabet|hyperglyc", "4"),
    (r"obes|weight gain", "5"), (r"psychos|steroid.*(?:mood|psych)", "6"), (r"glaucom", "7"), (r"lipodystroph", "8"),
    (r"adrenal insufficien", "9"), (r"thrombos|\bDVT\b|pulmonary embol|\bPE\b|clot", "10"),
    (r"hypogammaglobulin", "11"), (r"leukopeni|neutropeni|lymphopeni", "12"), (r"recurrent infection", "13"),
    (r"colitis|diverticulitis|perforat", "14"), (r"cancer|carcinoma|lymphoma|leukemia|melanoma|malignan", "15")]


def demographics_extra(items: list, dic: Dictionary, *, living: str | None, dx_date: date | None,
                       relapse_insts: list[dict], nr_insts: list[dict], courses: list[dict], antibodies: list[dict],
                       edss: list[dict], last_contact: str | None, gate_dx: str | None,
                       comorbidities: list[str] | None = None) -> dict:
    """Demographics fields beyond the existing ones: background and death groups, vitals, and the derived ones.
    living (the table's living_deceased code) and comorbidities decide which questions REDCap asks."""
    inst = new_instance("demographics", None)
    bg = [i for i in items if i.group == "background" and not i.negated and not i.hypothetical]
    for f in ("baseline_mobility", "cancer"):
        put(inst, f, bg, dic)
    put(inst, "insurance_denial", bg, dic, mode="any1")
    put(inst, "dmt_denied", bg, dic)
    # primary hospital for outpatient care (only when the notes name hospitals): the one named most often
    op = [i for i in items if i.group == HOSPITAL_GROUP and not i.negated and not i.hypothetical and _v(i, "hx_opcare")]
    _put_hospital(inst, op, dic, "hx_opcare", field="hx_opcare")
    if living == "2":
        dth = [i for i in items if i.group == "death" and not i.negated and not i.hypothetical]
        put(inst, "cause_death", dth, dic)
        put(inst, "death_nmo", dth, dic)
    # vitals: the measurement nearest the diagnosis date (else the latest)
    for f in ("weight", "height"):
        vs = [i for i in items if i.group == "vitals" and _v(i, f) and _d(i.doc_date)]
        if vs:
            pick = min(vs, key=lambda i: abs((_d(i.doc_date) - dx_date).days)) if dx_date else \
                max(vs, key=lambda i: _d(i.doc_date))
            derive(inst, f, _v(pick, f), [pick.field_evidence.get(f)],
                   f"{f}: the measurement {'nearest the diagnosis date' if dx_date else 'of the latest note'} "
                   f"({len(vs)} found)" if len(vs) > 1 else None)
    if last_contact:
        derive(inst, "last_fu", last_contact)
    if gate_dx == "nmosd":
        derive(inst, "nmo_criteria", "1", note="nmo_criteria proposed from the diagnosis check (NMOSD)")
    # attacks
    phen = lambda rs: sorted({PHENOTYPE_TO_LOCATION[p] for r in rs for p in (r["values"].get("relapse_phenotype") or [])  # noqa: E731
                               if p in PHENOTYPE_TO_LOCATION}, key=int)
    if dx_date:
        early = [r for r in relapse_insts if r["date"] and r["date"] <= date.fromordinal(dx_date.toordinal() + 30)]
        derive(inst, "relapse_type_diagnosis", phen(early))
    derive(inst, "loc_nmo_total", phen(relapse_insts))
    if relapse_insts:
        derive(inst, "relapse_number", str(len(relapse_insts)))
        derive(inst, "rh_number", str(sum(r["values"].get("admission") == "1" for r in relapse_insts)))
    derive(inst, "nr_number", str(len(nr_insts)) if (relapse_insts or nr_insts) else None)
    # treatments
    if dx_date:
        before = [c for c in courses if c.get("start_date") and c["start_date"] < dx_date]
        if before:
            codes, others = drug_codes("rx_before_nmo", before)
            derive(inst, "dx_other_treatment", "1", _tx_evidence(before))
            derive(inst, "rx_before_nmo", codes, _tx_evidence(before))
            if others:
                derive(inst, "other_treatment_name", "; ".join(others))
    if living == "1" and courses:
        still = [c for c in courses if c["values"].get("still_on_therapy") == "1" and c["drug"] != "16"]
        codes, others = drug_codes("dmt_lastfu", still)
        derive(inst, "dmt_lastfu", codes or ["1"], _tx_evidence(still))
        if others:
            derive(inst, "dmt_other", "; ".join(others))
        last = [e for e in edss if e["values"].get("timing") == TIMING["last"] and e["values"].get("mrs")]
        if last:
            derive(inst, "mrs_lastfu", str(int(last[-1]["values"]["mrs"]) - 1), last[-1]["evidence"].get("mrs"))
    # antibodies
    for t, field, pos in (("aqp4", "aqp4", "aqp4_assay_status"), ("mog", "mog", "mog_test_positive")):
        res = [a["values"].get(pos) for a in antibodies if a["values"].get("type_test") == t and a["values"].get(pos)]
        if res:
            positive = "1" in res
            derive(inst, field, "1" if positive else "2",
                   [e for a in antibodies if a["values"].get("type_test") == t for e in a["evidence"][:2]])
    return finish(inst, dic, {"living_deceased": living, "comorbidities": comorbidities or []})


def last_follow_up(dic: Dictionary, last_contact: str | None, last_clinical: date | None, data_end: date | None,
                   conditions: list[dict]) -> dict | None:
    if not last_contact and not last_clinical:
        return None
    inst = new_instance("last_follow_up", _d(last_contact) or last_clinical)
    derive(inst, "last_follow_up_date", last_contact or last_clinical.isoformat())
    if last_clinical and data_end:
        years = (data_end - last_clinical).days / 365.25
        derive(inst, "is_the_patient_still_prima", "1" if years <= 2 else "3",
               note=f"last clinical note {years:.1f} years before the newest note in the data")
    effects = sorted({code for c in conditions if c.get("category") not in (NMOSD_RELATED,)
                      for rx, code in LONG_TERM_EFFECTS if re.search(rx, c.get("name", ""), re.I)}, key=int)
    if effects:
        derive(inst, "has_your_patient_experienc", effects,
               note="long-term side effects proposed from the conditions found in the notes: check each is treatment-related")
    return finish(inst, dic)


__all__ = ["NO_CATEGORY", "admissions", "demographics_extra", "derive_hospitalizations", "derive_relapse",
           "lab_results", "last_follow_up", "merge_relapse_details", "merge_treatment_details", "reconcile_edss",
           "reconcile_imaging", "reconcile_infections", "reconcile_ivig", "reconcile_labs", "route_admissions",
           "stay_hospitals"]
