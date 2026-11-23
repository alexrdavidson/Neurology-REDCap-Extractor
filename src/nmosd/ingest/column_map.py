"""Map physical table/column names in an RPDR export to logical names.

Defaults match the confirmed schema of the real export (BUILD_PLAN §1, metadata-only inspection
09.11.26) and also accept the classic RPDR flat-file names (Vis, Prg, Rad, ...). Any table that has
a `Report_Text` column is treated as a note table, so new note tables are picked up automatically;
any other table with an EMPI column is copied as a structured table (e.g. Med, Lab, Mrn, Con).
"""

from __future__ import annotations

import re

NOTE_TABLE_ALIASES: dict[str, list[str]] = {
    "VisitNote": ["VisitNote", "Vis", "VisitNotes"],
    "ProgressNote": ["ProgressNote", "Prg", "ProgressNotes"],
    "Radiology": ["Radiology", "Rad"],
    "Pathology": ["Pathology", "Pat"],
    "Discharge_Summaries": ["Discharge_Summaries", "Dis", "DischargeSummaries"],
    "HistoryAndPhysical": ["HistoryAndPhysical", "Hnp", "HandP"],
    "Cardiology": ["Cardiology", "Car"],
    "Endoscopy": ["Endoscopy", "End"],
    "Operative": ["Operative", "Opn"],
    "Microbiology": ["Microbiology", "Mic"],
    "PulmonaryFunction": ["PulmonaryFunction", "Pul"],
}
STRUCTURED_TABLE_ALIASES: dict[str, list[str]] = {
    "Demographics": ["Demographics", "Dem"],
    "Medications": ["Medications", "Med"],
    "Labs": ["Labs", "Lab"],
    "LabNotes": ["LabNotes", "Lno"],
    "Mrn": ["Mrn", "MRN"],
    "Contact": ["Contact", "Con", "ContactInformation"],
    "Encounters": ["Encounters", "Enc"],
}
SKIP_TABLE_PATTERNS = [re.compile(r"^\s*<--"), re.compile(r"^MSys", re.I), re.compile(r"^~")]

NOTE_COLUMNS: dict[str, list[str]] = {
    "empi": ["EMPI"],
    "report_number": ["Report_Number"],
    "report_datetime": ["Report_Date_Time"],
    "report_description": ["Report_Description"],
    "report_status": ["Report_Status"],
    "report_type": ["Report_Type"],
    "text": ["Report_Text"],
}
DEMOGRAPHICS_COLUMNS: dict[str, list[str]] = {
    "empi": ["EMPI"],
    "dob": ["Date_of_Birth"],
    "age": ["Age"],
    "sex_at_birth": ["Sex_At_Birth"],
    "gender_legal_sex": ["Gender_Legal_Sex"],
    "race1": ["Race1"],
    "race2": ["Race2"],
    "race_group": ["Race_Group"],
    "ethnic_group": ["Ethnic_Group"],
    "vital_status": ["Vital_status", "Vital_Status"],
    "date_of_death": ["Date_Of_Death", "Date_of_Death"],
    "zip": ["Zip_code", "Zip"],
    "language": ["Language"],
    "language_group": ["Language_group"],
    "marital_status": ["Marital_status"],
    "religion": ["Religion"],
    "veteran": ["Is_a_veteran"],
    "country": ["Country"],
    "gender_identity": ["Gender_Identity"],
}
# Columns that identify a person and must never reach de-identified output.
IDENTIFIER_COLUMNS = {
    "empi", "epic_pmrn", "mrn", "mrn_type", "enterprise_master_patient_index", "last_name", "first_name",
    "middle_name", "address1", "address2", "city", "state", "zip", "zip_code", "home_phone", "day_phone",
    "email_address", "ssn", "date_of_birth", "provider", "clinic", "hospital", "accession", "encounter_number",
    "report_number", "account_number",
}


def resolve(actual_cols: list[str], mapping: dict[str, list[str]]) -> dict[str, str | None]:
    """logical -> actual column name (case-insensitive), None when absent."""
    lower = {c.lower(): c for c in actual_cols}
    return {k: next((lower[a.lower()] for a in alts if a.lower() in lower), None) for k, alts in mapping.items()}


def classify_table(name: str, columns: list[str]) -> tuple[str, str]:
    """Return (kind, logical_name) with kind in {'note', 'structured', 'skip'}."""
    if any(p.search(name) for p in SKIP_TABLE_PATTERNS):
        return "skip", name
    for logical, aliases in NOTE_TABLE_ALIASES.items():
        if name.lower() in {a.lower() for a in aliases}:
            return "note", logical
    for logical, aliases in STRUCTURED_TABLE_ALIASES.items():
        if name.lower() in {a.lower() for a in aliases}:
            return "structured", logical
    lower = {c.lower() for c in columns}
    if "report_text" in lower:
        return "note", name
    if "empi" in lower:
        return "structured", name
    return "skip", name
