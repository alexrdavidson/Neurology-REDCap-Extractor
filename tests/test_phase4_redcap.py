"""Phase 4: REDCap dictionary parsing and schema generation.

The committed fixture is transcribed from the PID 47765 codebook. When the real data dictionary CSV is placed
at config/redcap/data_dictionary.csv (it holds no PHI), the same checks also run against it.
"""

from __future__ import annotations

import io
import json

import pytest
from pydantic import ValidationError

from nmosd.config import load_rules
from nmosd.redcap.dictionary import load_dictionary
from nmosd.redcap.schemas import (
    GROUP_ITEMS,
    UNSUPPORTED_KEYS,
    llm_groups,
    map_structured,
    ollama_schema,
    output_model,
    structured_values,
    write_schemas,
)
from tests.conftest import REPO

FIXTURE = REPO / "tests/fixtures/redcap/data_dictionary_pilot.csv"
REAL = REPO / "config/redcap/data_dictionary.csv"
DICTS = [pytest.param(FIXTURE, id="codebook-fixture"),
         pytest.param(REAL, id="real-dictionary",
                      marks=pytest.mark.skipif(not REAL.exists(), reason="real data dictionary CSV not supplied yet"))]


@pytest.fixture(params=DICTS)
def dic(request):
    return load_dictionary(request.param)


def test_choices_keyed_by_code(dic):
    assert list(dic["type_test"].choices) == ["mog", "aqp4"]
    assert dic["maintenance_therapy"].choices["5"].startswith("Inebilizumab")
    assert dic["admission"].choices == {"1": "Yes", "0": "No"}
    assert list(dic["adverse_events"].choices)[4:] == ["6", "5"], "order preserved; codes, not positions"


def test_quirks_detected(dic):
    q = dic.quirks()
    assert any("duplicate labels" in s and "methotrexate" in s for s in q["rx_before_nmo"])
    assert any("out of order" in s for s in q["adverse_events"])


def test_field_kinds(dic):
    for calc in ["bmi", "los", "treatment_number", "duration_of_therapy", "mrn_id3"]:
        assert dic[calc].is_calc and not dic[calc].is_exportable
    assert dic["relapse_table"].is_descriptive
    assert dic["date_nmo_diagnosis"].is_hidden and dic["date_nmo_diagnosis"].is_date
    assert dic["mrn"].identifier and dic["dob"].identifier
    assert dic["acute_rx"].none_of_the_above == "6"
    cols = dic["relapse_phenotype"].checkbox_columns()
    assert cols["1"] == "relapse_phenotype___1" and len(cols) == 7


def test_pilot_specs_are_complete_and_draft(dic):
    specs = load_rules("fields/pilot_fields.yaml")
    keys = {"definition", "acceptable_evidence", "exclusions", "time_window", "ambiguity_rule", "estimate_field",
            "instrument", "repeating", "prefilter_keywords", "preferred_source", "status"}
    n = 0
    for gname, g in specs["groups"].items():
        # 16.11.26: groups that list REDCap fields by name take every definition from the codebook
        for name in g.get("redcap_fields", []):
            assert name in dic, (gname, name)
        if "redcap_fields" in g or g.get("method") == "regex":
            assert g.get("status", "draft").startswith("draft"), gname
        for name, spec in g.get("fields", {}).items():
            assert keys <= set(spec), name
            assert spec["status"].startswith("draft"), name
            # dx_category feeds the diagnosis check only (config/eligibility.yaml); every other spec is a REDCap field
            assert name in dic or (spec.get("redcap") is False and spec["instrument"] == "eligibility"), name
            if spec["estimate_field"]:
                assert spec["estimate_field"] in dic
            n += 1
    assert n >= 18
    assert "plan" in specs["groups"]["treatment"]["fields"]["date_starting_maint"]["exclusions"].lower()
    for name, s in specs["structured"].items():
        assert s["preferred_source"] == "structured" and s["status"].startswith("draft") and name in dic


def test_generated_models_validate_codes(dic):
    tx = output_model("treatment", dic)
    ok = {"items": [{"maintenance_therapy": "1", "event": "start", "event_date": "2018-04-16", "event_date_estimate": "1",
                     "reason_for_stopping_tx": "not_established", "other_maintenance_tx": "", "other_stopping": "",
                     "reason_for_stopping_therap": "", "quote": "First rituximab infusion",
                     "certainty": "high", "negated": False, "hypothetical": False}]}
    assert tx.model_validate(ok)
    bad = json.loads(json.dumps(ok))
    bad["items"][0]["maintenance_therapy"] = "Rituximab"
    with pytest.raises(ValidationError):
        tx.model_validate(bad)
    rel = output_model("relapse", dic)
    item = {"date_onset": "2016-07-15", "date_attack_estimate": "3", "relapse_phenotype": ["1", "2"], "admission": "UNK",
            "date_admission": "not_established", "date_discharge": "not_established", "acute_rx": ["1"],
            "mri_relapse": "not_established", "clinical_confirm": "2", "trigger": "10", "gad_relapse": "not_established",
            "let": "1", "noncore_phenotype": "", "firstrelapsestate": "1",
            "quote": "q", "certainty": "low", "negated": False, "hypothetical": False}
    assert rel.model_validate({"items": [item]})
    with pytest.raises(ValidationError):
        rel.model_validate({"items": [dict(item, relapse_phenotype=["9"])]})


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_ollama_schemas_are_self_contained(dic):
    for g in GROUP_ITEMS:
        s = ollama_schema(output_model(g, dic))
        text = json.dumps(s)
        assert "$ref" not in text and "$defs" not in text
        for node in _walk(s):
            assert not (set(node) & UNSUPPORTED_KEYS), (g, set(node) & UNSUPPORTED_KEYS)
        assert s["type"] == "object" and s["properties"]["items"]["type"] == "array"
        item = s["properties"]["items"]["items"]
        assert set(item["required"]) == set(item["properties"])
        enums = [n["enum"] for n in _walk(item) if "enum" in n]
        if g == "followup":   # not a REDCap group: its codes have no UNK
            assert any("not_established" in e for e in enums)
        else:
            assert any("not_established" in e and "UNK" in e for e in enums)


def test_write_schemas(tmp_path):
    buf = io.StringIO()
    res = write_schemas(FIXTURE, tmp_path, stdout=buf)
    assert res["missing"] == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{g}.schema.json" for g in sorted(llm_groups())]
    assert set(GROUP_ITEMS) <= set(llm_groups())
    assert "quirk rx_before_nmo" in buf.getvalue()


def test_structured_mapping():
    out, review = map_structured({"sex_at_birth": "Female", "race1": "Black", "ethnic_group": "Non Hispanic",
                                  "vital_status": "Not reported as deceased", "date_of_death": None})
    assert out == {"sex": "2", "race": "2", "ethnicity": "2", "living_deceased": "1"} and review == []
    out, review = map_structured({"sex_at_birth": "Female", "race1": "Declined", "ethnic_group": "Unknown",
                                  "vital_status": "Not reported as deceased", "date_of_death": "2025-01-01"})
    assert out["living_deceased"] == "2" and set(review) == {"race", "ethnicity"}


def test_structured_fallback_columns_and_notes():
    """16.11.26: Gender_Legal_Sex when Sex_At_Birth is empty or has no code, Race_Group likewise for Race1; the
    reviewer is told which column was used, or why there is no value."""
    row = {"sex_at_birth": "", "gender_legal_sex": "Female", "race1": "Declined", "race_group": "White",
           "ethnic_group": "Non Hispanic", "vital_status": "Not reported as deceased", "date_of_death": None}
    vals = structured_values(row)
    assert vals["sex"] == ("2", "sex: taken from Gender_Legal_Sex 'Female' (Sex_At_Birth is empty)")
    assert vals["race"] == ("1", "race: taken from Race_Group 'White' (Race1 is 'Declined', which has no REDCap code)")
    assert vals["ethnicity"] == ("2", "") and vals["living_deceased"] == ("1", "")
    row.update(sex_at_birth="Male", gender_legal_sex="Female", race_group="Unknown", ethnic_group=None)
    vals = structured_values(row)
    assert vals["sex"] == ("1", ""), "Sex_At_Birth comes first"
    assert vals["race"] == (None, "race: no usable structured value (Race1 is 'Declined', which has no REDCap code; "
                                  "Race_Group is 'Unknown', which has no REDCap code)")
    assert vals["ethnicity"] == (None, "ethnicity: no usable structured value (Ethnic_Group is empty)")
    row.update(vital_status="", date_of_death="2025-01-01")
    assert structured_values(row)["living_deceased"] == ("2", ""), "a date of death means deceased"
    assert map_structured(row) == ({"sex": "1", "living_deceased": "2"}, ["race", "ethnicity"])


@pytest.mark.ollama
def test_schemas_accepted_by_local_ollama():
    """Integration: run only where Ollama is up with a pulled model (e.g. on the pilot laptop)."""
    from nmosd.config import load_settings
    from nmosd.extract.ollama_client import OllamaClient

    try:
        client = OllamaClient.from_settings(load_settings())
        installed = client.installed_models()
    except Exception:
        pytest.skip("no local Ollama server")
    if client.model not in installed:
        pytest.skip(f"{client.model} not pulled")
    dic = load_dictionary(FIXTURE)
    for g in GROUP_ITEMS:
        res = client.generate_json("Return an empty items list.", "No clinical content.", ollama_schema(output_model(g, dic)))
        assert res.error is None and res.parsed is not None and "items" in res.parsed
