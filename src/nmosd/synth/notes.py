"""Render fictional RPDR-style notes for the synthetic people.

Everything mimics the masked shapes in BUILD_PLAN §1b: CRLF line endings, fixed-width header blocks
per table, section headings, Dx:/Tx: summary lines, boilerplate, copied-forward paragraphs,
date look-alikes, and fake PHI in awkward formats. Each inserted identifier is logged in a
seeded-PHI registry; each clinically meaningful sentence is logged as an annotation (quote with
date slots) so tests and the synthetic oracle client can find it after de-identification.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from nmosd.synth.people import OUTSIDE_HOSPITALS, PCP_PRACTICE, Person, Relapse, Treatment

CRLF = "\r\n"
REPORT_END = "[report_end]"
PHENO_TEXT = {1: "optic neuritis", 2: "transverse myelitis", 3: "brainstem syndrome", 4: "area postrema syndrome"}
DRUG_CODES = {"rituximab": 1, "mycophenolate": 2, "eculizumab": 3, "ravulizumab": 4, "inebilizumab": 5,
              "satralizumab": 6, "azathioprine": 7, "tocilizumab": 8}
INFUSED = {"rituximab", "inebilizumab", "eculizumab", "ravulizumab"}
B_CELL = {"rituximab", "inebilizumab"}


# ------------------------------------------------------------------------------------------
# date formatting helpers (original, unshifted space)
# ------------------------------------------------------------------------------------------
def f_mdyyyy(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def f_m_dd_yyyy(d: date) -> str:
    return f"{d.month}/{d.day:02d}/{d.year}"


def f_mmddyyyy(d: date) -> str:
    return f"{d.month:02d}/{d.day:02d}/{d.year}"


def f_mmddyy(d: date) -> str:
    return f"{d.month:02d}/{d.day:02d}/{d.year % 100:02d}"


def f_long(d: date) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def f_iso(d: date) -> str:
    return d.isoformat()


def f_dmon(d: date) -> str:
    return f"{d.day:02d}-{d.strftime('%b').upper()}-{d.year}"


def f_month(d: date) -> str:
    return f"{d.strftime('%B')} {d.year}"


def f_m_yyyy(d: date) -> str:
    return f"{d.month}/{d.year}"


def season_of(d: date) -> str:
    return {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer"}.get(d.month, "fall")


def rdt_two_digit(dt: datetime) -> str:
    """Report_Date_Time for Visit/Progress/H&P: mm/dd/yy hh:mm:ss (17 chars)."""
    return dt.strftime("%m/%d/%y %H:%M:%S")


def rdt_four_digit(dt: datetime) -> str:
    """Report_Date_Time for Discharge/Pathology/Radiology: m/d/yyyy h:mm:ss AM."""
    h = dt.hour % 12 or 12
    return f"{dt.month}/{dt.day}/{dt.year} {h}:{dt.minute:02d}:{dt.second:02d} {'AM' if dt.hour < 12 else 'PM'}"


def clock(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


# ------------------------------------------------------------------------------------------
# note container
# ------------------------------------------------------------------------------------------
@dataclass
class Note:
    table: str
    person: Person
    when: datetime
    description: str
    report_type: str
    status: str = "F"
    lines: list[str] = field(default_factory=list)
    annotations: list[dict] = field(default_factory=list)
    phi: list[tuple[str, str]] = field(default_factory=list)   # (category, value)
    report_number: str = ""
    extra_report_numbers: list[str] = field(default_factory=list)

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    def seed(self, category: str, value: str) -> str:
        self.phi.append((category, value))
        return value

    def ann(self, group: str, segments: list, items: list[dict]) -> str:
        """Render an annotated quote. segments: str literals and (slot_name, date_text, precision)."""
        rendered, segs, slots = [], [], {}
        for s in segments:
            if isinstance(s, tuple):
                name, text, precision = s
                rendered.append(text)
                segs.append({"slot": name})
                slots[name] = {"text": text, "precision": precision}
            else:
                rendered.append(s)
                segs.append(s)
        quote = "".join(rendered)
        self.annotations.append({"group": group, "quote": quote, "segments": segs, "slots": slots, "items": items})
        return quote

    def text(self) -> str:
        return CRLF.join(self.lines) + CRLF + REPORT_END


# ------------------------------------------------------------------------------------------
# headers
# ------------------------------------------------------------------------------------------
def banner(rng: random.Random) -> str:
    return ("***This report contains confidential information about a patient and is released for "
            f"authorized research use only, '{rng.randint(100000, 999999)}.txt'.")


def inpatient_header(n: Note, rng: random.Random, unit: str, site: str = "MGH") -> None:
    p = n.person
    mrn = n.seed("ID", p.mrns.get(site, p.mrn))
    last, first = n.seed("PATIENT", p.last.upper()), n.seed("PATIENT", p.first.upper())
    dob = n.seed("DOB", f_m_dd_yyyy(p.dob))
    town = n.seed("LOCATION", p.town)
    zipc = n.seed("LOCATION", f"{p.zip5}-{p.zip4}")
    acct = n.seed("ID", str(rng.randint(10_000_000, 99_999_999)))
    age = p.age_on(n.when.date())
    n.add(banner(rng),
          f"{site:<6}{unit:<32}  {last}, {first} {p.middle}",
          f"{age} Years {p.sex[0]}     MRN: {mrn}, DOB: {dob}, Adm: {f_m_dd_yyyy(n.when.date())}",
          f"{town:<24}MA {zipc}    Account #: {acct}",
          "")


def visit_header(n: Note, rng: random.Random, provider: str, department: str, variant: int) -> None:
    p = n.person
    last, first = n.seed("PATIENT", p.last), n.seed("PATIENT", p.first)
    mrn = n.seed("ID", p.mrns["MGH"])
    if variant == 0 and p.preferred:
        pref = n.seed("PATIENT", p.preferred)
        line2 = f"{last}, {first} “{pref}” (MR # {mrn}) DOB: {n.seed('DOB', f_mmddyyyy(p.dob))}"
    elif variant == 1:
        line2 = f"{last}, {first} (MRN {mrn}) DOB: {n.seed('DOB', f_mmddyyyy(p.dob))}"
    else:
        line2 = f"{last}, {first} {p.middle} (MR # {mrn})"
    n.add(banner(rng), line2, n.seed("STAFF", provider),
          f"{f_m_dd_yyyy(n.when.date())} {clock(n.when)}    {department}", "")


def radiology_header(n: Note, exam: str, reason: str) -> None:
    d = n.when
    n.add(exam, f"REASON FOR EXAM: {reason}",
          f"Exam Date: {f_dmon(d.date())} {d.strftime('%H:%M')}    Status: {'Final' if n.status == 'F' else 'Preliminary'}", "")


def pathology_header(n: Note, accession: str, kind: str) -> None:
    n.add(f"Accession Number: {n.seed('ID', accession)}        Report Status: Final",
          f"Type: {kind}",
          n.seed("LOCATION", "410 Pemberton Hollow Street, Boston MA 02199"),
          f"Phone: {n.seed('PHONE', '(617) 555-0100')}  Fax: {n.seed('PHONE', '(617) 555-0101')}",
          "")


# ------------------------------------------------------------------------------------------
# narrative building blocks (deterministic per person → copied forward verbatim)
# ------------------------------------------------------------------------------------------
def relapse_expr(r: Relapse, style: str) -> tuple[str, str]:
    """(text, precision) for a relapse onset in a given style."""
    if r.precision == "season":
        return f"{season_of(r.onset)} {r.onset.year}", "season"
    if r.precision == "year":
        return str(r.onset.year), "year"
    if r.precision == "month":
        if style == "dx":
            return f_m_yyyy(r.onset), "month"
        if style == "short":
            return f"{r.onset.month}/{r.onset.year % 100:02d}", "month"
        if style == "apos":
            return f"{r.onset.strftime('%B')} '{r.onset.year % 100:02d}", "month"
        return f_month(r.onset), "month"
    if style == "dx":
        return f_m_yyyy(r.onset), "month"
    if style == "iso":
        return f_iso(r.onset), "day"
    return f_mdyyyy(r.onset), "day"


def relapse_items(r: Relapse, slot: str, with_course: bool) -> list[dict]:
    item = {"date_onset_slot": slot, "relapse_phenotype": [str(c) for c in r.phenotype]}
    if with_course:
        item["admission"] = str(r.admitted)
        item["acute_rx"] = [str(c) for c in r.acute_rx]
    return [item]


def origin_paragraph(n: Note) -> str:
    p = n.person
    out = (f"{p.title} {n.seed('PATIENT', p.last)} is followed in the neuroimmunology clinic for {p.dx_phrase}. "
           f"{p.pronoun.capitalize()} was first evaluated at {n.seed('HOSPITAL', OUTSIDE_HOSPITALS[int(p.key[1]) % 3])} "
           f"and was referred by {n.seed('STAFF', 'Dr. Pennywhistle')} at {n.seed('ORGANIZATION', PCP_PRACTICE)}. ")
    if p.otherdx == 1:
        out += n.ann("diagnosis", ["Prior to the correct diagnosis, she was told she had multiple sclerosis"],
                     [{"otherdx": "1"}]) + ". "
    elif p.otherdx == 3:
        out += n.ann("diagnosis", ["Earlier episodes were labelled relapsing optic neuritis"], [{"otherdx": "3"}]) + ". "
    elif p.otherdx == 4:
        out += n.ann("diagnosis", [f"Her myelitis was initially labelled {p.dx_other}"],
                     [{"otherdx": "4", "dx_other": p.dx_other}]) + ". "
    dx_style = "month" if p.dx_est >= 2 else "day"
    dtext = f_month(p.dx_date) if dx_style == "month" else f_mdyyyy(p.dx_date)
    out += n.ann("diagnosis", [f"{p.pronoun.capitalize()} was diagnosed with {p.dx_word} on ", ("dx", dtext, dx_style)],
                 [{"date_nmo_diagnosis_slot": "dx", "dx_category": p.dx_category}]) + "."
    return out


def relapse_paragraph(n: Note, r: Relapse, style: str = "narrative") -> str:
    p = n.person
    expr, prec = relapse_expr(r, "apos" if (p.key == "P3" and style == "alt") else "narrative")
    lead = "On " if prec == "day" else "In "
    q = n.ann("relapse", [lead, ("onset", expr, prec), f" {p.pronoun} developed {r.pheno_text}"],
              relapse_items(r, "onset", with_course=False))
    out = q + ". "
    if r.admitted and r.admit and r.discharge:
        out += n.ann("relapse", [f"{p.pronoun.capitalize()} was admitted from ", ("adm", f_mdyyyy(r.admit), "day"),
                                 " to ", ("dis", f_mdyyyy(r.discharge), "day"), f" and treated with {r.acute_text}"],
                     [{"date_onset_slot": None, "admission": "1", "date_admission_slot": "adm",
                       "date_discharge_slot": "dis", "acute_rx": [str(c) for c in r.acute_rx],
                       "relapse_phenotype": [str(c) for c in r.phenotype]}]) + ". "
    elif r.admitted:
        out += f"{p.pronoun.capitalize()} was hospitalized at {n.seed('HOSPITAL', r.hospital or OUTSIDE_HOSPITALS[2])} and treated with {r.acute_text}. "
    else:
        out += n.ann("relapse", [f"{p.pronoun.capitalize()} was treated as an outpatient with {r.acute_text}"],
                     [{"admission": "0", "acute_rx": [str(c) for c in r.acute_rx],
                       "relapse_phenotype": [str(c) for c in r.phenotype], "date_onset_slot": None}]) + ". "
    if r.mri_text:
        out += f"MRI at that time showed {r.mri_text}."
    return out.strip()


def dx_line(n: Note, d: date) -> str:
    p = n.person
    parts = []
    for r in [r for r in p.relapses if r.onset <= d]:
        expr, prec = relapse_expr(r, "short" if p.key == "P3" else "dx")
        parts.append(n.ann("relapse", [f"{r.pheno_short} ", ("onset", expr, prec)],
                           relapse_items(r, "onset", with_course=False)))
    dx_txt = f_m_yyyy(p.dx_date)
    q = n.ann("diagnosis", [f"Dx: {p.dx_phrase} (diagnosed ", ("dx", dx_txt, "month"), ")"],
              [{"date_nmo_diagnosis_slot": "dx", "dx_category": p.dx_category}])
    return f"{q}. Attacks: {'; '.join(parts) if parts else 'none'}."


def tx_line(n: Note, d: date) -> str:
    p = n.person
    parts = []
    for t in [t for t in p.treatments if t.start <= d]:
        code = str(t.code)
        if t.stop and t.stop <= d:
            parts.append(n.ann("treatment", [f"{t.name} ", ("s", f_m_yyyy(t.start), "month"), "-",
                                             ("e", f_m_yyyy(t.stop), "month"), f" ({t.reason_text})"],
                               [{"maintenance_therapy": code, "event": "start", "event_date_slot": "s"},
                                {"maintenance_therapy": code, "event": "stop", "event_date_slot": "e",
                                 "reason_for_stopping_tx": str(t.reason_stop)}]))
        else:
            parts.append(n.ann("treatment", [f"{t.name} since ", ("s", f_m_yyyy(t.start), "month")],
                               [{"maintenance_therapy": code, "event": "start", "event_date_slot": "s"},
                                {"maintenance_therapy": code, "event": "ongoing"}]))
    return "Tx: " + ("; ".join(parts) if parts else "no maintenance therapy yet") + "."


def current_treatment(p: Person, d: date) -> Treatment | None:
    cur = [t for t in p.treatments if t.start <= d and (t.stop is None or t.stop > d)]
    return cur[-1] if cur else None


def med_table(p: Person, d: date) -> list[str]:
    rows = [("Medication", "Sig", "Start Date")]
    cur = current_treatment(p, d)
    if cur:
        rows.append((cur.name.capitalize(), cur.dose_text, f_mmddyy(cur.start)))
    rows += [("Vitamin D3", "2000 units PO daily", "01/15/15"),
             ("Gabapentin", "300 mg PO TID; may take 1/2 tab", "06/02/12"),
             ("Acetaminophen", "650 mg PO q6h PRN", "")]
    return [f"{a:<22}{b:<40}{c}" for a, b, c in rows]


def exam_block(p: Person, d: date, rng: random.Random) -> list[str]:
    sev = len([r for r in p.relapses if r.onset <= d])
    va_l = "20/400" if any(1 in r.phenotype for r in p.relapses if r.onset <= d) else "20/25"
    return [
        "Neurologic examination",
        f"CNP: VA 20/20 OD, {va_l} OS (pinhole no improvement); CF at 3 ft in the left temporal field. "
        "Left RAPD. EOM full.",
        f"Motor: Normal bulk. Strength 5/5 throughout except {'4+/5' if sev > 1 else '5-/5'} left hip flexion "
        "and 4/5 left ankle dorsiflexion.",
        "Reflexes: 2+ and symmetric in the arms; 3+ at the knees; biceps 2/4 bilaterally. Toes downgoing.",
        f"Sensation: Reduced vibration at the toes; sensory level {'T6' if sev > 1 else 'absent'}.",
        "Coordination: No dysmetria. Gait: Mildly spastic; walks unaided 500 m.",
        f"EDSS {min(2.0 + sev, 6.5):.1f}",
    ]


def boilerplate_block(n: Note, d: date, rng: random.Random) -> list[str]:
    b1 = d - timedelta(days=rng.randint(60, 120))
    b2 = b1 - timedelta(days=rng.randint(60, 200))
    return [
        "BP Readings from Last 3 Encounters:",
        f"{f_mmddyy(d)}   118/76",
        f"{f_mmddyy(b1)}   124/80",
        f"{f_mmddyy(b2)}   121/79",
        "",
        "Immunizations",
        f"TRIVALENT PF                  {f_mmddyy(d - timedelta(days=200))}",
        f"PCV20                         {f_mmddyy(d - timedelta(days=700))}",
        "",
        "ESTABLISHED HIGH MDM 40 MIN",
        "",
        "Encounter-Level Documents:",
        "  After Visit Summary (signed)",
        "Order-Level Documents:",
        "  There are no order-level documents.",
        "Patient-Level Documents:",
        "  Advance care planning (scanned)",
    ]


def signoff(n: Note, who: str) -> list[str]:
    signed = n.when + timedelta(hours=5)
    return ["", f"Electronically signed by {n.seed('STAFF', who)} on {f_mdyyyy(signed.date())} {clock(signed)}",
            f"cc: {n.seed('STAFF', 'Dr. Ambrose Pennywhistle')}, {n.seed('ORGANIZATION', PCP_PRACTICE)}"]
