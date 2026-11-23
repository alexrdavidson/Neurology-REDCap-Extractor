"""Build the synthetic RPDR-like dataset (deterministic from a seed)."""

from __future__ import annotations

import csv
import hashlib
import json
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from nmosd.synth import notes as N
from nmosd.synth.people import CLINICIANS, OUTSIDE_HOSPITALS, Person, build_people

NOTE_COLUMNS = ["EMPI", "Report_Number", "Report_Date_Time", "Report_Description", "Report_Status",
                "Report_Type", "Report_Text"]
DEM_COLUMNS = ["EMPI", "Gender_Legal_Sex", "Date_of_Birth", "Age", "Sex_At_Birth", "Gender_Identity", "Language",
               "Language_group", "Race1", "Race2", "Race_Group", "Ethnic_Group", "Marital_status", "Religion",
               "Is_a_veteran", "Zip_code", "Country", "Vital_status", "Date_Of_Death"]
PLACEHOLDER_TABLE = '<-- Click on "Forms" to view the data'
EXPORT_DATE = date(2026, 11, 4)
# patient key -> (days after the last visit the outreach reminder is sent, days after it the infusion is due)
OUTREACH_AFTER_LAST = {"P1": (30, 60)}
REAL_TABLE_NAMES = {"Vis": "VisitNote", "Prg": "ProgressNote", "Rad": "Radiology", "Pat": "Pathology",
                    "Dis": "Discharge_Summaries", "Hnp": "HistoryAndPhysical", "Dem": "Demographics"}
TWO_DIGIT_TABLES = {"VisitNote", "ProgressNote", "HistoryAndPhysical"}
# REDCap codes of the synthetic demographics, for the note annotations (same as config/redcap/structured_maps.yaml)
SEX_CODES = {"Female": "2", "Male": "1"}
RACE_CODES = {"White": "1", "Black or African American": "2", "Other": "5"}


@dataclass
class Dataset:
    people: list[Person]
    notes: list[N.Note]


# ------------------------------------------------------------------------------------------
# encounter scheduling
# ------------------------------------------------------------------------------------------
def _at(d: date, rng: random.Random, lo: int = 8, hi: int = 17) -> datetime:
    return datetime.combine(d, time(rng.randint(lo, hi), rng.choice([0, 10, 15, 20, 30, 40, 45]), 0))


def schedule(p: Person, rng: random.Random) -> list[tuple[date, str, dict]]:
    ev: list[tuple[date, str, dict]] = []
    if p.notes_profile == "control":
        ev += [(p.first_seen, "consult", {}), (p.first_seen + timedelta(days=3), "mri", {"relapse": p.relapses[0]}),
               (date(2023, 5, 2), "visit", {})]
        return ev
    for r in p.relapses:
        if r.admitted and r.admit and r.admit >= p.first_seen:
            ev += [(r.admit, "ed", {"relapse": r}), (r.admit, "hp", {"relapse": r}),
                   (r.admit, "mri", {"relapse": r}),
                   (r.admit + timedelta(days=1), "progress_neuro", {"relapse": r}),
                   (r.admit + timedelta(days=2), "nursing", {"relapse": r}),
                   (r.discharge, "discharge", {"relapse": r})]
        elif not r.admitted and r.onset >= p.first_seen:
            ev += [(r.onset + timedelta(days=4), "urgent", {"relapse": r}),
                   (r.onset + timedelta(days=5), "mri", {"relapse": r})]
    # regular follow-up visits
    start = p.first_seen if p.notes_profile != "elderly" else date(2022, 9, 1)
    d, k = start + timedelta(days=21), 0
    while d < p.last_seen and k < 8:
        kind = "tele" if (d.year in (2020, 2021) or k % 3 == 2) else "visit"
        ev.append((d, kind, {}))
        d += timedelta(days=rng.randint(230, 330))
        k += 1
    ev.append((p.last_seen, "visit", {}))
    if p.notes_profile != "elderly":
        ev.append((p.first_seen, "consult", {}))
    else:
        ev.append((date(2015, 3, 2), "visit", {}))
    for t in p.treatments:
        if t.planned_note_date:
            ev.append((t.planned_note_date, "visit", {"planned": t}))
        if t.name in N.INFUSED and t.start >= p.first_seen:
            ev.append((t.start, "procedure", {"treatment": t}))
        if t.name in N.B_CELL:
            fc = t.start + timedelta(days=180)
            if fc < (t.stop or p.last_seen):
                ev.append((fc, "flow", {"treatment": t}))
        if t.name in {"mycophenolate", "azathioprine"} and t.start >= p.first_seen:
            ev.append((t.start + timedelta(days=90), "renewal", {"treatment": t}))
    for drug, d0 in p.planned_only:
        ev.append((d0, "visit", {"planned_only": drug}))
    # surveillance MRI (one duplicated under two report numbers), low-value notes, messages
    ev.append((p.first_seen + timedelta(days=400), "mri_surv", {"duplicate": True}))
    ev.append((p.first_seen + timedelta(days=150), "xr", {}))
    ev.append((p.first_seen + timedelta(days=95), "email", {}))
    ev.append((p.last_seen - timedelta(days=40), "email", {}))
    ev.append((p.first_seen + timedelta(days=260), "triage", {}))
    if p.key == "P1":
        ev.append((date(2019, 8, 6), "path_routine", {}))
    if p.key in OUTREACH_AFTER_LAST:   # a reminder sent after the last visit: extends follow-up (10.11.26)
        ev.append((p.last_seen + timedelta(days=OUTREACH_AFTER_LAST[p.key][0]), "outreach", {}))
    if p.pregnancy:
        ev.append((p.pregnancy["delivered"] - timedelta(days=-1), "lactation", {}))
    ev = [e for e in ev if e[0] <= EXPORT_DATE]
    ev.sort(key=lambda e: (e[0], e[1]))
    return ev


# ------------------------------------------------------------------------------------------
# note writers
# ------------------------------------------------------------------------------------------
class Writer:
    def __init__(self, p: Person, rng: random.Random):
        self.p, self.rng = p, rng
        self.last_visit: date | None = None
        self.visit_variant = 0
        self.dob_formats = [N.f_mdyyyy, N.f_long, N.f_iso, N.f_mmddyy, N.f_dmon]
        self.dob_i = 0

    # ---- helpers
    def _note(self, table: str, when: datetime, desc: str, rtype: str, status: str = "F") -> N.Note:
        site = "B" if (table == "Radiology" and self.rng.random() < 0.25) else "M"
        return N.Note(table, self.p, when, desc, f"{site}{rtype}", status)

    def _dob_mention(self, n: N.Note) -> str:
        fmt = self.dob_formats[self.dob_i % len(self.dob_formats)]
        self.dob_i += 1
        val = fmt(self.p.dob)
        label = {N.f_mdyyyy: "DOB", N.f_long: "born", N.f_iso: "D.O.B.:", N.f_mmddyy: "dob",
                 N.f_dmon: "DOB:"}[fmt]
        return f"{label} {n.seed('DOB', val)}"

    def _race_ethnicity(self, n: N.Note) -> str:
        p = self.p   # ethnicity is structured only (not looked for in the notes), so only race is annotated
        return n.ann("demographics", [f"Self-reported race: {p.race_group}; ethnicity: {p.ethnic_group}"],
                     [{"race": RACE_CODES[p.race_group]}])

    @staticmethod
    def _past_history(n: N.Note) -> str:
        """Comorbidities: two in REDCap categories (migraine: neurologic; anxiety: psychiatric), one outside them;
        a procedure and a negated history that are not conditions."""
        cond = lambda quote, name, cat: n.ann("comorbidity", [quote], [{"condition": name, "comorbidities": cat}])  # noqa: E731
        return (cond("Vitamin D deficiency", "Vitamin D deficiency", "no_category") + ". "
                + cond("Migraine without aura", "Migraine without aura", "3") + " since adolescence. "
                + cond("Anxiety, managed with therapy", "Anxiety", "2") + ". "
                "Left knee arthroscopy (2011). No history of tuberculosis or hepatitis B.")

    def _age_phrase(self, n: N.Note, d: date) -> str:
        age = self.p.age_on(d)
        txt = f"{age}-year-old" if age < 90 else n.seed("AGE", f"{age}-year-old")
        return txt

    # ---- clinic letters
    def neuro_visit(self, d: date, kind: str, meta: dict) -> N.Note:
        p, rng = self.p, self.rng
        when = _at(d, rng)
        desc = {"tele": "Telemedicine", "urgent": "Office Visit", "consult": "Office Visit"}.get(kind, "Office Visit")
        n = self._note("VisitNote", when, desc, "VIS", "R" if (kind == "visit" and rng.random() < 0.08) else "F")
        provider = CLINICIANS["np"] if kind == "tele" and rng.random() < 0.5 else CLINICIANS["neuro"]
        N.visit_header(n, rng, provider, "MGH NEUROLOGY - NEUROIMMUNOLOGY", self.visit_variant % 3)
        self.visit_variant += 1
        title = {"consult": "NEUROIMMUNOLOGY NEW PATIENT CONSULTATION", "tele": "TELEMEDICINE FOLLOW-UP (VIDEO)",
                 "urgent": "URGENT NEUROIMMUNOLOGY VISIT"}.get(kind, "NEUROIMMUNOLOGY FOLLOW-UP VISIT")
        n.add(title, "")
        n.add(N.dx_line(n, d), N.tx_line(n, d), "")
        age = self._age_phrase(n, d)
        n.add("HPI",
              f"{p.title} {n.seed('PATIENT', p.last)} is a {age} {p.noun} ({self._dob_mention(n)}) seen today "
              f"{'by video' if kind == 'tele' else 'in clinic'}, accompanied by {p.possessive} "
              f"{p.relatives[0][0]}.", "")
        n.add(N.origin_paragraph(n), "")
        for r in [r for r in p.relapses if r.onset <= d]:
            n.add(N.relapse_paragraph(n, r, "alt" if (p.key == "P3" and self.visit_variant % 2) else "narrative"), "")
        n.add("INTERVAL EVENTS", self._interval(n, d, kind, meta), "")
        n.add("Past Medical History", self._past_history(n), "")
        rel, name = p.relatives[1] if len(p.relatives) > 1 else p.relatives[0]
        n.add("Family History",
              f"{p.possessive.capitalize()} {rel}, {n.seed('RELATIVE', name)}, has hypothyroidism. "
              "No family history of multiple sclerosis or NMOSD.", "")
        n.add("Current medication regimen", *N.med_table(p, d), "")
        n.add("Social History",
              f"Lives in {n.seed('LOCATION', p.town)} with {p.possessive} {p.relatives[0][0]} "
              f"{n.seed('RELATIVE', p.relatives[0][1].split()[0])}. Never smoker. Works as a "
              f"{'school librarian' if p.sex == 'Female' else 'surveyor'}. "
              f"Preferred contact: cell {n.seed('PHONE', p.phone)}. " + self._race_ethnicity(n) + ".", "")
        n.add("PHYSICAL EXAM", "Vitals: BP 118/76 | Pulse 72 | Temp 98.2 F | Wt 68 kg", *N.exam_block(p, d, rng), "")
        n.add("DATA / RESULTS", *self._data_lines(n, d), "")
        n.add("ASSESSMENT & PLAN", *self._assessment(n, d, kind, meta), "")
        n.add(*N.boilerplate_block(n, d, rng))
        n.add(*N.signoff(n, provider))
        self.last_visit = d
        return n

    def _interval(self, n: N.Note, d: date, kind: str, meta: dict) -> str:
        p = self.p
        since = self.last_visit
        recent = [r for r in p.relapses if since and since < r.onset <= d]
        if kind == "urgent" and meta.get("relapse"):
            r = meta["relapse"]
            return (f"{p.pronoun.capitalize()} presents urgently with {r.pheno_text} that began 4 days ago. "
                    f"{p.pronoun.capitalize()} has been taking {p.possessive} medications as prescribed.")
        if recent:
            return (f"Since the last visit {p.pronoun} had a relapse ({recent[-1].pheno_short}) as described above. "
                    f"{p.pronoun.capitalize()} has recovered partially and walks without an aid.")
        last = f" on {N.f_mdyyyy(since)}" if since else ""
        extra = ""
        if p.key == "P1" and d.year >= 2019:
            extra = f" {p.possessive.capitalize()} sister {n.seed('RELATIVE', 'Hope')} has been helping with childcare."
        if p.key == "P2" and p.pregnancy and d < p.pregnancy["delivered"] and d.year >= 2020:
            extra = " She is currently pregnant (second trimester) and feels well."
        return (f"No new relapses since the last visit{last}. {p.pronoun.capitalize()} denies new visual "
                f"symptoms, weakness or bladder change. Mild fatigue in the afternoons; Uhthoff phenomenon "
                f"with hot showers.{extra}")

    def _data_lines(self, n: N.Note, d: date) -> list[str]:
        p = self.p
        out = ["Selected relevant imaging"]
        for r in [r for r in p.relapses if r.onset <= d and r.mri_text]:
            mdate = (r.admit or r.onset + timedelta(days=5))
            out.append(f"MRI {r.mri_region.lower()} {N.f_mdyyyy(mdate)}: {r.mri_text}.")
        out.append("Selected relevant laboratory data")
        for a in [a for a in p.antibodies if a.drawn <= d]:
            name = "AQP4-IgG" if a.test == "aqp4" else "MOG-IgG"
            res = "positive" if a.positive else "negative"
            titer = f", titer {a.titer}" if a.titer and a.positive else ""
            q = n.ann("antibody", [f"{name} {res} by cell-based assay ({a.lab}) on ", ("a", N.f_mdyyyy(a.drawn), "day")],
                      [{"type_test": a.test, "result": res, "assay_date_slot": "a"}])
            out.append(q + titer + ".")
        cur = N.current_treatment(p, d)
        if cur and cur.name in N.B_CELL:
            fc = cur.start + timedelta(days=180)
            if fc <= d:
                out.append(f"CD19 0% (flow cytometry {N.f_mdyyyy(fc)}); IgG 612 mg/dL.")
            out.append(f"TB T spot negative ({N.f_mdyyyy(cur.start - timedelta(days=30))}). "
                       "Hep B virus serologies: HBsAg negative, HBcAb negative.")
        return out

    def _assessment(self, n: N.Note, d: date, kind: str, meta: dict) -> list[str]:
        p = self.p
        cur = N.current_treatment(p, d)
        lines = [f"{self._age_phrase(n, d)} " + n.ann("demographics", [f"{p.noun} with {p.dx_phrase}"],
                                                       [{"sex": SEX_CODES[p.sex]}])
                 + (f", currently on {cur.name}." if cur else ", not yet on maintenance therapy.")]
        since = self.last_visit
        for t in p.treatments:
            if since and since < t.start <= d:
                lines.append(n.ann("treatment", [f"{p.pronoun.capitalize()} started {t.name} on ",
                                                 ("s", N.f_mdyyyy(t.start), "day")],
                                   [{"maintenance_therapy": str(t.code), "event": "start", "event_date_slot": "s"}]) + ".")
            if t.stop and since and since < t.stop <= d:
                lines.append(n.ann("treatment", [f"{t.name.capitalize()} was stopped on ",
                                                 ("e", N.f_mdyyyy(t.stop), "day"), f", {t.reason_text}"],
                                   [{"maintenance_therapy": str(t.code), "event": "stop", "event_date_slot": "e",
                                     "reason_for_stopping_tx": str(t.reason_stop)}]) + ".")
        if meta.get("planned"):
            t = meta["planned"]
            lines.append(n.ann("treatment", [f"Plan to start {t.name} after insurance approval"],
                               [{"maintenance_therapy": str(t.code), "event": "planned"}]) + ".")
        if meta.get("planned_only"):
            drug = meta["planned_only"]
            lines.append(n.ann("treatment", [f"Plan to start {drug} pending insurance approval"],
                               [{"maintenance_therapy": str(N.DRUG_CODES[drug]), "event": "planned"}]) + ".")
        for drug, d0 in p.planned_only:
            if since and since < d0 + timedelta(days=20) <= d:
                lines.append(f"{drug.capitalize()} was denied by insurance and was never given.")
        if cur:
            lines.append(n.ann("treatment", [f"Continue {cur.name} {cur.dose_text}"],
                               [{"maintenance_therapy": str(cur.code), "event": "ongoing"}]) + ".")
            if cur.name in N.B_CELL:
                lines.append("Check CD19 count and IgG level before the next infusion.")
        lines.append("We discussed the risk of infections and reviewed vaccinations. Return to clinic in 6 months.")
        return lines

    # ---- inpatient
    def ed_note(self, d: date, r) -> N.Note:
        p, rng = self.p, self.rng
        n = self._note("VisitNote", _at(d, rng, 0, 6), "ED Provider Note", "VIS")
        N.visit_header(n, rng, CLINICIANS["ed"], "MGH EMERGENCY DEPARTMENT", 1)
        n.add("ED PROVIDER NOTE", "", "HPI",
              f"{p.first} {p.last} is a {self._age_phrase(n, d)} {p.noun} with {p.dx_phrase} who presents with "
              + n.ann("relapse", [f"{r.pheno_text} since ", ("onset", N.f_mdyyyy(r.onset), "day")],
                      N.relapse_items(r, "onset", False)) + ".", "")
        n.seed("PATIENT", p.first)
        n.seed("PATIENT", p.last)
        n.add("Medications", *N.med_table(p, d), "", "Exam",
              "BP 132/84, HR 96. Alert. Strength 4/5 in the legs; reflexes 3+ at the knees.", "",
              "Assessment", "Concern for NMOSD relapse. Neurology consulted; admit for IV steroids.")
        n.add(*N.signoff(n, CLINICIANS["ed"]))
        return n

    def hp_note(self, d: date, r) -> N.Note:
        p, rng = self.p, self.rng
        n = self._note("HistoryAndPhysical", _at(d, rng, 9, 22), "History and Physical", "HNP")
        N.inpatient_header(n, rng, "NEUROLOGY ELLISON 12")
        n.add("NEUROLOGY ADMISSION NOTE", "", "History of Presenting Illness",
              f"{p.title} {n.seed('PATIENT', p.last)} is a {self._age_phrase(n, d)} {p.noun} ({self._dob_mention(n)}) "
              f"with {p.dx_phrase} admitted with "
              + n.ann("relapse", [f"{r.pheno_text}; symptom onset ", ("onset", N.f_mdyyyy(r.onset), "day")],
                      N.relapse_items(r, "onset", False)) + ".", "")
        n.add(N.dx_line(n, d), N.tx_line(n, d), "")
        n.add("Relevant Prior to Admission Medications", *N.med_table(p, d), "")
        n.add("Neurologic examination", *N.exam_block(p, d, rng)[1:], "")
        n.add("DATA & IMAGING", f"MRI {r.mri_region.lower()} today: {r.mri_text}.", "")
        n.add("ASSESSMENT & PLAN", "Active Neurologic Issue(s) & Plan",
              f"# NMOSD relapse ({r.pheno_short}): start IV methylprednisolone 1 g daily x5 days; "
              "consult apheresis for PLEX if no improvement.",
              f"# Contact: {p.relatives[0][0]} {n.seed('RELATIVE', p.relatives[0][1])}, {n.seed('PHONE', p.phone)}.")
        n.add(*N.signoff(n, CLINICIANS["resident"]))
        return n

    def progress_neuro(self, d: date, r) -> N.Note:
        p, rng = self.p, self.rng
        n = self._note("ProgressNote", _at(d, rng), "Progress Note - Neurology", "PRG")
        N.inpatient_header(n, rng, "NEUROLOGY ELLISON 12")
        n.add("Neurology Progress Note", "", "Subjective",
              f"Hospital day 2. Tolerating steroids. {p.first} reports slight improvement.", "",
              "Exam", "Strength 4/5 legs, improved from 3/5. Sensory level T6. Reflexes 3+.", "",
              "Plan", f"Day 2 of 5 IVMP. {r.acute_text.capitalize()}. PT/OT.",
              "BP Readings from Last 2 Encounters:", f"{N.f_mmddyy(d)}   128/82", f"{N.f_mmddyy(d - timedelta(days=1))}   132/84")
        n.seed("PATIENT", p.first)
        n.add(*N.signoff(n, CLINICIANS["resident"]))
        return n

    def nursing(self, d: date) -> N.Note:
        rng = self.rng
        n = self._note("ProgressNote", _at(d, rng, 19, 23), "Nursing Note", "PRG")
        N.inpatient_header(n, rng, "ELLISON 12 NURSING")
        n.add("Nursing Progress Note", "Patient resting comfortably. Voiding via straight cath q6h. "
              "Pain 3/10. Fall precautions in place.", "", "Patient Stated Goal", "Walk to the bathroom independently.",
              "", "Education Provided", "Steroid side effects; glucose monitoring.")
        n.add(*N.signoff(n, CLINICIANS["nurse"]))
        return n

    def lactation(self, d: date) -> N.Note:
        p, rng = self.p, self.rng
        n = self._note("ProgressNote", _at(d, rng), "Lactation Note", "PRG")
        N.inpatient_header(n, rng, "BLAKE 14 OBSTETRICS")
        n.add("Lactation Consultation", f"Postpartum day 2 after delivery of {p.possessive} daughter "
              f"{n.seed('RELATIVE', 'Zuri')}. Breastfeeding well. Mother on eculizumab; no contraindication "
              "to breastfeeding per neurology.")
        n.add(*N.signoff(n, CLINICIANS["lactation"]))
        return n

    def discharge(self, d: date, r) -> N.Note:
        p, rng = self.p, self.rng
        n = self._note("Discharge_Summaries", _at(d, rng, 10, 16), "Discharge Summary", "DIS")
        N.inpatient_header(n, rng, "NEUROLOGY ELLISON 12")
        n.add("DISCHARGE SUMMARY", "", "Principal Problem", f"NMOSD relapse: {PHENO(r)}", "",
              "Brief Summary of Hospitalization",
              n.ann("relapse", [f"{p.pronoun.capitalize()} presented with symptom onset on ",
                                ("onset", N.f_mdyyyy(r.onset), "day"), ", was admitted on ",
                                ("adm", N.f_mdyyyy(r.admit), "day"), f", was treated with {r.acute_text} and was "
                                "discharged on ", ("dis", N.f_mdyyyy(r.discharge), "day")],
                    [{"date_onset_slot": "onset", "admission": "1", "date_admission_slot": "adm",
                      "date_discharge_slot": "dis", "acute_rx": [str(c) for c in r.acute_rx],
                      "relapse_phenotype": [str(c) for c in r.phenotype],
                      "mri_relapse": str(r.mri_new) if r.mri_new is not None else None}]) + ".", "",
              "INITIAL HPI", N.relapse_paragraph(n, r), "",
              "Hospital Course by Problem",
              f"# {PHENO(r).capitalize()}: improved with treatment. MRI showed {r.mri_text}.", "")
        n.add(N.dx_line(n, d), N.tx_line(n, d), "")
        cur = N.current_treatment(p, d)
        nxt = [t for t in p.treatments if d < t.start <= d + timedelta(days=60)]
        n.add("ITEMS FOR FOLLOW UP")
        for t in nxt:
            n.add(n.ann("treatment", [f"Plan to start {t.name} as an outpatient"],
                        [{"maintenance_therapy": str(t.code), "event": "planned"}]) + ".")
        if cur and not nxt:
            n.add(f"Continue {cur.name}.")
        n.add(f"Follow up with {n.seed('STAFF', 'Dr. Blackwood')} in 4 weeks.", "",
              f"Discharge address: {n.seed('LOCATION', p.street)}, {n.seed('LOCATION', p.town)}, MA "
              f"{n.seed('LOCATION', p.zip5)}. MRN {n.seed('ID', p.mrns['MGH'][:3] + '-' + p.mrns['MGH'][3:])}.")
        n.add(*N.signoff(n, CLINICIANS["hospitalist"]))
        return n

    # ---- radiology / pathology
    def mri(self, d: date, r, surveillance: bool = False) -> list[N.Note]:
        rng = self.rng
        region = "CERVICAL AND THORACIC SPINE" if surveillance else r.mri_region
        desc = {"BRAIN": "MRI BRAIN W/WO CONTRAST", "CERVICAL SPINE": "MRI SPINE CERVICAL W/WO CONTRAST",
                "THORACIC SPINE": "MRI SPINE THORACIC W/WO CONTRAST", "ORBITS": "MRI ORBITS FACE W/WO CONTRAST",
                "CERVICAL AND THORACIC SPINE": "MRI SPINE CERVICAL W/WO CONTRAST"}[region]
        when = _at(d, rng, 7, 21)
        n = self._note("Radiology", when, desc, "RAD")
        reason = "NMOSD surveillance" if surveillance else f"NMOSD, new {r.pheno_text.split(' with ')[0]}"
        N.radiology_header(n, f"MRI {region} WITH AND WITHOUT CONTRAST", reason)
        finding = "No new or enhancing lesions." if surveillance else f"There is {r.mri_text}."
        n.add("TECHNIQUE", "Multiplanar multisequence MRI before and after 7 mL gadobutrol.", "",
              "COMPARISON", f"MRI dated {N.f_iso(d - timedelta(days=400))} (outside study).", "",
              "FINDINGS")
        if "SPINE" in region:
            n.add("Findings by level:", "C2-C5: see below. T4-T9: no stenosis. Cord signal described below.",
                  "CERVICAL SPINE" if "CERVICAL" in region else "THORACIC SPINE")
        else:
            n.add("BRAIN" if region == "BRAIN" else "ORBITS")
        n.add(finding, "")
        if surveillance:
            imp = "No new or enhancing lesions compared with prior."
            n.add("IMPRESSION", n.ann("imaging", [imp], [{"mri_relapse": "0"}]))
        else:
            imp = f"Findings consistent with an acute NMOSD relapse: {r.mri_text}"
            n.add("IMPRESSION", n.ann("relapse", [imp],
                                      [{"mri_relapse": str(r.mri_new) if r.mri_new is not None else None,
                                        "relapse_phenotype": [str(c) for c in r.phenotype], "date_onset_slot": None}]) + ".")
        n.add("", f"Interpreted by {n.seed('STAFF', CLINICIANS['radiologist'])}")
        n.report_number = ""
        out = [n]
        if surveillance:
            n.description = "MRI SPINE CERVICAL W/WO CONTRAST"
            n.extra_report_numbers = ["dup"]   # same text also filed under the thoracic order
        elif r.admitted and r.admit:
            prelim = self._note("Radiology", when, desc, "RAD", status="P")
            prelim.lines = [ln.replace("Status: Final", "Status: Preliminary") for ln in n.lines
                            if not ln.startswith("Interpreted by")] + ["PRELIMINARY REPORT"]
            prelim.phi = list(n.phi)
            prelim.report_number = "same"
            out.insert(0, prelim)
        return out

    def xr(self, d: date) -> N.Note:
        n = self._note("Radiology", _at(d, self.rng), "XR CHEST 2 VIEWS", "RAD")
        N.radiology_header(n, "XR CHEST PA AND LATERAL", "cough")
        n.add("FINDINGS", "Lungs are clear. No effusion.", "", "IMPRESSION", "No acute cardiopulmonary process.")
        return n

    def flow(self, d: date, t) -> N.Note:
        n = self._note("Pathology", _at(d, self.rng), "Flow Cytometry", "PAT")
        acc = f"F{d.year % 100:02d}-{self.rng.randint(10000, 99999)}"
        N.pathology_header(n, acc, "Flow Cytometry")
        n.add("CLINICAL HISTORY", f"NMOSD on {t.name}.", "", "RESULT",
              f"{'CD19':<12}{'0.4 %':<12}{'(ref 6-23 %)'}", f"{'CD20':<12}{'0.2 %':<12}{'(ref 6-23 %)'}",
              f"{'CD3':<12}{'78 %':<12}{'(ref 60-85 %)'}", "", "INTERPRETATION",
              "Marked depletion of CD19+ B cells, consistent with anti-CD20/CD19 therapy.", "",
              f"Pathologist: {n.seed('STAFF', CLINICIANS['pathologist'])}")
        return n

    def path_routine(self, d: date) -> N.Note:
        n = self._note("Pathology", _at(d, self.rng), "Surgical Pathology", "PAT")
        N.pathology_header(n, f"S{d.year % 100:02d}-{self.rng.randint(10000, 99999)}", "Surgical Pathology")
        n.add("CLINICAL HISTORY", "Skin lesion, left forearm.", "", "FINAL PATHOLOGIC DIAGNOSIS",
              "Seborrheic keratosis.", "", f"Pathologist: {n.seed('STAFF', CLINICIANS['pathologist'])}")
        return n

    # ---- messages / procedures
    def email(self, d: date) -> N.Note:
        p, rng = self.p, self.rng
        n = self._note("VisitNote", _at(d, rng), "Patient Email", "VIS")
        N.visit_header(n, rng, CLINICIANS["neuro"], "MGH NEUROLOGY - NEUROIMMUNOLOGY", 2)
        cur = N.current_treatment(p, d)
        n.add("Patient Email", f"From: {n.seed('EMAIL', p.email)}",
              f"Hi Dr. {n.seed('STAFF', 'Blackwood')}, this is {n.seed('PATIENT', p.preferred or p.first)}. "
              f"Can I take Tylenol with {cur.name if cur else 'my medication'}? My new number is "
              f"{n.seed('PHONE', p.phone)}. Thanks!", "",
              "Reply: Yes, acetaminophen is fine. Please call if you develop new symptoms.")
        if cur and cur.name in N.INFUSED:
            n.add(n.ann("followup", [f"Your next {cur.name} infusion is scheduled for ",
                                     ("v", N.f_mdyyyy(d + timedelta(days=21)), "day")],
                        [{"visit_type": "infusion", "visit_date_slot": "v"}]) + ".")
        n.add(*N.signoff(n, CLINICIANS["np"]))
        return n

    def outreach(self, d: date) -> N.Note:
        """A reminder sent to the patient (Outreach: "low value" by class, a follow-up contact by description)."""
        p, rng = self.p, self.rng
        n = self._note("VisitNote", _at(d, rng), "Outreach", "VIS")
        N.visit_header(n, rng, CLINICIANS["nurse"], "MGH INFUSION CENTER", 2)
        cur = N.current_treatment(p, d)
        when = p.last_seen + timedelta(days=OUTREACH_AFTER_LAST[p.key][1])
        n.add("Outreach", n.ann("followup", [f"Reminder: your {cur.name if cur else 'infusion'} infusion is scheduled "
                                             "for ", ("v", N.f_mdyyyy(when), "day")],
                                [{"visit_type": "infusion", "visit_date_slot": "v"}])
              + ". Please arrive 30 minutes early.")
        n.add(*N.signoff(n, CLINICIANS["nurse"]))
        return n

    def renewal(self, d: date, t) -> N.Note:
        n = self._note("VisitNote", _at(d, self.rng), "MyChart Renewal", "VIS")
        N.visit_header(n, self.rng, CLINICIANS["np"], "MGH NEUROLOGY - NEUROIMMUNOLOGY", 2)
        n.add("Medication renewal request",
              n.ann("treatment", [f"Renewal: {t.name} {t.dose_text}, 90-day supply"],
                    [{"maintenance_therapy": str(t.code), "event": "ongoing"}]) + ".",
              "Approved. Labs (CBC, LFTs) up to date.")
        n.add(*N.signoff(n, CLINICIANS["np"]))
        return n

    def procedure(self, d: date, t) -> N.Note:
        n = self._note("VisitNote", _at(d, self.rng), "Procedure", "VIS")
        N.visit_header(n, self.rng, CLINICIANS["nurse"], "MGH INFUSION CENTER", 2)
        n.add("Infusion Procedure Note",
              n.ann("treatment", [f"First {t.name} infusion given on ", ("s", N.f_mmddyyyy(t.start), "day")],
                    [{"maintenance_therapy": str(t.code), "event": "start", "event_date_slot": "s"}])
              + f" ({t.dose_text}). Premedicated with acetaminophen and diphenhydramine.",
              "Vitals stable throughout: BP 116/74 pre, 112/70 post. No infusion reaction.")
        n.add(*N.signoff(n, CLINICIANS["nurse"]))
        return n

    def triage(self, d: date) -> N.Note:
        n = self._note("VisitNote", _at(d, self.rng), "Nurse Triage", "VIS")
        N.visit_header(n, self.rng, CLINICIANS["nurse"], "MGH NEUROLOGY TRIAGE", 1)
        n.add("Telephone triage", f"Patient called from {n.seed('PHONE', self.p.phone)} reporting a cold. "
              "No new neurologic symptoms. Advised supportive care. Pain 2/10.")
        n.add(*N.signoff(n, CLINICIANS["nurse"]))
        return n


def PHENO(r) -> str:
    return " and ".join(N.PHENO_TEXT.get(c, "other") for c in r.phenotype)


def write_person(p: Person, rng: random.Random) -> list[N.Note]:
    w = Writer(p, rng)
    out: list[N.Note] = []
    for d, kind, meta in schedule(p, rng):
        r = meta.get("relapse")
        if kind in ("consult", "visit", "tele", "urgent"):
            out.append(w.neuro_visit(d, kind, meta))
        elif kind == "ed":
            out.append(w.ed_note(d, r))
        elif kind == "hp":
            out.append(w.hp_note(d, r))
        elif kind == "progress_neuro":
            out.append(w.progress_neuro(d, r))
        elif kind == "nursing":
            out.append(w.nursing(d))
        elif kind == "discharge":
            out.append(w.discharge(d, r))
        elif kind == "mri":
            out.extend(w.mri(d, r))
        elif kind == "mri_surv":
            out.extend(w.mri(d, p.relapses[0], surveillance=True))
        elif kind == "xr":
            out.append(w.xr(d))
        elif kind == "flow":
            out.append(w.flow(d, meta["treatment"]))
        elif kind == "path_routine":
            out.append(w.path_routine(d))
        elif kind == "email":
            out.append(w.email(d))
        elif kind == "outreach":
            out.append(w.outreach(d))
        elif kind == "renewal":
            out.append(w.renewal(d, meta["treatment"]))
        elif kind == "procedure":
            out.append(w.procedure(d, meta["treatment"]))
        elif kind == "triage":
            out.append(w.triage(d))
        elif kind == "lactation":
            out.append(w.lactation(d))
    return out


# ------------------------------------------------------------------------------------------
# dataset assembly
# ------------------------------------------------------------------------------------------
def build_dataset(seed: int = 20261109) -> Dataset:
    rng = random.Random(seed)
    people = build_people()
    notes: list[N.Note] = []
    for p in people:
        notes.extend(write_person(p, random.Random(rng.randint(0, 2**31))))
    used: set[str] = set()

    def new_number(prefix: str) -> str:
        while True:
            num = f"{prefix}{rng.randint(1_000_000, 9_999_999)}"
            if num not in used:
                used.add(num)
                return num

    last_rad: dict[str, str] = {}
    for n in notes:
        prefix = {"Radiology": "RAD", "Pathology": "PATH"}.get(n.table, "")
        if n.report_number == "same":
            n.report_number = new_number(prefix)
            last_rad[n.person.key] = n.report_number
        elif n.table == "Radiology" and n.status == "F" and last_rad.get(n.person.key) and not n.extra_report_numbers:
            n.report_number = last_rad.pop(n.person.key)
        else:
            n.report_number = new_number(prefix)
        n.extra_report_numbers = [new_number(prefix) for _ in n.extra_report_numbers]
    return Dataset(people, notes)


def _report_dt(n: N.Note) -> str:
    return N.rdt_two_digit(n.when) if n.table in TWO_DIGIT_TABLES else N.rdt_four_digit(n.when)


def note_rows(ds: Dataset) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {t: [] for t in ["VisitNote", "ProgressNote", "Radiology", "Pathology",
                                                     "Discharge_Summaries", "HistoryAndPhysical"]}
    for n in ds.notes:
        base = {"EMPI": n.person.empi, "Report_Number": n.report_number, "Report_Date_Time": _report_dt(n),
                "Report_Description": n.description, "Report_Status": n.status, "Report_Type": n.report_type,
                "Report_Text": n.text()}
        tables[n.table].append(base)
        for extra in n.extra_report_numbers:
            alt = dict(base, Report_Number=extra, Report_Description="MRI SPINE THORACIC W/WO CONTRAST")
            tables[n.table].append(alt)
    return tables


def demographics_rows(ds: Dataset) -> list[dict]:
    rows = []
    for p in ds.people:
        rows.append({
            "EMPI": p.empi, "Gender_Legal_Sex": p.sex, "Date_of_Birth": p.dob.strftime("%m/%d/%y 00:00:00"),
            "Age": str(p.age_on(EXPORT_DATE) if not p.date_of_death else p.age_on(p.date_of_death)),
            "Sex_At_Birth": p.sex, "Gender_Identity": "Identifies as " + p.sex.lower(), "Language": p.language,
            "Language_group": "English" if p.language == "English" else "Non-English", "Race1": p.race1,
            "Race2": "", "Race_Group": p.race_group, "Ethnic_Group": p.ethnic_group, "Marital_status": p.marital,
            "Religion": p.religion, "Is_a_veteran": "No", "Zip_code": p.zip5, "Country": "United States",
            "Vital_status": p.vital_status,
            "Date_Of_Death": p.date_of_death.strftime("%m/%d/%Y") if p.date_of_death else "",
        })
    return rows


def extra_table_rows(ds: Dataset) -> dict[str, list[dict]]:
    """RPDR tables missing from the real export (Mrn, Con, Med, Lab) for auto-pickup tests."""
    mrn, con, med, lab = [], [], [], []
    for p in ds.people:
        for site, m in p.mrns.items():
            mrn.append({"EMPI": p.empi, "MRN_Type": site, "MRN": m, "Status": "Active"})
        con.append({"EMPI": p.empi, "Last_Name": p.last, "First_Name": p.first, "Middle_Name": p.middle,
                    "Address1": p.street, "City": p.town, "State": "MA", "Zip": p.zip5, "Home_Phone": p.phone,
                    "Email_Address": p.email})
        for t in p.treatments:
            med.append({"EMPI": p.empi, "Medication_Date": t.start.strftime("%m/%d/%Y"), "Medication": t.name.upper(),
                        "Code_Type": "EPIC", "Code": str(1000 + t.code), "Quantity": "1",
                        "Inpatient_Outpatient": "Outpatient"})
        for a in p.antibodies:
            lab.append({"EMPI": p.empi, "Seq_Date_Time": a.drawn.strftime("%m/%d/%Y 08:00"), "Group_Id": a.test.upper(),
                        "Test_Description": f"{a.test.upper()} IGG CBA", "Result": "Positive" if a.positive else "Negative",
                        "Result_Text": f"titer {a.titer}" if a.titer else "", "Abnormal_Flag": "H" if a.positive else ""})
    return {"Mrn": mrn, "Con": con, "Med": med, "Lab": lab}


# ------------------------------------------------------------------------------------------
# gold standard, registry, identifiers
# ------------------------------------------------------------------------------------------
GOLD_FIELDS = ["record_id", "redcap_repeat_instrument", "redcap_repeat_instance", "sex", "date_nmo_diagnosis",
               "date_nmo_estimate", "otherdx", "dx_other", "living_deceased",
               "date_onset", "date_attack_estimate", *[f"relapse_phenotype___{i}" for i in range(1, 8)],
               "admission", "date_admission", "date_discharge", *[f"acute_rx___{i}" for i in range(1, 7)],
               "mri_relapse", "maintenance_therapy", "date_starting_maint", "date_maintenance_estimate",
               "still_on_therapy", "date_stopping_maint", "date_stopping_estimate", "reason_for_stopping_tx",
               "type_test", "aqp4_assay_date", "aqp4_assay_status", "mog_assay_date", "mog_test_positive"]


def record_id(p: Person) -> str:
    return f"SYN-{p.key[1:]:0>2}"


def gold_rows(ds: Dataset) -> list[dict]:
    rows = []
    for p in [p for p in ds.people if p.pilot]:
        rid = record_id(p)
        rows.append({"record_id": rid, "sex": "2" if p.sex == "Female" else "1",
                     "date_nmo_diagnosis": p.dx_date.isoformat(), "date_nmo_estimate": str(p.dx_est),
                     "otherdx": str(p.otherdx), "dx_other": p.dx_other,
                     "living_deceased": "2" if p.date_of_death else "1"})
        for i, r in enumerate(sorted(p.relapses, key=lambda r: r.onset), 1):
            row = {"record_id": rid, "redcap_repeat_instrument": "relapse", "redcap_repeat_instance": str(i),
                   "date_onset": r.onset.isoformat(), "date_attack_estimate": str(r.estimate),
                   "admission": str(r.admitted),
                   "date_admission": r.admit.isoformat() if r.admit else "",
                   "date_discharge": r.discharge.isoformat() if r.discharge else "",
                   "mri_relapse": "" if r.mri_new is None else str(r.mri_new)}
            for c in range(1, 8):
                row[f"relapse_phenotype___{c}"] = "1" if c in r.phenotype else "0"
            for c in range(1, 7):
                row[f"acute_rx___{c}"] = "1" if c in r.acute_rx else "0"
            rows.append(row)
        for i, t in enumerate(sorted(p.treatments, key=lambda t: t.start), 1):
            rows.append({"record_id": rid, "redcap_repeat_instrument": "treatment", "redcap_repeat_instance": str(i),
                         "maintenance_therapy": str(t.code), "date_starting_maint": t.start.isoformat(),
                         "date_maintenance_estimate": str(t.start_est),
                         "still_on_therapy": "0" if t.stop else "1",
                         "date_stopping_maint": t.stop.isoformat() if t.stop else "",
                         "date_stopping_estimate": str(t.stop_est) if t.stop_est else "",
                         "reason_for_stopping_tx": str(t.reason_stop) if t.reason_stop else ""})
        for i, a in enumerate(sorted(p.antibodies, key=lambda a: (a.drawn, a.test)), 1):
            row = {"record_id": rid, "redcap_repeat_instrument": "antibody_lab_data",
                   "redcap_repeat_instance": str(i), "type_test": a.test}
            if a.test == "aqp4":
                row |= {"aqp4_assay_date": a.drawn.isoformat(), "aqp4_assay_status": "1" if a.positive else "2"}
            else:
                row |= {"mog_assay_date": a.drawn.isoformat(), "mog_test_positive": "1" if a.positive else "0"}
            rows.append(row)
    return [{k: r.get(k, "") for k in GOLD_FIELDS} for r in rows]


ELIGIBILITY_FIELDS = ["record_id", "dx_category", "followup_months", "followup_12m", "included"]


def last_contact(p: Person) -> date:
    """Last follow-up contact: the last visit, or a later infusion date given in an outreach message."""
    if p.key in OUTREACH_AFTER_LAST:
        return max(p.last_seen, p.last_seen + timedelta(days=OUTREACH_AFTER_LAST[p.key][1]))
    return p.last_seen


def eligibility_rows(ds: Dataset) -> list[dict]:
    """Gold answers for the diagnosis check (P3 has MOGAD, so it is excluded)."""
    rows = []
    for p in [p for p in ds.people if p.pilot]:
        months = round((last_contact(p) - p.dx_date).days / 30.4375, 1)
        rows.append({"record_id": record_id(p), "dx_category": p.dx_category, "followup_months": months,
                     "followup_12m": "1" if months >= 12 else "0",
                     "included": "1" if p.dx_category == "nmosd" and months >= 12 else "0"})
    return rows


def seeded_phi(ds: Dataset) -> dict[str, list[dict]]:
    out: dict[str, set] = {}
    for n in ds.notes:
        for cat, val in n.phi:
            out.setdefault(n.person.empi, set()).add((cat, val))
    return {k: [{"category": c, "value": v} for c, v in sorted(s)] for k, s in sorted(out.items())}


def identifiers_yaml(ds: Dataset) -> dict:
    """What the user would confirm for the known-identifier layer (synthetic)."""
    pts = {}
    for p in ds.people:
        pts[p.empi] = {
            "names": {"first": [p.first], "last": [p.last], "middle": [p.middle],
                      "preferred": [p.preferred] if p.preferred else []},
            "mrns": list(p.mrns.values()),
            "dob": p.dob.isoformat(),
            "relatives": [{"relation": rel, "name": name} for rel, name in p.relatives],
            "address": {"street": [p.street], "town": [p.town], "zip": [p.zip5, f"{p.zip5}-{p.zip4}"]},
            "phones": [p.phone],
            "emails": [p.email],
        }
    staff = sorted({v for v in CLINICIANS.values()})
    return {"patients": pts, "staff": staff, "organizations": [*OUTSIDE_HOSPITALS, N.PCP_PRACTICE]}


def annotations(ds: Dataset) -> list[dict]:
    out = []
    for n in ds.notes:
        if n.annotations:
            out.append({"empi": n.person.empi, "table": n.table, "report_number": n.report_number,
                        "status": n.status, "annotations": n.annotations})
    return out


# ------------------------------------------------------------------------------------------
# writers
# ------------------------------------------------------------------------------------------
def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def write_sqlite(path: Path, tables: dict[str, list[dict]], columns: dict[str, list[str]]) -> None:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        for name, cols in columns.items():
            col_sql = ", ".join(f"{qident(c)} TEXT" for c in cols)
            con.execute(f"CREATE TABLE {qident(name)} ({col_sql})")
            rows = tables.get(name, [])
            if rows:
                ph = ", ".join("?" for _ in cols)
                con.executemany(f"INSERT INTO {qident(name)} VALUES ({ph})", [[r.get(c, "") for c in cols] for r in rows])
        con.commit()
    finally:
        con.close()


def write_csvs(out_dir: Path, tables: dict[str, list[dict]], columns: dict[str, list[str]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, cols in columns.items():
        if name == PLACEHOLDER_TABLE:
            continue
        with (out_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, quoting=csv.QUOTE_ALL)
            w.writeheader()
            for r in tables.get(name, []):
                w.writerow({c: r.get(c, "") for c in cols})


def build(out_dir: Path, seed: int = 20261109, with_csv: bool = False, extra_tables: bool = False,
          table_names: str = "real") -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ds = build_dataset(seed)
    tables = note_rows(ds)
    tables["Demographics"] = demographics_rows(ds)
    columns = {name: NOTE_COLUMNS for name in tables if name != "Demographics"}
    columns["Demographics"] = DEM_COLUMNS
    columns[PLACEHOLDER_TABLE] = ["Field1"]
    if extra_tables:
        for name, rows in extra_table_rows(ds).items():
            tables[name] = rows
            columns[name] = list(rows[0].keys())
    if table_names == "rpdr":
        inv = {v: k for k, v in REAL_TABLE_NAMES.items()}
        tables = {inv.get(k, k): v for k, v in tables.items()}
        columns = {inv.get(k, k): v for k, v in columns.items()}
    db = out_dir / "synthetic_rpdr.sqlite"
    write_sqlite(db, tables, columns)
    if with_csv:
        write_csvs(out_dir / "csv", tables, columns)
    gold = gold_rows(ds)
    with (out_dir / "gold_redcap.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=GOLD_FIELDS)
        w.writeheader()
        w.writerows(gold)
    with (out_dir / "gold_eligibility.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ELIGIBILITY_FIELDS)
        w.writeheader()
        w.writerows(eligibility_rows(ds))
    with (out_dir / "record_map.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["empi", "record_id"])
        for p in ds.people:
            if p.pilot:
                w.writerow([p.empi, record_id(p)])
    (out_dir / "pilot_empis.txt").write_text("\n".join(p.empi for p in ds.people if p.pilot) + "\n", encoding="utf-8")
    (out_dir / "identifiers.yaml").write_text(yaml.safe_dump(identifiers_yaml(ds), sort_keys=True), encoding="utf-8")
    (out_dir / "seeded_phi.json").write_text(json.dumps(seeded_phi(ds), indent=1, sort_keys=True), encoding="utf-8")
    (out_dir / "annotations.json").write_text(json.dumps(annotations(ds), indent=1, sort_keys=True), encoding="utf-8")
    digest = hashlib.sha256()
    for name in sorted(tables):
        for r in tables[name]:
            digest.update(json.dumps(r, sort_keys=True).encode())
    manifest = {"seed": seed, "export_date": EXPORT_DATE.isoformat(), "content_sha256": digest.hexdigest(),
                "tables": {k: len(v) for k, v in sorted(tables.items())}, "patients": len(ds.people),
                "pilot_patients": sum(p.pilot for p in ds.people), "table_names": table_names}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return manifest
