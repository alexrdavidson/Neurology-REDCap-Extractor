"""16.11.26: every field of the REDCap codebook (1-303), numbered as in the codebook, and the user's review feedback.

1. The codebook PDF converts to the dictionary CSV with the codebook's numbering; every field has a source.
2. The codebook's label, field note, branching logic and codes go into the model's instructions.
3. Fixes from the review of the pilot: comorbidities from long problem lists (cut-off answers), the estimate code of a
   year-only date, otherdx at the initial presentation, one course for repeat doses, still on therapy from the most
   recent notes, an undated positive AQP4, the notes search, no number on the diagnosis check.
4. The new instruments (admissions, imaging, labs, IVIG, infections, EDSS) and the derived fields.
Synthetic data only.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import sys
from datetime import date

import pytest

from nmosd.extract.ollama_client import LLMResult
from nmosd.extract.stage1_evidence import extract_chunk, items_to_rows, salvage_items
from nmosd.extract.stage2_instruments import (
    demographics_extra,
    finish,
    lab_results,
    merge_relapse_details,
    reconcile_imaging,
    reconcile_labs,
    route_admissions,
)
from nmosd.extract.stage2_reconcile import (
    DocMeta,
    Item,
    reconcile_antibodies,
    reconcile_comorbidities,
    reconcile_diagnosis,
    reconcile_treatments,
    titer_code,
)
from nmosd.extract.targets import pick_assessment_notes
from nmosd.extract.vitals import find_vitals
from nmosd.redcap.dictionary import eval_branching, load_dictionary
from nmosd.redcap.schemas import NMOSD_RELATED, group_items, llm_groups, ollama_schema, output_model
from nmosd.redcap.sources import (
    DRUG_CODE_MAPS,
    OTHER_CODE,
    form_label,
    repeating_forms,
    review_fields,
    source,
)
from nmosd.textproc.dates import written_precision
from nmosd.textproc.view import TextView
from tests.conftest import REPO

DICT = REPO / "config/redcap/data_dictionary.csv"
PDF = REPO / "docs/NMOSD database _ REDCap_Codebook.pdf"


@pytest.fixture(scope="module")
def dic():
    return load_dictionary(DICT)


def _item(n, group, fields, event_date=None, est=None, *, doc_date="2020-01-01", neg=False, hyp=False, quote="q",
          doc="D1"):
    return Item(f"i{n}", group, doc, doc_date, n, dict(fields), event_date, est, neg, hyp, [f"e{n}"],
                {k: f"e{n}" for k in fields}, quote)


# ---------------------------------------------------------------------------------------------
# 1. The codebook, its numbering, and where every field comes from
# ---------------------------------------------------------------------------------------------
@pytest.mark.skipif(not shutil.which("pdftotext") or not PDF.exists(), reason="needs poppler's pdftotext and the PDF")
def test_codebook_pdf_rebuilds_the_dictionary(tmp_path):
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import codebook_to_dictionary as cb
    finally:
        sys.path.pop(0)
    out = tmp_path / "dd.csv"
    assert cb.main([str(PDF), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == DICT.read_text(encoding="utf-8")


def test_codebook_numbering_and_fields(dic):
    assert len(dic.fields) == 303 and len(dic.forms) == 12
    assert [f.number for f in dic.fields.values()] == list(range(1, 304))
    spot = {1: "record_id", 14: "otherdx", 45: "date_onset", 73: "adverse_events_hosp", 157: "maintenance_therapy",
            167: "date_stopping_maint", 263: "mrs", 284: "aqp4_assay_status", 303: "last_follow_up_complete"}
    assert {n: list(dic.fields)[n - 1] for n in spot} == spot
    assert dic.estimate_for("date_onset") == "date_attack_estimate" and dic.estimate_for("aqp4_assay_date") is None
    assert dic["date_onset"].clean_label == "Attack Date (date of symptom onset)"
    assert "prolonged effect" in dic["date_stopping_maint"].note
    assert dic["relapse_complete"].is_form_status and not dic["relapse_complete"].is_exportable
    assert eval_branching(dic["gad_relapse"].branching, {"mri_relapse": "0"}) is False
    assert eval_branching(dic["maintenance_change"].branching, {"reason_maintenance": "5"}) is True


def test_every_field_has_a_source(dic):
    kinds = {n: source(dic, n)[0] for n in dic.fields}
    assert set(kinds.values()) <= {"notes", "table", "derived", "redcap", "identifier", "reviewer"}
    assert sum(k in ("notes", "derived", "table") for k in kinds.values()) >= 230
    assert kinds["bmi"] == kinds["los"] == "redcap" and kinds["sex"] == "table" and kinds["mrn"] == "identifier"
    assert kinds["dmt_relapse"] == "derived" and kinds["cerebellar_step_25"] == "notes"
    assert kinds["date_attack_estimate"] == "notes", "an estimate radio follows its date"
    rf = review_fields(dic)
    assert list(rf) == list(dic.forms) and all(rf.values())
    assert repeating_forms(dic) == set(dic.forms) - {"demographics"}
    assert form_label(dic, "relapse") == "Relapse (#45–86)"


def test_drug_code_maps_go_code_to_code(dic):
    mt = dic["maintenance_therapy"].choices
    first = lambda label: re.split(r"[ (]", label.lower())[0][:6]  # noqa: E731
    for field, m in DRUG_CODE_MAPS.items():
        for src, dst in m.items():
            assert dst in dic[field].choices, (field, dst)
            if dst != OTHER_CODE[field]:
                assert first(mt[src]) == first(dic[field].choices[dst]), (field, src, dst)


# ---------------------------------------------------------------------------------------------
# 2. The codebook in the model's instructions
# ---------------------------------------------------------------------------------------------
def test_codebook_text_goes_into_the_schema(dic):
    groups = llm_groups()
    for g in ("imaging", "admission", "labs", "edss_ambulation", "treatment_detail", "infection", "ivig"):
        assert g in groups
    desc = json.dumps(ollama_schema(output_model("edss_ambulation", dic)))
    assert "REDCap #193" in desc and "If unsure, mark 'Yes'" in desc and "REDCap asks this only if" in desc
    tx = json.dumps(ollama_schema(output_model("treatment", dic)))
    assert "REDCap #157" in tx and "PO prednisone" in tx and "routine maintenance dose" in tx
    keys = [k for k, _, _ in group_items("imaging")]
    assert keys[:2] == ["date_of_study", "date_mri_estimate"]
    assert next(k for k, _, _ in group_items("admission")) == "for_attack"


# ---------------------------------------------------------------------------------------------
# 3. Fixes from the pilot review
# ---------------------------------------------------------------------------------------------
def test_a_year_only_date_is_never_exact():
    assert written_precision("on Ultomiris since 2019", "2019-01-01", 2023) == "3"
    assert written_precision("started 3/2019", "2019-03-15", 2023) == "2"
    assert written_precision("first infusion 03/14/2019", "2019-03-14", 2023) == "1"
    chunk = TextView.full("Has been on Ultomiris since 2019, tolerating well.")
    items = [{"maintenance_therapy": "4", "event": "start", "event_date": "2019-01-01", "event_date_estimate": "1",
              "quote": "Has been on Ultomiris since 2019", "certainty": "high", "negated": False, "hypothetical": False}]
    rows, _ = items_to_rows("treatment", items, chunk, run_id="r", doc_id="D", chunk_id="c", study_id="S",
                            doc_date="2023-05-01")
    assert {r["estimate_code"] for r in rows if r["field"] == "event_date"} == {"3"}


class _CutOffClient:
    """Answers with every condition of the excerpt, but stops after 3 (like num_predict running out)."""
    num_predict = 64

    def __init__(self):
        self.calls = 0

    def generate_json(self, system, prompt, schema):
        self.calls += 1
        text = prompt.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0]
        names = re.findall(r"^Problem (\w+)", text, re.M)
        items = [{"condition": n, "comorbidities": "no_category", "autoimmune": "not_established", "quote": f"Problem {n}",
                  "certainty": "high", "negated": False, "hypothetical": False} for n in names]
        content = json.dumps({"items": items})
        if len(items) > 3:
            content = content[: content.index(f'{{"condition": "{names[3]}"') + 20]
            return LLMResult(content=content, parsed=None, model="m", digest=None, options={}, eval_count=64)
        return LLMResult(content=content, parsed=json.loads(content), model="m", digest=None, options={}, eval_count=10)


def test_a_cut_off_answer_is_split_and_nothing_is_lost(dic):
    lines = [f"Problem C{n:02d} " + "x" * 150 for n in range(12)]
    chunk = TextView.full("\n".join(lines))
    model = output_model("comorbidity", dic)
    client = _CutOffClient()
    res = extract_chunk(client, model, ollama_schema(model), "comorbidity", chunk, run_id="r", doc_id="D",
                        chunk_id="c", study_id="S", doc_type="note", source_table="VisitNote", doc_date="2020-01-01",
                        chunk_no=1, n_chunks=1)
    got = {r["value"] for r in res.rows if r["field"] == "condition"}
    assert got == {f"C{n:02d}" for n in range(12)} and res.splits >= 1 and len(res.calls) == client.calls


def test_salvage_keeps_complete_items():
    full = json.dumps({"items": [{"a": 'x "y" {z}'}, {"a": "b]"}, {"a": "cut"}]})
    assert salvage_items(full[:-12]) == [{"a": 'x "y" {z}'}, {"a": "b]"}]


def test_repeat_infusions_are_one_course_and_recent_notes_decide():
    infusions = [_item(n, "treatment", {"maintenance_therapy": "4", "event": "start"},
                       date.fromordinal(date(2021, 1, 4).toordinal() + 56 * n).isoformat(), "1",
                       doc_date=date.fromordinal(date(2021, 1, 4).toordinal() + 56 * n).isoformat()) for n in range(12)]
    eps = reconcile_treatments(infusions)
    assert len(eps) == 1 and eps[0]["values"]["date_starting_maint"] == "2021-01-04"
    assert eps[0]["values"]["still_on_therapy"] == "1" and "merged" in " ".join(eps[0]["info"])
    # a stop, then later notes say it continues: the most recent notes win (flagged)
    items = [_item(1, "treatment", {"maintenance_therapy": "7", "event": "start"}, "2015-02-01", "1", doc_date="2015-02-01"),
             _item(2, "treatment", {"maintenance_therapy": "7", "event": "stop"}, "2016-01-01", "1", doc_date="2016-01-05"),
             _item(3, "treatment", {"maintenance_therapy": "7", "event": "ongoing"}, None, doc_date="2016-06-01")]
    aza = reconcile_treatments(items)[0]
    assert aza["values"]["still_on_therapy"] == "1" and "most recent notes" in aza["conflict"]
    # not mentioned for years while another drug continues: proposed as stopped
    items = [_item(1, "treatment", {"maintenance_therapy": "2", "event": "start"}, "2015-02-01", "1", doc_date="2015-02-01"),
             _item(2, "treatment", {"maintenance_therapy": "2", "event": "ongoing"}, None, doc_date="2016-01-01"),
             _item(3, "treatment", {"maintenance_therapy": "1", "event": "start"}, "2017-03-01", "1", doc_date="2017-03-01"),
             _item(4, "treatment", {"maintenance_therapy": "1", "event": "ongoing"}, None, doc_date="2023-01-01")]
    mmf, rtx = reconcile_treatments(items)
    assert mmf["values"]["still_on_therapy"] == "0" and "not mentioned since" in mmf["conflict"]
    assert rtx["values"]["still_on_therapy"] == "1"


def test_an_undated_positive_aqp4_is_kept(dic):
    items = [_item(1, "antibody", {"type_test": "aqp4", "result": "positive", "titer": "1:1000"}, None)]
    ab = reconcile_antibodies(items, dic)
    assert len(ab) == 1 and ab[0]["values"]["aqp4_assay_status"] == "1" and "no test date" in ab[0]["conflict"]
    assert ab[0]["values"]["aqp4_assay_titer"] == titer_code(dic["aqp4_assay_titer"].choices, "1:1000") == "6"
    assert titer_code(dic["titer"].choices, "1:100") == "3" and titer_code(dic["titer"].choices, "1:20000") == "6"
    # an undated result joins a dated test with the same result
    items.append(_item(2, "antibody", {"type_test": "aqp4", "result": "positive"}, "2016-05-02", "1"))
    ab = reconcile_antibodies(items, dic)
    assert len(ab) == 1 and ab[0]["values"]["aqp4_assay_date"] == "2016-05-02"


def test_otherdx_is_the_diagnosis_considered_at_presentation():
    items = [_item(1, "diagnosis", {"date_nmo_diagnosis": "2016-09-14"}, "2016-09-14", "1", doc_date="2017-01-01"),
             _item(2, "diagnosis", {"otherdx": "4", "dx_other": "migraine"}, doc_date="2019-01-01",
                   quote="She has migraine"),
             _item(3, "diagnosis", {"otherdx": "1"}, doc_date="2018-01-01", hyp=True,
                   quote="initially thought to have MS")]
    dx = reconcile_diagnosis(items)
    assert dx["values"]["otherdx"] == "1" and "dx_other" not in dx["values"]


def test_nmosd_related_conditions_are_listed_not_ticked():
    items = [_item(1, "comorbidity", {"condition": "Transverse myelitis", "comorbidities": NMOSD_RELATED}),
             _item(2, "comorbidity", {"condition": "Migraine", "comorbidities": "3"}),
             _item(3, "comorbidity", {"condition": "Osteopenia", "comorbidities": "no_category"})]
    cm = reconcile_comorbidities(items)
    assert cm["values"]["comorbidities"] == ["3"]
    assert {c["name"]: c["category"] for c in cm["conditions"]}["Transverse myelitis"] == NMOSD_RELATED


def test_vitals_patterns():
    assert find_vitals('BP 120/80 | Wt 154 lb (69.9 kg) | Ht 5\' 6" | BMI 24.9') == [
        ("weight", 69.9, 12, 21), ("height", 167.6, 34, 42)]
    assert find_vitals("Weight: 2 kg; hit 5 times") == []


# ---------------------------------------------------------------------------------------------
# 4. New instruments and derived fields
# ---------------------------------------------------------------------------------------------
def test_admissions_go_to_the_attack_or_become_hospitalizations(dic):
    relapse = [{"anchor": "2019-03-01", "values": {"date_onset": "2019-03-01", "date_attack_estimate": "1",
                                                   "relapse_phenotype": ["2"], "acute_rx": ["1"]},
                "evidence": ["e0"], "conflict": ""}]
    items = [_item(1, "admission", {"for_attack": "1", "nr_admission": "2019-03-04", "nr_discharge": "2019-03-10",
                                    "acute_rx": ["2"], "nr_icu": "0"}, "2019-03-04"),
             _item(2, "admission_course", {"nr_admission": "2019-03-04", "nr_rehab": "1",
                                           "nr_rehab_admit": "2019-03-10"}, "2019-03-04"),
             _item(3, "admission", {"for_attack": "0", "nr_admission": "2020-07-01", "nr_cause": ["1"],
                                    "nr_infection_detail": ["1"]}, "2020-07-01"),
             _item(4, "relapse_detail", {"date_onset": "2019-03-02", "trigger": "1", "let": "1"}, "2019-03-02", "1")]
    rel = merge_relapse_details(relapse, items, dic, 30, 120)
    nr = route_admissions(rel, items, dic)
    r = finish(rel[0], dic)["values"]
    assert r["admission"] == "1" and r["date_admission"] == "2019-03-04" and r["acute_rx"] == ["1", "2"]
    assert r["rehab"] == "1" and r["rehab_admit"] == "2019-03-10" and r["trigger"] == "1" and r["let"] == "1"
    h = finish(nr[0], dic)["values"]
    assert len(nr) == 1 and h["nr_cause"] == ["1"] and h["nr_infection_detail"] == ["1"]


def test_mris_within_two_weeks_are_one_study(dic):
    docs = {"R1": DocMeta(date(2019, 3, 5), "neuro_mri"), "R2": DocMeta(date(2019, 3, 12), "neuro_mri")}
    items = [_item(1, "imaging", {"mri_type": ["1"], "did_this_mri_show_radiolog": "1"}, doc_date="2019-03-05", doc="R1"),
             _item(2, "imaging_spine", {"mri_c_abn": "1", "cervical_features": ["3"]}, "2019-03-12", "1",
                   doc_date="2019-03-12", doc="R2"),
             _item(3, "imaging", {"mri_optic_abn": "1"}, doc_date="2019-03-05", doc="R1")]
    rel = [{"date": date(2019, 3, 1), "values": {}}]
    out = reconcile_imaging(items, dic, docs, rel)
    assert len(out) == 1
    v = out[0]["values"]
    assert v["date_of_study"] == "2019-03-05" and v["mri_type"] == ["1", "3"] and v["mri_available_info"] == ["1"]
    assert v["cervical_features"] == ["3"] and v["mri_relapse_number"] == "1"
    assert "mri_optic_abn" not in v, "orbits not imaged: REDCap does not ask (branching)"


def test_lab_results_become_cd_and_ig_instances(dic):
    items = [_item(1, "labs", {"test": "cd19_abs", "value": "<1", "unit": "cells/uL"}, "2021-06-01"),
             _item(2, "labs", {"test": "cd19_pct", "value": "0.1", "unit": "%"}, "2021-06-01"),
             _item(3, "labs", {"test": "igg", "value": "5.2", "unit": "g/L"}, "2021-06-02")]
    cd, ig = reconcile_labs(lab_results(items, {}), dic)
    # REDCap asks for the % only when no absolute count is available (branching)
    assert cd[0]["values"] == {"date_cd": "2021-06-01", "cd19_yn": "1", "cd19_abs_yn": "1", "cd19_abs": "0"}
    cd, _ = reconcile_labs(lab_results(items[1:2], {}), dic)
    assert cd[0]["values"] == {"date_cd": "2021-06-01", "cd19_yn": "1", "cd19_abs_yn": "0", "cd19_percent": "0.1"}
    assert ig[0]["values"] == {"date_igg": "2021-06-02", "igg": "520"}


def test_edss_notes_at_treatment_start_stop_and_last_follow_up():
    notes = [("N1", date(2018, 12, 1)), ("N2", date(2019, 6, 1)), ("N3", date(2022, 1, 1)), ("N4", date(2023, 5, 1))]
    picks = pick_assessment_notes([(date(2019, 1, 15), "start", "start"), (date(2021, 12, 20), "stop", "stop")], notes)
    assert [(p.doc_id, p.timing) for p in picks] == [("N1", "1"), ("N3", "2"), ("N4", "3")]


def test_derived_demographics(dic):
    rel = [{"date": date(2016, 7, 1), "values": {"relapse_phenotype": ["1"], "admission": "1"}},
           {"date": date(2019, 3, 1), "values": {"relapse_phenotype": ["2", "4"]}}]
    courses = [{"drug": "7", "start_date": date(2016, 1, 1), "stop_date": date(2017, 1, 1),
                "values": {"still_on_therapy": "0"}, "evidence": ["t1"]},
               {"drug": "4", "start_date": date(2019, 5, 1), "stop_date": None, "values": {"still_on_therapy": "1"},
                "evidence": ["t2"]}]
    ab = [{"values": {"type_test": "aqp4", "aqp4_assay_status": "1"}, "evidence": ["a1"]}]
    d = demographics_extra([], dic, living="1", dx_date=date(2016, 9, 1), relapse_insts=rel, nr_insts=[], courses=courses,
                           antibodies=ab, edss=[], last_contact="2023-05-01", gate_dx="nmosd")["values"]
    assert d["relapse_number"] == "2" and d["rh_number"] == "1" and d["loc_nmo_total"] == ["1", "2", "3"]
    assert d["relapse_type_diagnosis"] == ["1"] and d["rx_before_nmo"] == ["4"] and d["dx_other_treatment"] == "1"
    assert d["dmt_lastfu"] == ["5"] and d["aqp4"] == "1" and d["last_fu"] == "2023-05-01" and d["nmo_criteria"] == "1"


# ---------------------------------------------------------------------------------------------
# Review, search, export, coverage on the synthetic pipeline
# ---------------------------------------------------------------------------------------------
def test_search_matches_across_line_breaks_then_all_words(pipeline, tmp_path):
    from nmosd.review.store import ReviewStore

    s = ReviewStore(tmp_path / "deid" / "r.sqlite", pipeline["extract"], pipeline["deid"], DICT)
    try:
        sid = s.patients()[0]
        doc = next(r for r in s.search(sid, "") if len((s.document(r["doc_id"], sid) or {}).get("text", "").split()) > 20)
        words = re.findall(r"[A-Za-z]{4,}", s.document(doc["doc_id"], sid)["text"])[:2]
        broken = f"{words[0].upper()}\n\n  {words[1]}"
        assert all(r["match"] == "phrase" for r in s.search(sid, broken.replace("\n", " ")))
        assert s.search(sid, f"{words[0]} {words[1]}")
        res = s.search(sid, f"{words[1]} zzz-not-a-word")
        assert res == []
        res = s.search(sid, f"{words[1]} {words[0]}")
        assert res and res[0]["match"] in ("phrase", "all words")
    finally:
        s.close()


def test_coverage_report_is_counts_only(pipeline):
    from nmosd.eval.coverage import run_coverage

    buf = io.StringIO()
    assert run_coverage(pipeline["extract"], pipeline["deid"], DICT, stdout=buf) == 0
    out = buf.getvalue()
    assert "| 157 | maintenance_therapy | notes (treatment) |" in out and "## Disability (#190–264)" in out
    assert "NMO-" not in out and not re.search(r"\d{4}-\d{2}-\d{2}", out), "no IDs, no dates"


def test_review_shows_codebook_numbers_and_every_instrument(pipeline, tmp_path):
    st_testing = pytest.importorskip("streamlit.testing.v1")
    from nmosd.review.store import GATE, ReviewStore

    review = tmp_path / "deid" / "review.sqlite"
    s = ReviewStore(review, pipeline["extract"], pipeline["deid"], DICT)
    sid = next(p for p in s.patients() if s.fields(p, GATE, GATE)[0].proposed == "nmosd")
    for fs in s.fields(sid, GATE, GATE):
        s.record("Synthetic Reviewer", "accept", sid, GATE, GATE, fs.field)
    s.close()
    at = st_testing.AppTest.from_file(str(REPO / "src/nmosd/review/app.py"), default_timeout=60)
    argv = sys.argv
    sys.argv = ["app.py", "--extract", str(pipeline["extract"]), "--deid", str(pipeline["deid"]), "--review",
                str(review), "--dictionary", str(DICT)]
    try:
        at.run()
        at.text_input(key="login_name").input("Synthetic Reviewer")
        at.button(key="login").click().run()
        at.selectbox(key="patient").set_value(sid).run()
        opts = at.radio(key="instrument").options
        assert opts[0] == "Diagnosis check" and len(opts) == 13 and "Disability (#190–264)" in opts
        at.radio(key="instrument").set_value("relapse").run()
        assert not at.exception
        text = " ".join(m.value for m in at.markdown)
        assert "45. Attack Date (date of symptom onset)" in text
        assert re.search(r"\*\*48\. What was the phenotype\?\*\*", text)
    finally:
        sys.argv = argv


def test_export_writes_the_new_instruments_as_repeating(pipeline, tmp_path):
    from nmosd.redcap.export import EXPORT_NAME, run_export
    from nmosd.review.store import GATE, INSTRUMENT_FIELDS, ReviewStore

    review = tmp_path / "deid" / "review.sqlite"
    s = ReviewStore(review, pipeline["extract"], pipeline["deid"], DICT)
    for sid in s.patients():
        for fs in s.fields(sid, GATE, GATE):
            s.record("R", "accept", sid, GATE, GATE, fs.field)
        for inst in INSTRUMENT_FIELDS:
            for key in s.instances(sid, inst):
                for fs in s.visible(s.fields(sid, inst, key)):
                    if fs.proposed is not None:
                        s.record("R", "accept", sid, inst, key, fs.field)
    s.close()
    out = tmp_path / "deid" / "export"
    out.mkdir(parents=True)
    buf = io.StringIO()
    assert run_export(pipeline["extract"], review, DICT, out, deid=pipeline["deid"], stdout=buf) == 0, buf.getvalue()
    rows = list(csv.DictReader((out / EXPORT_NAME).open(encoding="utf-8")))
    insts = {r["redcap_repeat_instrument"] for r in rows}
    assert "last_follow_up" in insts and "" in insts
    demo = [r for r in rows if not r["redcap_repeat_instrument"]]
    assert all(r["relapse_number"] for r in demo), "derived demographics are exported once reviewed"
    assert "Validation errors: 0" in buf.getvalue()
