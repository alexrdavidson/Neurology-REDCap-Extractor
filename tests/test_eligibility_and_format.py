"""Updates from the clinical team (10.11.26), on synthetic data only.

1. Diagnosis check first: every patient's review starts by confirming NMOSD (not MOGAD/MS/...) with at least
   12 months of follow-up, weighting the most recent clinic letters and hospital notes; excluded patients are
   never exported.
2. Output format: our CSVs copy the header (columns, order) and file style of the project's own REDCap export,
   reading only its header line; `profile-redcap-csv` describes such a file without printing any value.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date

import pytest

from nmosd.cli import build_parser
from nmosd.config import load_rules
from nmosd.eval.compare_gold import run_evaluate
from nmosd.extract.stage2_reconcile import DocMeta, Item, reconcile_eligibility
from nmosd.redcap.csv_format import (
    CsvStyle,
    format_date,
    parse_date,
    read_template,
    standard_columns,
    write_csv,
)
from nmosd.redcap.csv_profile import expected_export_order, profile_csv
from nmosd.redcap.dictionary import load_dictionary
from nmosd.redcap.export import EXPORT_NAME, run_export
from nmosd.redcap.reidentify import DATE_ESTIMATE, FINAL_NAME, run_reidentify
from nmosd.redcap.schemas import output_model
from nmosd.review.store import GATE, INSTRUMENT_FIELDS, ReviewStore
from tests.conftest import PASSPHRASE, REPO

DICT = REPO / "tests/fixtures/redcap/data_dictionary_pilot.csv"
REVIEWER = "Synthetic Reviewer"
RULES = load_rules("eligibility.yaml")   # the committed rules (12 months, 24-month window, 3-month margin)


# =============================================================================================
# reconciliation rules (unit)
# =============================================================================================
def _dx(doc, cat, start=0, hyp=False, neg=False):
    return Item(f"i-{doc}-{start}", "diagnosis", doc, None, start, {"dx_category": cat}, hypothetical=hyp,
                negated=neg, evidence_ids=[f"e-{doc}-{start}"], field_evidence={"dx_category": f"e-{doc}-{start}"})


def _docs(**kw):
    """name=(date, relevance class[, description])"""
    return {k: DocMeta(date.fromisoformat(v[0]), v[1], v[2] if len(v) > 2 else None) for k, v in kw.items()}


def _visit(doc, sent, when, kind="infusion", neg=False, hyp=False):
    return Item(f"v-{doc}", "followup", doc, sent, 0, {"visit_date": when, "visit_type": kind}, negated=neg,
                hypothetical=hyp, evidence_ids=[f"ev-{doc}"], field_evidence={"visit_date": f"ev-{doc}"})


def test_most_recent_clinic_letter_wins_over_an_older_majority():
    docs = _docs(d1=("2016-01-10", "clinical_note"), d2=("2017-03-02", "clinical_note"),
                 d3=("2018-05-05", "clinical_note"), d4=("2024-06-01", "clinical_note"))
    items = [_dx("d1", "nmosd"), _dx("d2", "nmosd"), _dx("d3", "nmosd"), _dx("d4", "mogad")]
    r = reconcile_eligibility(items, {}, [], docs, {}, RULES)
    assert r["dx_category"] == "mogad"
    assert "recent notes also state" not in r["dx_conflict"], "the NMOSD statements are older than the window"
    assert r["dx_evidence"][0] == "e-d4-0", "evidence newest first"
    docs["d3"] = DocMeta(date(2023, 9, 1), "clinical_note")
    r = reconcile_eligibility(items, {}, [], docs, {}, RULES)
    assert r["dx_category"] == "mogad" and "recent notes also state nmosd" in r["dx_conflict"]


def test_copied_forward_statement_counts_at_its_latest_note_but_new_text_wins_ties():
    docs = _docs(d1=("2015-02-01", "clinical_note"), d5=("2024-05-01", "clinical_note"))
    # d1's "MS" paragraph (chars 90-200) is copied into d5, which also has a newly written NMOSD assessment
    repeats = {"d1": [(90, 200, "d5")]}
    items = [_dx("d1", "ms", start=100), _dx("d5", "nmosd", start=300)]
    r = reconcile_eligibility(items, {}, [], docs, repeats, RULES)
    assert r["dx_category"] == "nmosd"
    assert "recent notes also state ms" in r["dx_conflict"], "the copied MS line is still in the latest note"
    ms = next(s for s in r["summary"]["statements"] if s["category"] == "ms")
    assert ms["last"] == "2024-05-01" and ms["documents"] == 2


def test_other_documents_possible_diagnoses_and_no_statement():
    docs = _docs(m1=("2022-01-01", "patient_message"), c1=("2023-01-01", "clinical_note"))
    r = reconcile_eligibility([_dx("m1", "nmosd")], {}, [], docs, {}, RULES)
    assert r["dx_category"] == "nmosd" and "no clinic letter or hospital note" in r["dx_conflict"]
    r = reconcile_eligibility([_dx("c1", "nmosd", hyp=True)], {}, [], docs, {}, RULES)
    assert r["dx_category"] == "unclear" and "only possible/suspected" in r["dx_conflict"]
    assert r["summary"]["possible_only"] == ["nmosd"]
    r = reconcile_eligibility([_dx("c1", "nmosd", neg=True)], {}, [], docs, {}, RULES)
    assert r["dx_category"] == "unclear" and "no diagnosis statement" in r["dx_conflict"]
    assert r["summary"]["ruled_out"] == ["nmosd"]


def test_follow_up_from_diagnosis_to_last_clinical_note():
    docs = _docs(c1=("2024-01-20", "clinical_note"), c2=("2024-06-01", "clinical_note"),
                 x1=("2026-01-01", "low_value", "Nursing Note"))
    items = [_dx("c1", "nmosd"), _dx("c2", "nmosd")]
    r = reconcile_eligibility(items, {"date_nmo_diagnosis": "2024-01-01"}, [], docs, {}, RULES)
    assert r["followup_12m"] == "0" and r["followup_conflict"] == "", "5 months is clearly short: no flag"
    assert r["summary"]["followup"]["to"] == "2024-06-01", "a nursing note is not a follow-up contact"
    assert r["summary"]["followup"]["to_source"] == "clinical note"
    docs["c3"] = DocMeta(date(2025, 2, 1), "clinical_note")
    r = reconcile_eligibility(items, {"date_nmo_diagnosis": "2024-01-01"}, [], docs, {}, RULES)
    assert r["followup_12m"] == "1" and r["summary"]["followup"]["months"] == 13.0
    assert "just over the 12-month threshold (13 months)" in r["followup_conflict"], "borderline"
    docs["c3"] = DocMeta(date(2024, 11, 1), "clinical_note")
    r = reconcile_eligibility(items, {"date_nmo_diagnosis": "2024-01-01"}, [], docs, {}, RULES)
    assert r["followup_12m"] == "0" and "just under the 12-month threshold" in r["followup_conflict"]
    docs["c3"] = DocMeta(date(2026, 1, 1), "clinical_note")
    r = reconcile_eligibility(items, {"date_nmo_diagnosis": "2024-01-01"}, [], docs, {}, RULES)
    assert r["followup_12m"] == "1" and r["followup_conflict"] == ""
    r = reconcile_eligibility(items, {}, [], docs, {}, RULES)
    assert r["summary"]["followup"]["from"] == "2024-01-20"
    assert "follow-up counted from the first note stating NMOSD" in r["followup_conflict"]


def test_patient_messages_and_the_dates_they_give_count_as_follow_up():
    """10.11.26: messages to the patient (infusion or appointment dates) are part of the patient's treatment."""
    dx = {"date_nmo_diagnosis": "2024-01-01"}
    base = dict(c1=("2024-01-20", "clinical_note"), c2=("2024-06-01", "clinical_note"))
    items = [_dx("c1", "nmosd")]
    # a patient message after the last clinic letter
    r = reconcile_eligibility(items, dx, [], _docs(**base, m1=("2025-03-01", "patient_message", "Patient Email")),
                              {}, RULES)
    assert r["followup_12m"] == "1" and r["summary"]["followup"]["to_source"] == "patient message"
    # an Outreach reminder is "low value" by class but a message by description
    r = reconcile_eligibility(items, dx, [], _docs(**base, o1=("2025-03-01", "low_value", "Outreach")), {}, RULES)
    assert r["summary"]["followup"]["to"] == "2025-03-01" and r["followup_12m"] == "1"
    # the infusion date a message gives counts too, and is its evidence
    docs = _docs(**base, m1=("2024-12-01", "patient_message", "Patient Email"))
    r = reconcile_eligibility([*items, _visit("m1", "2024-12-01", "2025-02-15")], dx, [], docs, {}, RULES)
    fu = r["summary"]["followup"]
    assert fu["to"] == "2025-02-15" and fu["to_source"] == "infusion date given in a patient message"
    assert r["followup_evidence"] == ["ev-m1"]
    assert "reached only through a date given in a patient message" in r["followup_conflict"], \
        "the date is after the last note and decides the 12 months: the reviewer checks it took place"
    # cancelled, tentative and implausibly distant dates are ignored
    for v in (_visit("m1", "2024-12-01", "2025-02-15", neg=True), _visit("m1", "2024-12-01", "2025-02-15", hyp=True),
              _visit("m1", "2024-12-01", "2027-01-15")):
        r = reconcile_eligibility([*items, v], dx, [], docs, {}, RULES)
        assert r["summary"]["followup"]["to"] == "2024-12-01", v.fields


def test_coexisting_diagnoses_do_not_compete_with_nmosd():
    """Patients often carry several diagnoses (e.g. NMOSD and Sjögren's): only MOGAD/MS/AE/infarct compete."""
    docs = _docs(c1=("2022-01-01", "clinical_note"), c2=("2024-03-01", "clinical_note"))
    # the latest note lists NMOSD and a co-existing autoimmune disease
    r = reconcile_eligibility([_dx("c1", "nmosd"), _dx("c2", "nmosd", 10), _dx("c2", "other", 50)], {}, [], docs, {},
                              RULES)
    assert r["dx_category"] == "nmosd" and r["dx_conflict"] == "no AQP4-IgG result found"
    assert r["summary"]["coexisting"] == ["other"]
    # the newest note names ONLY another diagnosis: still not the proposal, but flagged for the reviewer
    r = reconcile_eligibility([_dx("c1", "nmosd"), _dx("c2", "other")], {}, [], docs, {}, RULES)
    assert r["dx_category"] == "nmosd" and "the most recent notes state another diagnosis (other)" in r["dx_conflict"]
    # no NMOSD-spectrum statement at all
    r = reconcile_eligibility([_dx("c2", "other")], {}, [], docs, {}, RULES)
    assert r["dx_category"] == "other" and "no NMOSD-spectrum diagnosis stated" in r["dx_conflict"]


def test_a_recent_not_nmosd_statement_is_flagged():
    docs = _docs(c1=("2019-01-01", "clinical_note"), c2=("2024-03-01", "clinical_note"),
                 c3=("2024-05-01", "patient_message"))
    items = [_dx("c1", "nmosd"), _dx("c2", "nmosd", neg=True), _dx("c3", "nmosd")]
    r = reconcile_eligibility(items, {}, [{"values": {"type_test": "aqp4", "aqp4_assay_status": "1"}}], docs, {},
                              RULES)
    assert r["dx_category"] == "nmosd" and "a recent note says the patient does not have nmosd" in r["dx_conflict"]
    assert r["summary"]["ruled_out"] == ["nmosd"] and "e-c2-0" in r["dx_evidence"]


@pytest.mark.parametrize("proposal,ab,note", [
    ("ms", {"aqp4": "1"}, "AQP4-IgG positive result found"),
    ("nmosd", {"aqp4": "2", "mog": "1"}, "check for MOGAD"),
    ("nmosd", {"aqp4": "2"}, "seronegative"),
    ("nmosd", {}, "no AQP4-IgG result found"),
    ("nmosd", {"aqp4": "1"}, None),
])
def test_antibody_results_are_checked_against_the_diagnosis(proposal, ab, note):
    docs = _docs(c1=("2023-01-01", "clinical_note"))
    abs_ = []
    if "aqp4" in ab:
        abs_.append({"values": {"type_test": "aqp4", "aqp4_assay_status": ab["aqp4"]}})
    if "mog" in ab:
        abs_.append({"values": {"type_test": "mog", "mog_test_positive": ab["mog"]}})
    r = reconcile_eligibility([_dx("c1", proposal)], {}, abs_, docs, {}, RULES)
    if note:
        assert note in r["dx_conflict"]
    else:
        assert r["dx_conflict"] == ""


def test_diagnosis_schema_has_the_category_codes():
    dic = load_dictionary(DICT)
    m = output_model("diagnosis", dic)
    base = {"date_nmo_diagnosis": "not_established", "date_nmo_estimate": "not_established", "otherdx": "UNK",
            "dx_other": "", "quote": "consistent with MOGAD", "certainty": "high", "negated": False,
            "hypothetical": False}
    assert m.model_validate({"items": [base | {"dx_category": "mogad"}]})
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        m.model_validate({"items": [base | {"dx_category": "MOGAD"}]})


# =============================================================================================
# pipeline: proposals on the synthetic patients
# =============================================================================================
def _gold_eligibility(pipeline):
    rmap = dict(csv.reader((pipeline["synthetic"] / "record_map.csv").open(encoding="utf-8")))
    gold = {r["record_id"]: r for r in csv.DictReader((pipeline["synthetic"] / "gold_eligibility.csv").open())}
    return {sid: gold[rmap[p["empi"]]] for sid, p in pipeline["link"]["patients"].items()}


def test_diagnosis_check_proposals_match_gold(pipeline):
    s = ReviewStore(pipeline["root"] / "deid" / "gate_props.sqlite", pipeline["extract"], pipeline["deid"], DICT)
    for sid, g in _gold_eligibility(pipeline).items():
        f = {x.field: x for x in s.fields(sid, GATE, GATE)}
        assert f["dx_category"].proposed == g["dx_category"], sid
        assert f["followup_12m"].proposed == g["followup_12m"]
        assert not f["dx_category"].conflict, f["dx_category"].conflict
        ev = s.evidence(f["dx_category"].evidence_ids, sid, newest_first=True)
        assert ev and all(e["field"] == "dx_category" for e in ev)
        assert [e["doc_date"] for e in ev] == sorted((e["doc_date"] for e in ev), reverse=True)
        summ = s.gate_summary(sid)
        assert summ["basis"] == "clinic letters and hospital notes"
        assert abs(summ["followup"]["months"] - float(g["followup_months"])) <= 2
        if g["record_id"] == "SYN-01":   # P1: an Outreach reminder after the last visit gives a later infusion date
            assert summ["followup"]["to_source"] == "infusion date given in a patient message"
            ev = s.evidence(f["followup_12m"].evidence_ids, sid)
            assert any(e["field_group"] == "followup" and e["field"] == "visit_date" for e in ev)
            doc = s.document(next(e["doc_id"] for e in ev if e["field_group"] == "followup"), sid)
            assert doc["report_description"] == "Outreach" and doc["relevance_class"] == "low_value"
        else:
            assert summ["followup"]["to_source"] == "clinical note"
    s.close()


def test_outreach_notes_are_selected_by_description_for_the_followup_group():
    from nmosd.extract.prefilter import is_candidate

    text = "Reminder: your infusion is scheduled for 3/4/2026."
    assert is_candidate("followup", "low_value", text, {"low_value"}, "Outreach") == (True, "selected")
    assert is_candidate("followup", "patient_message", text, {"low_value"}, "Patient Email")[0]
    assert is_candidate("treatment", "low_value", text, {"low_value"}, "Outreach") == (False, "skipped_class")
    assert is_candidate("followup", "low_value", text, {"low_value"}, "Nursing Note") == (False, "skipped_class")


# =============================================================================================
# review store: the gate
# =============================================================================================
@pytest.fixture()
def store(pipeline, tmp_path):
    s = ReviewStore(tmp_path / "deid" / "review.sqlite", pipeline["extract"], pipeline["deid"], DICT)
    yield s
    s.close()


def _by_category(s):
    out = {}
    for sid in s.patients():
        out.setdefault(s.fields(sid, GATE, GATE)[0].proposed, []).append(sid)
    return out


def test_gate_states_and_progress(store):
    cats = _by_category(store)
    mogad, a, b, c = cats["mogad"][0], *cats["nmosd"][:3]
    assert store.gate(a).status == "pending" and store.progress(a)[1] > 20
    for sid in (mogad, a):
        for f in ("dx_category", "followup_12m"):
            store.record(REVIEWER, "accept", sid, GATE, GATE, f)
    g = store.gate(mogad)
    assert g.status == "excluded" and "MOG" in g.reason
    assert store.progress(mogad) == (2, 2), "an excluded patient needs nothing else"
    assert store.gate(a).included
    store.record(REVIEWER, "accept", b, GATE, GATE, "dx_category")
    store.record(REVIEWER, "correct", b, GATE, GATE, "followup_12m", new_value="0", reason="only 8 months")
    assert store.gate(b).status == "excluded" and "less than 12" in store.gate(b).reason
    store.record(REVIEWER, "unresolved", c, GATE, GATE, "dx_category", reason="ask the PI")
    assert store.gate(c).status == "unresolved"
    assert store.labels("dx_category")["mogad"].startswith("MOG")
    assert store.reviewed_values(a).keys() <= set(INSTRUMENT_FIELDS), "the check is never an export instrument"


@pytest.fixture()
def borderline_store(pipeline, tmp_path):
    """A store over a COPY of the synthetic extraction DB in which one NMOSD patient's check is flagged."""
    import sqlite3

    ex = tmp_path / "deid" / "extract.sqlite"
    ex.parent.mkdir(parents=True)
    src = sqlite3.connect(pipeline["extract"])
    dst = sqlite3.connect(ex)
    src.backup(dst)
    src.close()
    s = ReviewStore(tmp_path / "deid" / "review.sqlite", pipeline["extract"], pipeline["deid"], DICT)
    sid = _by_category(s)["nmosd"][0]
    s.close()
    dst.execute("UPDATE proposals SET conflict_flag=1, conflict_note='recent notes also state mogad' WHERE "
                "study_id=? AND instrument=? AND field='dx_category'", (sid, GATE))
    dst.commit()
    dst.close()
    review = tmp_path / "deid" / "review.sqlite"
    s = ReviewStore(review, ex, pipeline["deid"], DICT)
    yield s, sid, ex, review
    s.close()


def test_borderline_cases_need_a_reason_before_exclusion(borderline_store):
    s, sid, _, _ = borderline_store
    assert s.gate_borderline(sid) == ["recent notes also state mogad"]
    clear = next(p for p in s.patients() if p != sid and s.fields(p, GATE, GATE)[0].proposed == "mogad")
    assert s.gate_borderline(clear) == [], "a clear MOGAD case is not borderline"
    s.record(REVIEWER, "accept", clear, GATE, GATE, "dx_category")      # no reason needed
    assert s.gate(clear).status == "excluded"
    with pytest.raises(ValueError, match="Borderline case"):
        s.record(REVIEWER, "correct", sid, GATE, GATE, "dx_category", new_value="mogad")
    with pytest.raises(ValueError, match="Borderline case"):
        s.record(REVIEWER, "correct", sid, GATE, GATE, "followup_12m", new_value="0")
    assert s.gate(sid).status == "pending", "nothing was recorded"
    s.record(REVIEWER, "accept", sid, GATE, GATE, "dx_category")          # including needs no reason
    s.record(REVIEWER, "correct", sid, GATE, GATE, "dx_category", new_value="mogad",
             reason="MOG-IgG 1:1000 and AQP4-IgG negative twice; neurology letter of the last visit says MOGAD")
    assert s.gate(sid).status == "excluded"
    log = [r for r in s.history(sid) if r["instrument"] == GATE]
    assert log[-1]["reason"].startswith("MOG-IgG")


@pytest.mark.streamlit
def test_app_lets_the_reviewer_look_at_a_borderline_case_read_only(pipeline, borderline_store):
    st_testing = pytest.importorskip("streamlit.testing.v1")
    import sys

    s, sid, ex, review = borderline_store
    at = st_testing.AppTest.from_file(str(REPO / "src/nmosd/review/app.py"), default_timeout=60)
    argv = sys.argv
    sys.argv = ["app.py", "--extract", str(ex), "--deid", str(pipeline["deid"]), "--review", str(review),
                "--dictionary", str(DICT)]
    try:
        at.run()
        at.text_input(key="login_name").input(REVIEWER)
        at.button(key="login").click().run()
        at.selectbox(key="patient").set_value(sid).run()
        assert any("Borderline case" in w.value for w in at.warning)
        assert at.radio(key="instrument").options == ["Diagnosis check"]
        at.checkbox(key=f"{sid}:preview").check().run()
        at.radio(key="instrument").set_value("relapse").run()
        assert not at.exception
        assert any("Read-only preview" in i.value for i in at.info)
        keys = [b.key for b in at.button if b.key]
        assert any(k.endswith(":ev") for k in keys), "evidence is viewable"
        assert not any(k.endswith((":acc", ":cor")) or k.startswith("del") or k == "add_inst" for k in keys)
        # excluding without a reason is refused on screen
        at.radio(key="instrument").set_value(GATE).run()
        at.selectbox(key=f"{sid}:{GATE}:dx_category:sb").set_value("mogad").run()
        at.button(key=f"{sid}:{GATE}:dx_category:cor").click().run()
        assert any("Borderline case" in e.value for e in at.error)
        assert s.gate(sid).status == "pending"
    finally:
        sys.argv = argv


def test_export_skips_patients_not_included(pipeline, store, tmp_path):
    cats = _by_category(store)
    included = cats["nmosd"][0]
    for f in ("dx_category", "followup_12m"):
        store.record(REVIEWER, "accept", cats["mogad"][0], GATE, GATE, f)
        store.record(REVIEWER, "accept", included, GATE, GATE, f)
    store.record(REVIEWER, "unresolved", cats["nmosd"][1], GATE, GATE, "dx_category")
    for inst in INSTRUMENT_FIELDS:
        for key in store.instances(included, inst):
            for fs in store.fields(included, inst, key):
                store.record(REVIEWER, "accept" if fs.proposed is not None else "unresolved", included, inst, key,
                             fs.field)
    buf = io.StringIO()
    out_dir = tmp_path / "deid" / "export"
    out_dir.mkdir(parents=True)
    review = tmp_path / "deid" / "review.sqlite"   # the `store` fixture's database
    assert run_export(pipeline["extract"], review, DICT, out_dir, deid=pipeline["deid"], stdout=buf) == 0
    rows = list(csv.DictReader((out_dir / EXPORT_NAME).open(encoding="utf-8")))
    assert {r["record_id"] for r in rows} == {included}
    out = buf.getvalue()
    assert "patients exported: 1" in out and "excluded at the diagnosis check: 1" in out
    assert "diagnosis check unresolved (warning): 1" in out and "diagnosis check not done (warning): 2" in out


# =============================================================================================
# output format: the project's REDCap export as a template
# =============================================================================================
SECRET = "Zebulon Quixote-Marchbanks"   # stands in for real data in the template's data rows


def _template(path, dic, style: CsvStyle, id_col="record_id", drop=()):
    cols = [id_col, "redcap_repeat_instrument", "redcap_repeat_instance",
            *[c for c in expected_export_order(dic) if c != "record_id" and c not in drop]]
    rows = [{id_col: "4410", "dx_other": SECRET, "dob": "1961-02-03", "mrn": "7777777"}]
    write_csv(path, cols, rows, style)
    return cols


def test_read_template_header_and_style(tmp_path):
    dic = load_dictionary(DICT)
    p = tmp_path / "t.csv"
    cols = _template(p, dic, CsvStyle(bom=True, newline="\r\n", quote_all=True))
    got, style = read_template(p)
    assert got == cols and style == CsvStyle(True, "\r\n", True)
    cols = _template(p, dic, CsvStyle())
    assert read_template(p) == (cols, CsvStyle(False, "\r\n", False))
    write_csv(p, ["a", "b"], [{"a": "1"}], CsvStyle(newline="\n"))
    assert read_template(p)[1].newline == "\n"


def test_standard_order_puts_complete_after_each_form():
    cols = standard_columns(load_dictionary(DICT), {"demographics", "relapse"})
    assert cols[:3] == ["record_id", "redcap_repeat_instrument", "redcap_repeat_instance"]
    assert cols.index("demographics_complete") < cols.index("date_onset") < cols.index("relapse_complete")
    assert cols[-1] == "relapse_complete" and "bmi" not in cols and "mrn" not in cols
    # missing-data codes get their own checkbox columns, right after the field's choices (as in REDCap's export)
    i = cols.index("relapse_phenotype___7")
    assert cols[i + 1:i + 3] == ["relapse_phenotype___unk", "relapse_phenotype___na"]


@pytest.mark.parametrize("fmt,expected", [("YYYY-MM-DD", "2019-03-04"), ("MM/DD/YYYY", "03/04/2019"),
                                          ("M/D/YYYY", "3/4/2019"), ("DD/MM/YYYY", "04/03/2019"),
                                          ("D/M/YYYY", "4/3/2019")])
def test_date_formats_round_trip(fmt, expected):
    assert format_date("2019-03-04", fmt) == expected
    assert parse_date(expected, fmt) == date(2019, 3, 4)
    assert parse_date("2019-03-04", fmt) == date(2019, 3, 4), "YYYY-MM-DD is always read"
    assert format_date("UNK", fmt) == "UNK" and parse_date("NA", fmt) is None and format_date("", fmt) == ""
    with pytest.raises(ValueError):
        format_date("2019-03-04", "DD.MM.YY")


def test_export_reidentify_and_evaluate_with_slash_dates(pipeline, reviewed_copy, tmp_path):
    """The project's export writes dates month first with slashes: our files do the same, end to end."""
    out_dir = tmp_path / "deid" / "export"
    out_dir.mkdir(parents=True)
    buf = io.StringIO()
    assert run_export(pipeline["extract"], reviewed_copy, DICT, out_dir, deid=pipeline["deid"],
                      date_format="MM/DD/YYYY", stdout=buf) == 0
    assert "dates as MM/DD/YYYY" in buf.getvalue()
    rows = list(csv.DictReader((out_dir / EXPORT_NAME).open(encoding="utf-8")))
    onsets = [r["date_onset"] for r in rows if r["date_onset"]]
    assert onsets and all(re.fullmatch(r"\d{2}/\d{2}/\d{4}", v) for v in onsets)
    final_dir = tmp_path / "work" / "final"
    assert run_reidentify(out_dir / EXPORT_NAME, pipeline["linkage"], final_dir,
                          record_map=pipeline["synthetic"] / "record_map.csv", passphrase=PASSPHRASE,
                          add_identifiers=True, date_format="MM/DD/YYYY", stdout=io.StringIO()) == 0
    final = list(csv.DictReader((final_dir / FINAL_NAME).open(encoding="utf-8")))
    assert any(r["dob"] == "03/04/1988" for r in final), "DOB in the project's format too"
    gold = list(csv.DictReader((pipeline["synthetic"] / "gold_redcap.csv").open(encoding="utf-8")))
    exact = {(g["record_id"], g["date_onset"]) for g in gold if g["date_onset"] and g["date_attack_estimate"] == "1"
             and g["record_id"] != "SYN-03"}
    got = {(r["record_id"], f"{r['date_onset'][6:]}-{r['date_onset'][:2]}-{r['date_onset'][3:5]}") for r in final
           if r["date_onset"]}
    assert exact <= got, "un-shifting restores the original dates exactly"
    # evaluation reads a gold file with slash dates (as the project's export has) the same as YYYY-MM-DD
    slash_gold = tmp_path / "work" / "gold_slash.csv"
    cols = list(gold[0])
    write_csv(slash_gold, cols, [{k: (format_date(v, "MM/DD/YYYY") if k in DATE_ESTIMATE and v else v)
                                  for k, v in g.items()} for g in gold])
    res = {}
    for name, g in (("iso", pipeline["synthetic"] / "gold_redcap.csv"), ("slash", slash_gold)):
        rep = tmp_path / "work" / f"eval_{name}.md"
        run_evaluate(g, out_dir / EXPORT_NAME, None, pipeline["linkage"], pipeline["synthetic"] / "record_map.csv",
                     None, out=rep, passphrase=PASSPHRASE, stdout=io.StringIO())
        res[name] = rep.read_text().split("```json\n", 1)[1]
    assert res["iso"] == res["slash"] and '"recall": 1.0' in res["iso"]


def test_missing_data_codes_export_to_their_own_checkbox_columns(pipeline, store, tmp_path):
    sid = _by_category(store)["nmosd"][0]
    for f in ("dx_category", "followup_12m"):
        store.record(REVIEWER, "accept", sid, GATE, GATE, f)
    key = store.instances(sid, "relapse")[0]
    store.record(REVIEWER, "correct", sid, "relapse", key, "relapse_phenotype", new_value='["UNK"]',
                 reason="phenotype not documented")
    out_dir = tmp_path / "deid" / "export"
    out_dir.mkdir(parents=True)
    buf = io.StringIO()
    assert run_export(pipeline["extract"], tmp_path / "deid" / "review.sqlite", DICT, out_dir, deid=pipeline["deid"],
                      stdout=buf) == 0, buf.getvalue()
    row = next(r for r in csv.DictReader((out_dir / EXPORT_NAME).open(encoding="utf-8"))
               if r["redcap_repeat_instrument"] == "relapse")
    assert row["relapse_phenotype___unk"] == "1" and row["relapse_phenotype___na"] == "0"
    assert all(row[f"relapse_phenotype___{c}"] == "0" for c in range(1, 8))


@pytest.mark.parametrize("values,style", [
    (["03/04/2019", "12/25/2019", "01/02/2020"], "MM/DD/YYYY"),
    (["3/4/2019", "12/25/2019"], "M/D/YYYY"),
    (["25/12/2019", "04/03/2019"], "DD/MM/YYYY"),
    (["2019-03-04"], "YYYY-MM-DD"),
])
def test_profile_suggests_the_date_format(tmp_path, values, style):
    p = tmp_path / "e.csv"
    write_csv(p, ["record_id", "date_onset"], [{"record_id": str(i), "date_onset": v} for i, v in enumerate(values)])
    buf = io.StringIO()
    res = profile_csv(p, None, out=buf)
    assert res["date_format"] == style and f"Suggested setting: export.date_format: {style}" in buf.getvalue()
    assert not any(v in buf.getvalue() for v in values), "counts only, never a date"


@pytest.fixture(scope="module")
def reviewed_copy(pipeline, tmp_path_factory):
    """A review DB with every proposal accepted (diagnosis check first)."""
    review = tmp_path_factory.mktemp("rev") / "deid" / "review.sqlite"
    review.parent.mkdir(parents=True)
    s = ReviewStore(review, pipeline["extract"], pipeline["deid"], DICT)
    for sid in s.patients():
        for f in ("dx_category", "followup_12m"):
            s.record(REVIEWER, "accept", sid, GATE, GATE, f)
        for inst in INSTRUMENT_FIELDS:
            for key in s.instances(sid, inst):
                for fs in s.fields(sid, inst, key):
                    s.record(REVIEWER, "accept" if fs.proposed is not None else "unresolved", sid, inst, key, fs.field)
    s.close()
    return review


def test_export_and_reidentify_follow_the_template(pipeline, reviewed_copy, tmp_path):
    dic = load_dictionary(DICT)
    tpl = tmp_path / "work" / "NMOSDDatabase_DATA_template.csv"
    tpl.parent.mkdir(parents=True)
    cols = _template(tpl, dic, CsvStyle(bom=True, newline="\r\n", quote_all=True), id_col="study_id")
    header_bytes = tpl.read_bytes().split(b"\r\n", 1)[0] + b"\r\n"
    out_dir = tmp_path / "deid" / "export"
    out_dir.mkdir(parents=True)
    buf = io.StringIO()
    rc = run_export(pipeline["extract"], reviewed_copy, DICT, out_dir, deid=pipeline["deid"], template=tpl, stdout=buf)
    assert rc == 0, buf.getvalue()
    raw = (out_dir / EXPORT_NAME).read_bytes()
    assert raw.startswith(header_bytes), "same header, BOM and quoting as the template"
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert SECRET.encode() not in raw and b"4410" not in raw, "template data rows are never read"
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    assert list(rows[0]) == cols and all(r["study_id"].startswith("NMO-") for r in rows)
    assert all(r["mrn"] == "" and r["dob"] == "" and r["bmi"] == "" for r in rows), "identifiers/calc left blank"
    assert "template layout" in buf.getvalue() and "record ID column renamed" in buf.getvalue()
    # re-identification keeps the layout
    final_dir = tmp_path / "work" / "final"
    rc = run_reidentify(out_dir / EXPORT_NAME, pipeline["linkage"], final_dir,
                        record_map=pipeline["synthetic"] / "record_map.csv", passphrase=PASSPHRASE,
                        add_identifiers=True, stdout=io.StringIO())
    assert rc == 0
    final_raw = (final_dir / FINAL_NAME).read_bytes()
    assert final_raw.startswith(header_bytes)
    final = list(csv.DictReader(io.StringIO(final_raw.decode("utf-8-sig"))))
    assert {r["study_id"] for r in final} == {"SYN-01", "SYN-02", "SYN-04", "SYN-05"}
    assert any(r["dob"] == "1988-03-04" for r in final) and "record_id" not in final[0]


def test_template_missing_a_filled_column_is_an_error(pipeline, reviewed_copy, tmp_path):
    tpl = tmp_path / "work" / "t.csv"
    tpl.parent.mkdir(parents=True)
    _template(tpl, load_dictionary(DICT), CsvStyle(), drop=("mri_relapse",))
    out_dir = tmp_path / "deid" / "export"
    out_dir.mkdir(parents=True)
    buf = io.StringIO()
    rc = run_export(pipeline["extract"], reviewed_copy, DICT, out_dir, deid=pipeline["deid"], template=tpl, stdout=buf)
    assert rc == 1 and "missing from the template, left out: mri_relapse" in buf.getvalue()


# =============================================================================================
# profile-redcap-csv: metadata only
# =============================================================================================
def test_profile_prints_layout_but_never_values(tmp_path):
    dic = load_dictionary(DICT)
    cols = ["record_id", "redcap_repeat_instrument", "redcap_repeat_instance",
            *[c for c in expected_export_order(dic) if c != "record_id"]]
    rows = [
        {"record_id": "SYN-77", "mrn": "4417263", "dob": "1988-03-04", "sex": "2", "dx_other": SECRET,
         "date_nmo_diagnosis": "2016-09-14", "demographics_complete": "2"},
        {"record_id": "SYN-77", "redcap_repeat_instrument": "relapse", "redcap_repeat_instance": "1",
         "date_onset": "2016-07-15", "relapse_phenotype___1": "1", "relapse_complete": "2"},
        {"record_id": "SYN-77", "redcap_repeat_instrument": "relapse", "redcap_repeat_instance": "3",
         "date_onset": "3/4/2018"},
        {"record_id": "SYN-78", "redcap_repeat_instrument": "Odd Form Name!", "redcap_repeat_instance": "1",
         "dx_other": "line one\nline two"},
    ]
    p = tmp_path / "export.csv"
    write_csv(p, cols, rows, CsvStyle(bom=True))
    buf = io.StringIO()
    res = profile_csv(p, DICT, out=buf)
    out = buf.getvalue()
    for secret in [SECRET, "SYN-77", "SYN-78", "4417263", "1988-03-04", "2016-09-14", "3/4/2018", "Odd Form",
                   "line one"]:
        assert secret not in out, secret
    assert f"Columns: {len(cols)}; data rows: 4; distinct records: 2" in out
    assert "UTF-8 with BOM" in out and "| relapse | 2 | 3 |" in out and "<non-standard>" in out
    assert "| date_onset | 2 | date Y-M-D 1, date M/D/Y or D/M/Y 1 |" in out
    assert "Cells containing line breaks: 1" in out
    assert "same as REDCap's standard export order" in out and res["export_missing"] == []
    # reordered columns are reported by name only
    write_csv(p, [cols[0], cols[1], cols[2], cols[4], cols[3], *cols[5:]], rows)
    buf = io.StringIO()
    profile_csv(p, DICT, out=buf)
    assert "differs from position 2 (dob in the CSV, mrn expected)" in buf.getvalue()


def test_cli_has_the_new_commands():
    ap = build_parser()
    a = ap.parse_args(["profile-redcap-csv", "--file", "x.csv", "--no-dictionary"])
    assert a.fn.__name__ == "cmd_profile_redcap_csv"
    a = ap.parse_args(["export", "--template", "t.csv"])
    assert a.template == "t.csv" and not a.no_template


def test_settings_example_has_a_template_path():
    from nmosd.config import load_settings

    s = load_settings(REPO / "config" / "settings.example.yaml")
    assert "redcap_template" in s.get("paths") and s.path("redcap_template") is None

