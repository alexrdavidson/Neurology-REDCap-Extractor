"""Generate stage-1 evidence schemas (Pydantic v2 → JSON schema for Ollama `format=`) per field group.

Coded fields are `Literal[<REDCap codes>, "UNK", "not_established"]` (codes, never labels). Checkbox fields
are lists of codes. Dates are 'YYYY-MM-DD' strings (or 'not_established') with a separate estimate code;
partial dates are normalised after parsing, not rejected, so one odd date never voids a whole response.
"""

from __future__ import annotations

import copy
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from nmosd.config import load_rules
from nmosd.redcap.dictionary import Dictionary, load_dictionary

NE = "not_established"
EXTRA = ("UNK", NE)
EVENTS = ("start", "stop", "ongoing", "planned", "considered", NE)
RESULTS = ("positive", "negative", NE)
# The patient's current diagnosis as one note states it (not a REDCap field; feeds the diagnosis check that
# opens every review, config/eligibility.yaml). "unclear" is a reviewer outcome only, never extracted.
DX_CATEGORIES = ("nmosd", "mogad", "ms", "ae", "sc_infarct", "other")
DX_DESCRIPTION = ("The patient's diagnosis as this note states it NOW: nmosd = NMOSD/NMO/Devic's (AQP4-IgG positive "
                  "or seronegative); mogad = MOG antibody-associated disease; ms = multiple sclerosis; ae = autoimmune "
                  "encephalitis; sc_infarct = spinal cord infarct; other = another neurological or autoimmune "
                  "diagnosis; not_established if the note does not state it. One item per diagnosis when the patient "
                  "has several. Possible/suspected/rule-out diagnoses: hypothetical=true; 'not X': negated=true.")

# stage-1 item layout per field group: (item key, kind, dictionary field used for codes/labels)
GROUP_ITEMS: dict[str, list[tuple[str, str, str | None]]] = {
    "diagnosis": [("dx_category", "dxcat", None),
                  ("date_nmo_diagnosis", "date", "date_nmo_diagnosis"),
                  ("date_nmo_estimate", "estimate", "date_nmo_estimate"),
                  ("otherdx", "code", "otherdx"), ("dx_other", "text", "dx_other")],
    "relapse": [("date_onset", "date", "date_onset"), ("date_attack_estimate", "estimate", "date_attack_estimate"),
                ("relapse_phenotype", "multi", "relapse_phenotype"), ("admission", "code", "admission"),
                ("date_admission", "date", "date_admission"), ("date_discharge", "date", "date_discharge"),
                ("acute_rx", "multi", "acute_rx"), ("mri_relapse", "code", "mri_relapse"),
                # 16.11.26: the codebook's attack details (#47, #49, #51-54), asked in the same call
                ("clinical_confirm", "code", "clinical_confirm"), ("trigger", "code", "trigger"),
                ("gad_relapse", "code", "gad_relapse"), ("let", "code", "let"),
                ("noncore_phenotype", "text", "noncore_phenotype"), ("firstrelapsestate", "code", "firstrelapsestate")],
    "treatment": [("maintenance_therapy", "code", "maintenance_therapy"),
                  ("other_maintenance_tx", "text", "other_maintenance_tx"), ("event", "event", None),
                  ("event_date", "date", "date_starting_maint"),
                  ("event_date_estimate", "estimate", "date_maintenance_estimate"),
                  ("reason_for_stopping_tx", "code", "reason_for_stopping_tx"),
                  ("other_stopping", "text", "other_stopping"),
                  ("reason_for_stopping_therap", "text", "reason_for_stopping_therap")],
    # 16.11.26: assay method, laboratory, titer and specimen (codebook #281-#293); titer as written, mapped to the
    # AQP4 or MOG titer codes by the reconciler
    "antibody": [("type_test", "code", "type_test"), ("result", "result", None),
                 ("assay_date", "date", "aqp4_assay_date"), ("assay_date_estimate", "estimate", "date_mog_estimate"),
                 ("assay_method", "code", "aqp4_assay_type"), ("lab", "code", "aqp4_test_location"),
                 ("lab_other", "text", "aqp4_test_location_other"), ("titer", "text", None),
                 ("specimen", "code", "mog_assay_specimen")],
    # note check of the structured Demographics values (see STRUCTURED_SOURCES; not STRUCTURED_ONLY)
    "demographics": [("sex", "code", "sex"), ("race", "code", "race"), ("living_deceased", "code", "living_deceased")],
    # follow-up contacts given in patient messages (diagnosis check only; not REDCap fields)
    "followup": [("visit_type", "visittype", None), ("visit_date", "date", None)],
    # conditions in the medical history / problem list → the REDCap `comorbidities` checkbox (13.11.26)
    "comorbidity": [("condition", "text", None), ("comorbidities", "comorbcat", "comorbidities"),
                    ("autoimmune", "code", "autoimmune")],
}
VISIT_TYPES = ("infusion", "appointment", "test", "other")
NO_CATEGORY = "no_category"   # a real condition outside the REDCap comorbidity categories (kept for the reviewer)
NMOSD_RELATED = "nmosd_related"   # NMOSD itself or one of its attacks (e.g. transverse myelitis): listed, never ticked
ITEM_DESCRIPTIONS = {
    "visit_date": "Date of the infusion, injection, appointment or test that the message schedules, confirms or "
                  "reminds the patient about.",
    "visit_type": "infusion = infusion or injection of a treatment; appointment = clinic or video visit; test = MRI, "
                  "blood test or other investigation; other.",
    "condition": "The condition's name as the note writes it, without ICD codes or dates, e.g. 'Migraine', 'PTSD', "
                 "'Uterine leiomyoma'.",
    "titer": "The titer as written, e.g. '1:100' or '1:1000'; '' if none is given.",
    "other_maintenance_tx": "Name of the drug when maintenance_therapy is 15 (Other); '' otherwise.",
}
# REDCap field → de-identified `patients` column it is mapped from (no LLM); the notes are checked too, except
# for STRUCTURED_ONLY fields.
STRUCTURED_SOURCES = {"sex": "sex_at_birth", "race": "race1", "ethnicity": "ethnic_group",
                      "living_deceased": "vital_status"}
# Used when the column above is empty or has a value with no REDCap code; the review says so (16.11.26).
STRUCTURED_FALLBACKS = {"sex": ("gender_legal_sex",), "race": ("race_group",)}
# The RPDR Demographics column names, for the reviewer.
SOURCE_COLUMN_NAMES = {"sex_at_birth": "Sex_At_Birth", "gender_legal_sex": "Gender_Legal_Sex", "race1": "Race1",
                       "race_group": "Race_Group", "ethnic_group": "Ethnic_Group", "vital_status": "Vital_status",
                       "date_of_death": "Date_Of_Death"}


def _structured_only() -> frozenset[str]:
    """Fields taken from the structured Demographics table only (`note_check: false` in the `structured:` section of
    config/fields/pilot_fields.yaml): not searched for in the notes, no note evidence expected. 13.11.26 ethnicity;
    16.11.26 all four (user: the demographics come from a standardized database, the RPDR Demographics table)."""
    spec = load_rules("fields/pilot_fields.yaml").get("structured", {})
    return frozenset(f for f in STRUCTURED_SOURCES if spec.get(f, {}).get("note_check", True) is False)


STRUCTURED_ONLY = _structured_only()
UNSUPPORTED_KEYS = {"pattern", "format", "default", "examples", "$schema", "additionalProperties"}


def _codes_lit(codes: list[str]) -> Any:
    return Literal[tuple(dict.fromkeys([*codes, *EXTRA]))]  # type: ignore[valid-type]


def field_context(dic: Dictionary, name: str | None, codes: bool = True) -> str:
    """What the codebook says about a field, for the model: number, label, field note, when REDCap asks it, and
    the codes with their labels (the user asked, 16.11.26, that the codebook's label and note guide every answer)."""
    if not name or name not in dic:
        return ""
    f = dic[name]
    out = f"REDCap #{f.number} {f.clean_label or name}".rstrip()
    if f.note:
        out += f" (codebook note: {f.note})"
    if f.branching:
        out += f" [REDCap asks this only if {f.branching}]"
    if codes and f.choices:
        out += " Codes: " + "; ".join(f"{c}={lab}" for c, lab in f.choices.items())
    return out


_describe = field_context


# ------------------------------------------------------------------------------------------
# Field groups defined in config/fields/pilot_fields.yaml by REDCap field names (`redcap_fields:`), 16.11.26.
# Item keys are the REDCap field names; kinds come from the dictionary (date → date + its paired estimate radio,
# checkbox → list of codes, radio/dropdown/yesno → one code, text/notes/numbers → text). `extra_items:` adds keys
# that are not REDCap fields (e.g. `for_attack`), with their own options.
# ------------------------------------------------------------------------------------------
HAND_MADE_DATE_KEYS = {"date_nmo_diagnosis": "date_nmo_estimate", "date_onset": "date_attack_estimate",
                       "event_date": "event_date_estimate", "assay_date": "assay_date_estimate",
                       "date_admission": None, "date_discharge": None, "visit_date": None}
HAND_MADE_ANCHORS = ("date_onset", "event_date", "assay_date", "date_nmo_diagnosis", "visit_date")


@lru_cache(maxsize=1)
def _default_dic() -> Dictionary:
    from nmosd.redcap.dictionary import default_dictionary_path

    return load_dictionary(default_dictionary_path())


def _group_spec(group: str) -> dict:
    return load_rules("fields/pilot_fields.yaml")["groups"].get(group, {})


def is_generic(group: str) -> bool:
    return group not in GROUP_ITEMS and "redcap_fields" in _group_spec(group)


def extra_item(group: str, key: str) -> dict:
    return next((x for x in _group_spec(group).get("extra_items", []) if x["key"] == key), {})


@lru_cache(maxsize=64)
def group_items(group: str) -> list[tuple[str, str, str | None]]:
    """(item key, kind, dictionary field) per item key of a field group."""
    if group in GROUP_ITEMS:
        return GROUP_ITEMS[group]
    spec, dic = _group_spec(group), _default_dic()
    out: list[tuple[str, str, str | None]] = [(x["key"], x.get("kind", "enum"), x.get("source")) for x in
                                              spec.get("extra_items", [])]
    for name in spec.get("redcap_fields", []):
        f = dic[name]
        if f.is_date:
            out.append((name, "date", name))
            est = dic.estimate_for(name)
            if est:
                out.append((est, "estimate", est))
        elif f.is_checkbox:
            out.append((name, "multi", name))
        elif f.is_coded:
            out.append((name, "code", name))
        else:
            out.append((name, "text", name))
    return out


@lru_cache(maxsize=64)
def date_keys(group: str) -> dict[str, str | None]:
    """Date item keys of a group → the key of their estimate code (None: no estimate asked)."""
    items = group_items(group)
    if not is_generic(group):
        return {k: v for k, v in HAND_MADE_DATE_KEYS.items() if any(k == key for key, _, _ in items)}
    keys = [k for k, _, _ in items]
    dic = _default_dic()
    return {k: (dic.estimate_for(src) if src and dic.estimate_for(src) in keys else None)
            for k, kind, src in items if kind == "date"}


def anchor_keys(group: str) -> tuple[str, ...]:
    """The date keys that date an item (its event), in order of preference."""
    if not is_generic(group):
        return HAND_MADE_ANCHORS
    spec = _group_spec(group)
    return tuple(dict.fromkeys(x for x in [spec.get("anchor"), *date_keys(group)] if x))


def item_model(group: str, dic: Dictionary) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for key, kind, src in group_items(group):
        x = extra_item(group, key)
        desc = x.get("description") or ITEM_DESCRIPTIONS.get(key) or _describe(dic, src)
        if kind == "date":
            fields[key] = (str, Field(description=f"{desc} Format YYYY-MM-DD or '{NE}'."))
        elif kind == "estimate":
            codes = list(dic[src].choices) if src in dic else ["1", "2", "3", "4"]
            fields[key] = (_codes_lit(codes), Field(description=desc or "Date estimate code"))
        elif kind == "code":
            fields[key] = (_codes_lit(list(dic[src].choices)), Field(description=desc))
        elif kind == "multi":
            fields[key] = (list[_codes_lit(list(dic[src].choices))], Field(description=desc + " (list every code that applies)"))
        elif kind == "text":
            fields[key] = (str, Field(description=desc or "Free text"))
        elif kind == "enum":
            opts = x.get("options", {})
            fields[key] = (Literal[(*[str(o) for o in opts], NE)],
                           Field(description=f"{desc} Options: " + "; ".join(f"{c}={lab}" for c, lab in opts.items())))
        elif kind == "event":
            fields[key] = (Literal[EVENTS], Field(description="start = the FIRST dose of a new course actually given; "
                                                  "stop = the course ended; ongoing = currently taking, including a "
                                                  "routine maintenance dose or infusion of a drug the patient is "
                                                  "already on (e.g. 'Ultomiris infusion today', 'due for her next "
                                                  "rituximab'); planned/considered = NOT given yet"))
        elif kind == "result":
            fields[key] = (Literal[RESULTS], Field(description="Antibody result"))
        elif kind == "dxcat":
            fields[key] = (Literal[(*DX_CATEGORIES, NE)], Field(description=DX_DESCRIPTION))
        elif kind == "visittype":
            fields[key] = (Literal[(*VISIT_TYPES, NE)], Field(description=desc))
        elif kind == "comorbcat":
            fields[key] = (Literal[(*dic[src].choices, NO_CATEGORY, NMOSD_RELATED, NE)],
                           Field(description=f"{desc}. {NO_CATEGORY} = a condition outside these categories "
                                             "(e.g. osteopenia, diverticulosis, hearing loss, eosinophilia); "
                                             f"{NMOSD_RELATED} = NMOSD itself or one of its attacks or after-effects "
                                             "(e.g. transverse myelitis, optic neuritis). One code per condition."))
    fields["quote"] = (str, Field(description="Verbatim quote from the note that supports this item"))
    fields["certainty"] = (Literal["high", "medium", "low"], Field(description="How directly the quote supports it"))
    fields["negated"] = (bool, Field(description="True if the text says this did NOT happen"))
    fields["hypothetical"] = (bool, Field(description="True if planned, considered, recommended or conditional"))
    return create_model(f"{group.capitalize()}Item", **fields)


def output_model(group: str, dic: Dictionary) -> type[BaseModel]:
    item = item_model(group, dic)
    return create_model(f"{group.capitalize()}Output", items=(list[item], Field(description="One entry per mention")))


def _inline(schema: dict) -> dict:
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"].split("/")[-1]
                return walk(copy.deepcopy(defs[ref]))
            return {k: walk(v) for k, v in node.items() if k not in UNSUPPORTED_KEYS}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def ollama_schema(model: type[BaseModel]) -> dict:
    """JSON schema with $refs inlined and keywords llama.cpp's grammar converter may not support removed."""
    return _inline(model.model_json_schema())


@lru_cache(maxsize=8)
def _cached_dictionary(path: str) -> Dictionary:
    return load_dictionary(Path(path))


def llm_groups() -> list[str]:
    """Every field group the model answers (hand-made and `redcap_fields:` groups; not `method: regex` ones)."""
    groups = load_rules("fields/pilot_fields.yaml")["groups"]
    return [g for g, spec in groups.items() if spec.get("method", "llm") == "llm" and (g in GROUP_ITEMS or
                                                                                       "redcap_fields" in spec)]


def schemas_for(dictionary_path: Path) -> dict[str, tuple[type[BaseModel], dict]]:
    dic = _cached_dictionary(str(dictionary_path))
    out = {}
    for g in llm_groups():
        m = output_model(g, dic)
        out[g] = (m, ollama_schema(m))
    return out


def write_schemas(dictionary: Path, out_dir: Path, stdout=None) -> dict:
    stdout = stdout or sys.stdout
    dic = load_dictionary(dictionary)
    specs = load_rules("fields/pilot_fields.yaml")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    missing = [f for g in specs["groups"].values() for f, s in g.get("fields", {}).items()
               if f not in dic and s.get("redcap", True)]
    missing += [f for g in specs["groups"].values() for f in g.get("redcap_fields", []) if f not in dic]
    groups = llm_groups()
    for g in groups:
        (out_dir / f"{g}.schema.json").write_text(json.dumps(ollama_schema(output_model(g, dic)), indent=1), encoding="utf-8")
    print(f"Parsed {len(dic.fields)} fields in {len(dic.forms)} forms; wrote {len(groups)} schemas to {out_dir}.",
          file=stdout)
    if missing:
        print(f"Pilot fields missing from this dictionary: {missing}", file=stdout)
    for name, issues in dic.quirks().items():
        print(f"  quirk {name}: {'; '.join(issues)}", file=stdout)
    return {"fields": len(dic.fields), "missing": missing, "quirks": dic.quirks()}


# ------------------------------------------------------------------------------------------
# Structured Demographics → REDCap (no LLM; stage 2 checks the values against the notes, except STRUCTURED_ONLY)
# ------------------------------------------------------------------------------------------
def _lookup(value_map: dict, v: str) -> str | None:
    import re

    for k, c in value_map.items():
        if k.startswith("re:"):
            if v and re.search(k[3:], v, re.I):
                return str(c)
        elif v.lower() == str(k).lower():
            return str(c)
    return None


def structured_values(patient_row: dict) -> dict[str, tuple[str | None, str]]:
    """{redcap field: (code or None, note for the reviewer or "")} from a de-identified patients row.

    The note says where the code came from when it is not the usual column (a fallback column, or Date_Of_Death
    for living/deceased), or why there is none: the table's value without a REDCap code, or an empty column. These
    are category labels from the patient's own record, shown only in the review app (PHI location)."""
    maps = load_rules("redcap/structured_maps.yaml")
    out = {}
    for fld, primary in STRUCTURED_SOURCES.items():
        tried, code, used = [], None, None
        for col in (primary, *STRUCTURED_FALLBACKS.get(fld, ())):
            v = (patient_row.get(col) or "").strip()
            tried.append((col, v))
            code = _lookup(maps.get(fld, {}), v)
            if code is not None:
                used = col
                break
        if fld == "living_deceased" and patient_row.get("date_of_death"):
            code, used = str(maps.get("living_deceased_if_date_of_death", "2")), "date_of_death"
        said = "; ".join(f"{SOURCE_COLUMN_NAMES[c]} " + (f"is '{v}', which has no REDCap code" if v else "is empty")
                         for c, v in tried if c != used)
        if code is None:
            note = f"{fld}: no usable structured value ({said})"
        elif used == "date_of_death":
            note = ""
        elif used != primary:
            note = f"{fld}: taken from {SOURCE_COLUMN_NAMES[used]} '{tried[-1][1]}' ({said})"
        else:
            note = ""
        out[fld] = (code, note)
    return out


def map_structured(patient_row: dict) -> tuple[dict[str, str], list[str]]:
    """Return ({redcap field: code}, [unmapped field names for review]) from a de-identified patients row."""
    vals = structured_values(patient_row)
    return ({f: c for f, (c, _) in vals.items() if c is not None}, [f for f, (c, _) in vals.items() if c is None])
