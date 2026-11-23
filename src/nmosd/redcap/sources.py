"""Where each REDCap field's proposal comes from (16.11.26: every field of the codebook, 1-303).

* notes: a field group of config/fields/pilot_fields.yaml reads it from the notes (hand-made or `redcap_fields:`);
* table: the RPDR Demographics table (sex, race, ethnicity, living/deceased);
* derived: computed by the reconciler from other proposals (e.g. the number of relapses, the DMT at a relapse);
* redcap: REDCap fills it (calculated, descriptive text, form status, auto-populated);
* identifier: record ID (the study ID), MRN and date of birth, added by export / re-identification only;
* reviewer: nothing in the notes can answer it here (e.g. the investigator's initials); the reviewer enters it.
  The hospital fields (hx_opcare, nr_hosp, nr_hosp_2) are reviewer fields only when de-identification took the
  hospital names out of the notes; otherwise the `hospital` field group reads them (`hospital_names_kept`).

The review app shows every field with its codebook number, and the fields not filled here with the reason.
Drug codes differ between fields (maintenance_therapy 1 = Rituximab, dmt_relapse 2 = Rituximab, ...): the maps
below translate CODE to CODE, and tests check them against the labels.
"""

from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache

from nmosd.config import load_rules
from nmosd.redcap.dictionary import Dictionary

# Forms that repeat (the 10.11.26 profile of the project's REDCap export listed every form below except imaging,
# which had no rows in that sample; its date field carries @INSTANCETABLE_LABEL, used only by repeating forms).
# Settings `export.repeating_instruments` overrides this list.
NON_REPEATING = {"demographics"}

# Instrument names as the codebook's instrument table gives them (the dictionary CSV has only the form names).
FORM_LABELS = {"demographics": "Demographics", "relapse": "Relapse",
               "non_relapse_hospitalization": "Non relapse hospitalization", "imaging": "Imaging",
               "treatment": "Treatment", "cd1920_counts": "CD19/20 counts", "immunoglobulin_levels": "Immunoglobulin levels",
               "edss": "Disability", "ivig_for_hypogam": "Immunoglobulins for hypogam",
               "infections_requiring_antibiotics": "Infections requiring antibiotics",
               "antibody_lab_data": "Antibody lab data", "last_follow_up": "Last follow up"}
TABLE_FIELDS = {"sex", "race", "ethnicity", "living_deceased"}
IDENTIFIER_FIELDS = {"record_id": "the study ID (export)", "mrn": "re-identification", "dob": "re-identification"}
REVIEWER_FIELDS = {
    "initials": "the investigator's initials: enter in REDCap",
    "reviewer": "the REDCap reviewer list: enter in REDCap",
    "rtx_ever": "auto-populated by REDCap (do not edit)",
    "changes_fu": "compared with the last REDCap update: the reviewer decides",
}
# Hospital names (18.11.26). De-identification replaces them with [HOSPITAL_nn] (`deid.tokenise_hospitals`), so these
# fields are read from the notes (field group `hospital`) only when the notes still name hospitals: notes kept as they
# are (`deid.enabled: false`) or de-identified with `deid.tokenise_hospitals: false`. Otherwise the reviewer enters them.
HOSPITAL_GROUP = "hospital"
HOSPITAL_NAME_FIELDS = ("hx_opcare", "nr_hosp_2", "nr_hosp")
NO_HOSPITAL_NAMES = "hospital names are not kept in the de-identified notes: enter in REDCap"
DERIVED_FIELDS = {
    # demographics
    "last_fu": "the last contact found for the diagnosis check",
    "nmo_criteria": "the diagnosis check's proposal (NMOSD)",
    "relapse_type_diagnosis": "the phenotypes of the attacks up to the diagnosis date",
    "dx_other_treatment": "maintenance treatments started before the diagnosis date",
    "rx_before_nmo": "maintenance treatments started before the diagnosis date",
    "other_treatment_name": "maintenance treatments started before the diagnosis date",
    "aqp4": "the antibody results", "mog": "the antibody results",
    "rh_number": "the number of attacks with an admission", "nr_number": "the number of other admissions",
    "relapse_number": "the number of attacks",
    "dmt_lastfu": "the maintenance treatments still ongoing", "dmt_other": "the maintenance treatments still ongoing",
    "mrs_lastfu": "the mRS of the last follow-up disability entry",
    "loc_nmo_total": "the phenotypes of all attacks",
    "weight": "the vitals nearest the diagnosis date (text patterns)",
    "height": "the vitals nearest the diagnosis date (text patterns)",
    # relapse
    "dmt_relapse": "the maintenance treatments ongoing at the attack",
    "dmt_relapse_other": "the maintenance treatments ongoing at the attack",
    "cd19": "the CD19 count in the 90 days before the attack", "cd20": "the CD20 count in the 90 days before the attack",
    "alc": "the lymphocyte count in the 90 days before the attack",
    "adverse_event": "whether acute-treatment side effects were found",
    "dmt_change": "maintenance treatments started in the 90 days after the attack",
    "new_dmt": "maintenance treatments started in the 90 days after the attack",
    "dmt_change_other": "maintenance treatments started in the 90 days after the attack",
    # non-relapse hospitalization
    "nr_dmt": "the maintenance treatments ongoing at admission", "nr_dmt_other": "the maintenance treatments ongoing at admission",
    "nr_dmt_change": "maintenance treatments started during or within 30 days of the stay",
    "nr_dmt_discharge": "maintenance treatments started during or within 30 days of the stay",
    "nr_dmt_discharge_other": "maintenance treatments started during or within 30 days of the stay",
    # imaging
    "mri_available_info": "the kind of document the MRI was read from",
    "mri_relapse_number": "the attack nearest the MRI",
    # treatment
    "still_on_therapy": "the most recent notes that mention the drug",
    "reason_maintenance": "the order of the treatment courses (start / restart / addition / change)",
    "maintenance_start": "the reason in the notes and the attacks before the start",
    "maintenance_restart": "the reason in the notes", "maintenance_add": "the reason in the notes",
    "maintenance_change": "the reason in the notes",
    "relapse_number_maintenance": "the attack before the start",
    # cd19/20, immunoglobulins (from the `labs` results)
    "cd19_yn": "the lab results", "cd19_abs_yn": "the lab results", "cd20_yn": "the lab results",
    "cd20_abs_yn": "the lab results",
    # edss
    "date_of_assessment": "the date of the examination note", "timing": "the treatment start/stop or last follow-up",
    # ivig
    "ivig_yn": "the most recent notes that mention immunoglobulin replacement",
    "date_starting_ivig": "the immunoglobulin replacement mentions",
    "date_stopping_ivig": "the immunoglobulin replacement mentions",
    # last follow-up
    "last_follow_up_date": "the last contact found for the diagnosis check",
    "is_the_patient_still_prima": "the date of the last clinical note",
    "has_your_patient_experienc": "the conditions found in the notes (check each is treatment-related)",
}

# maintenance_therapy code (#157) → code of the same drug in other fields (None: no such choice)
_MT_TO_DMT17 = {"1": "2", "2": "3", "3": "4", "4": "5", "5": "6", "6": "7", "7": "8", "8": "9", "9": "10",
                "10": "11", "11": "12", "12": "13", "13": "14", "14": "15", "15": "16", "16": "17"}
DRUG_CODE_MAPS = {
    "dmt_relapse": _MT_TO_DMT17,          # 1 None, then the 16 drugs, 17 Prednisone
    "nr_dmt": _MT_TO_DMT17,
    "new_dmt": {c: c for c in _MT_TO_DMT17},   # same order as maintenance_therapy
    "nr_dmt_discharge": {k: v for k, v in _MT_TO_DMT17.items() if k != "16"},   # no prednisone choice
    "dmt_lastfu": {"1": "2", "2": "3", "3": "4", "4": "5", "5": "6", "6": "7", "7": "8", "8": "9", "13": "10",
                   "14": "11", "9": "12", "10": "12", "11": "12", "12": "12", "15": "12"},   # steroid-sparing only
    "rx_before_nmo": {"1": "2", "2": "3", "7": "4", "13": "5", "9": "6", "10": "7", "11": "8", "12": "10",
                      "14": "13", "8": "14", "3": "12", "4": "12", "5": "12", "6": "12", "15": "12", "16": "12"},
}
OTHER_CODE = {"dmt_relapse": "16", "nr_dmt": "16", "new_dmt": "15", "nr_dmt_discharge": "16", "dmt_lastfu": "12",
              "rx_before_nmo": "12"}
# relapse_phenotype (#48) → localisation codes of relapse_type_diagnosis (#19) and loc_nmo_total (#38)
PHENOTYPE_TO_LOCATION = {"1": "1", "2": "2", "3": "4", "4": "3", "5": "5", "6": "6", "7": "7"}


def map_drug(field: str, maintenance_code: str) -> str | None:
    return DRUG_CODE_MAPS[field].get(str(maintenance_code))


@lru_cache(maxsize=1)
def _note_fields() -> dict[str, str]:
    """REDCap field → the field group that reads it from the notes."""
    from nmosd.redcap.schemas import GROUP_ITEMS

    groups = load_rules("fields/pilot_fields.yaml")["groups"]
    out: dict[str, str] = {}
    for g, spec in groups.items():
        for name in spec.get("redcap_fields", []):
            out.setdefault(name, g)
        for _key, _kind, src in GROUP_ITEMS.get(g, []):
            if src:
                out.setdefault(src, g)
    # the admission groups read non-relapse fields; attacks get the same answers in the relapse fields
    for f in ("admission", "date_admission", "date_discharge", "icu_rh", "adverse_events_hosp", "infection_hosp",
              "infection_organism", "rehab", "rehab_admit", "rehab_discharge", "mrs_adm", "mrs_disch"):
        out.setdefault(f, "admission")
    out.update({"date_cd": "labs", "cd19_abs": "labs", "cd19_percent": "labs", "cd20_abs": "labs",
                "cd20_percent": "labs", "date_igg": "labs", "igg": "labs", "igm": "labs", "iga": "labs"})
    for f in ("date_mog_estimate", "mog_assay_date", "mog_test_positive", "aqp4_assay_date", "aqp4_assay_status",
              "aqp4_assay_titer", "titer", "aqp4_assay_type", "aqp4_test_location", "aqp4_test_location_other",
              "mog_assay_location", "mog_assay_location_other", "mog_assay_specimen"):
        out[f] = "antibody"
    out["date_stopping_maint"] = out["date_stopping_estimate"] = "treatment"
    for f in HOSPITAL_NAME_FIELDS:   # nr_hosp_2: an attack's admission gets its stay's nr_hosp
        out[f] = HOSPITAL_GROUP
    return out


def hospital_names_kept(manifest: dict | None) -> bool:
    """True when the notes still name hospitals (from the deid database's manifest): kept as they are
    (`deidentified: false`), or de-identified with `tokenise_hospitals: false`. No manifest: False."""
    m = manifest or {}
    return not m.get("deidentified", True) or not m.get("tokenise_hospitals", True)


def source(dic: Dictionary, name: str, hospital_names: bool = False) -> tuple[str, str]:
    """(kind, explanation) for a REDCap field; kind is notes | table | derived | redcap | identifier | reviewer.
    hospital_names: the notes still name hospitals (`hospital_names_kept`), so the hospital fields come from them."""
    f = dic[name]
    if name in IDENTIFIER_FIELDS:
        return "identifier", IDENTIFIER_FIELDS[name]
    if f.is_calc:
        return "redcap", "calculated by REDCap"
    if f.is_descriptive:
        return "redcap", "text shown on the REDCap form"
    if f.is_form_status:
        return "redcap", "form status (exported as Incomplete)"
    if name in HOSPITAL_NAME_FIELDS:
        return ("notes", f"the notes (field group {HOSPITAL_GROUP})") if hospital_names else ("reviewer", NO_HOSPITAL_NAMES)
    if name in REVIEWER_FIELDS:
        return ("redcap" if name == "rtx_ever" else "reviewer"), REVIEWER_FIELDS[name]
    if name in TABLE_FIELDS:
        return "table", "the RPDR Demographics table"
    if name in DERIVED_FIELDS:
        return "derived", DERIVED_FIELDS[name]
    if name in _note_fields():
        return "notes", f"the notes (field group {_note_fields()[name]})"
    est_of = next((d for d in dic.fields.values() if name in d.piped), None)
    if est_of is not None:
        return source(dic, est_of.name)[0], f"the precision of {est_of.name}"
    return "reviewer", "not read from the notes yet: enter in REDCap"


def review_fields(dic: Dictionary, hospital_names: bool = False) -> OrderedDict[str, list[str]]:
    """Instrument → the fields a reviewer confirms (dictionary order), for every form of the dictionary."""
    out: OrderedDict[str, list[str]] = OrderedDict()
    for form, names in dic.forms.items():
        out[form] = [n for n in names if source(dic, n, hospital_names)[0] in ("notes", "table", "derived")]
    return out


def not_filled(dic: Dictionary, form: str, hospital_names: bool = False) -> list[tuple[int, str, str]]:
    """(number, field, why) for a form's fields that the review does not propose."""
    return [(dic[n].number, n, source(dic, n, hospital_names)[1]) for n in dic.forms.get(form, [])
            if source(dic, n, hospital_names)[0] not in ("notes", "table", "derived")]


def form_label(dic: Dictionary, form: str) -> str:
    """'Relapse (#45-86)': the instrument's name and the codebook numbers of its fields."""
    nums = [dic[n].number for n in dic.forms.get(form, [])]
    name = FORM_LABELS.get(form, form.replace("_", " ").capitalize())
    return f"{name} (#{min(nums)}–{max(nums)})" if nums else name


def repeating_forms(dic: Dictionary, settings=None) -> set[str]:
    configured = settings.get("export.repeating_instruments") if settings is not None else None
    if configured:
        return set(configured)
    return {f for f in dic.forms if f not in NON_REPEATING}
