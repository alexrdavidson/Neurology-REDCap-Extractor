"""First-draft review feedback (13.11.26), on synthetic data only.

1. "On rituximab since 2019" gives the treatment's start date (no more "no start date found").
2. The evidence highlight lands on the quote in the note as it is now, in whole words; demographic mentions
   whose quote lacks the field's own wording (a medication list quoted for race) are not evidence.
3. Comorbidities (REDCap `comorbidities` / `autoimmune`): every condition in the medical history is found and
   listed for the reviewer, including those outside the REDCap categories.
4. With no diagnosis date in the notes, the first note stating NMOSD gives an estimated one.
5. Ethnicity comes from the structured data only (tests in test_phase5_6 / test_phase7_9).
6. The note opens scrolled to the highlighted passage.
7. "Evidence (n)" counts the passages the panel shows, not the evidence rows behind them.
Also: run hashes are per field group, so adding a group does not re-run the others.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from datetime import date

import pytest

from nmosd.config import load_settings
from nmosd.extract import run as extract_run
from nmosd.extract.quote_verify import locate_quote
from nmosd.extract.stage2_reconcile import (
    DocMeta,
    Item,
    diagnosis_date_fallback,
    reconcile_comorbidities,
    reconcile_eligibility,
    reconcile_treatments,
)
from nmosd.redcap.export import EXPORT_NAME, run_export
from nmosd.redcap.schemas import NO_CATEGORY, schemas_for
from nmosd.review.app import note_html
from nmosd.review.store import GATE, ReviewStore
from nmosd.textproc.dates import date_and_estimate
from tests.conftest import REPO

DICT = REPO / "tests/fixtures/redcap/data_dictionary_pilot.csv"
REVIEWER = "Synthetic Reviewer"


# =============================================================================================
# 1. treatment start from "since <date>"
# =============================================================================================
def _tx(i, event, quote, doc_date, d=None, est=None, drug="1"):
    return Item(f"t{i}", "treatment", f"D{i}", doc_date, 0, {"maintenance_therapy": drug, "event": event},
                event_date=d, estimate=est, evidence_ids=[f"e{i}"], quote=quote)


def test_date_and_estimate_follows_the_extraction_rules():
    assert date_and_estimate("2019, her B cells counts have", 2025) == ("2019-07-01", "3")
    assert date_and_estimate("March 2021 and doing well", 2022) == ("2021-03-15", "2")
    assert date_and_estimate("2/14/2019.", 2022) == ("2019-02-14", "1")
    assert date_and_estimate("her last visit on 3/2/2024", 2024, max_start=0) is None
    assert date_and_estimate("no date here", 2024) is None


def test_ongoing_since_a_date_gives_the_start():
    rtx = [_tx(1, "ongoing", "although [PATIENT] has been on rituximab since 2019, her B cells counts have",
               "2025-07-20"),
           _tx(2, "ongoing", "Continue rituximab 1 g every 6 months", "2025-09-01")]
    (ep,) = reconcile_treatments(rtx)
    v = ep["values"]
    assert v["date_starting_maint"] == "2019-07-01" and v["date_maintenance_estimate"] == "3", "year only: code 3"
    assert v["still_on_therapy"] == "1" and "no start date" not in ep["conflict"]
    assert ep["evidence"] == ["e1", "e2"], "each passage once"
    (ep,) = reconcile_treatments([_tx(3, "ongoing", "on inebilizumab since March 2021", "2022-01-05", drug="5")])
    assert (ep["values"]["date_starting_maint"], ep["values"]["date_maintenance_estimate"]) == ("2021-03-15", "2")
    # "since" not followed by a date is no start
    (ep,) = reconcile_treatments([_tx(4, "ongoing", "stable on rituximab since her last visit on 3/2/2024",
                                      "2024-09-01")])
    assert ep["values"]["date_starting_maint"] is None and "no start date found" in ep["conflict"]
    # a dated start mention wins over "since"
    (ep,) = reconcile_treatments([rtx[0], _tx(5, "start", "first rituximab infusion 2/10/2019", "2019-02-10",
                                              d="2019-02-10", est="1")])
    assert ep["values"]["date_starting_maint"] == "2019-02-10"
    # "since 2019" in a note written in March 2019 cannot mean July
    (ep,) = reconcile_treatments([_tx(6, "ongoing", "on rituximab since 2019", "2019-03-01")])
    assert ep["values"]["date_starting_maint"] == "2019-03-01" and ep["values"]["date_maintenance_estimate"] == "3"


# =============================================================================================
# 2. highlighting
# =============================================================================================
def test_highlight_is_re_found_in_the_current_note_in_whole_words():
    text = ("Medications at End of Encounter\n\ncalcium carbonate (500 mg elemental) tablet\n\n"
            "Social History\nRace: White. Lives with her sister.\n\nRace: White (repeated below)")
    first, second = text.index("Race: White"), text.rindex("Race: White")
    # stored offsets from before the note was de-identified again: found again, nearest occurrence wins
    assert locate_quote(text, "Race: White", 0, 11) == (first, first + 11)
    assert locate_quote(text, "Race: White", second - 3, second + 8) == (second, second + 11)
    # a match that starts inside a word is widened to the whole word
    s = text.index("blet")
    assert locate_quote(text, "blet", s, s + 4) == (text.index("tablet"), s + 4)
    assert locate_quote(text, "Ethnicity: Hispanic", 10, 30) is None, "not in the note: no misleading highlight"
    # whitespace and case differences still match
    assert locate_quote(text, "race:   white.  lives", None, None) == (first, first + len("Race: White. Lives"))


def test_note_opens_scrolled_to_the_highlight():
    text = "a <b> note\nwith the quote here"
    page = note_html(text, text.index("quote"), text.index("quote") + 5)
    assert "<mark id='hit'>quote</mark>" in page and "&lt;b&gt;" in page, "escaped, quote marked"
    assert "box.scrollTop" in page and "scrollIntoView" not in page, "scrolls the box only, never the page"
    assert "<mark" not in note_html("no quote", None, None)


# =============================================================================================
# 3. comorbidities
# =============================================================================================
def _cm(i, name, cat, auto=None, doc="D1", doc_date="2024-01-01", neg=False, hyp=False):
    fields = {"condition": name, "comorbidities": cat} | ({"autoimmune": auto} if auto else {})
    return Item(f"c{i}", "comorbidity", doc, doc_date, 0, fields, negated=neg, hypothetical=hyp,
                evidence_ids=[f"e{i}"])


def test_comorbidities_tick_categories_and_list_every_condition():
    items = [_cm(1, "Migraine", "3"), _cm(2, "migraine", "3", doc="D2", doc_date="2025-01-01"), _cm(3, "PTSD", "2"),
             _cm(4, "Osteopenia", NO_CATEGORY), _cm(5, "Hashimoto thyroiditis", "1", "1"),
             _cm(6, "Breast cancer", "8", neg=True), _cm(7, "Lupus", "1", "3", hyp=True),
             _cm(8, "Hearing loss", "3"), _cm(9, "Hearing loss", NO_CATEGORY, doc="D2"),
             _cm(10, "Hearing loss", NO_CATEGORY, doc="D3")]
    out = reconcile_comorbidities(items)
    assert out["values"] == {"comorbidities": ["1", "2", "3"], "autoimmune": ["1"]}
    assert [(c["name"], c["category"]) for c in out["conditions"]] == [
        ("Hashimoto thyroiditis", "1"), ("PTSD", "2"), ("Migraine", "3"),
        ("Hearing loss", NO_CATEGORY), ("Osteopenia", NO_CATEGORY)], "outside the categories: listed, not ticked"
    migraine = next(c for c in out["conditions"] if c["name"] == "Migraine")
    assert migraine["documents"] == 2 and migraine["last"] == "2025-01-01"
    assert "Hearing loss: notes give different categories" in out["conflict"]
    assert "e6" not in out["evidence"] and "e7" not in out["evidence"], "ruled-out / suspected: not evidence"
    assert reconcile_comorbidities([_cm(1, "", "9")])["values"]["comorbidities"] == ["9"]
    both = reconcile_comorbidities([_cm(1, "", "9"), _cm(2, "Asthma", "6")])
    assert both["values"]["comorbidities"] == ["6"] and "no past medical history" in both["conflict"]
    assert reconcile_comorbidities([])["values"] == {"comorbidities": None, "autoimmune": None}


def test_comorbidity_schema_offers_the_redcap_codes_and_no_category():
    _model, schema = schemas_for(DICT)["comorbidity"]
    props = schema["properties"]["items"]["items"]["properties"]
    assert set(props["comorbidities"]["enum"]) >= {str(c) for c in range(1, 10)} | {NO_CATEGORY}
    assert "10" in props["autoimmune"]["enum"] and props["condition"]["type"] == "string"


def test_comorbidities_from_the_synthetic_past_medical_history(pipeline, tmp_path):
    s = ReviewStore(tmp_path / "review.sqlite", pipeline["extract"], pipeline["deid"], DICT)
    try:
        for sid in s.patients():
            f = {x.field: x for x in s.fields(sid, "demographics", "demographics")}
            assert json.loads(f["comorbidities"].proposed) == ["2", "3"], sid
            assert f["autoimmune"].proposed is None
            ev = s.evidence(f["comorbidities"].evidence_ids, sid)
            assert ev and {e["field_group"] for e in ev} == {"comorbidity"}
            assert {c["name"]: c["category"] for c in s.conditions(sid)} == {
                "Anxiety": "2", "Migraine without aura": "3", "Vitamin D deficiency": NO_CATEGORY}
        # export: the checkbox columns, once accepted
        sid = next(p for p in s.patients() if s.fields(p, GATE, GATE)[0].proposed == "nmosd")
        for fs in s.fields(sid, GATE, GATE):
            s.record(REVIEWER, "accept", sid, GATE, GATE, fs.field)
        s.record(REVIEWER, "accept", sid, "demographics", "demographics", "comorbidities")
    finally:
        s.close()
    out = tmp_path / "deid" / "export"
    run_export(pipeline["extract"], tmp_path / "review.sqlite", DICT, out, deid=pipeline["deid"], stdout=io.StringIO())
    row = next(r for r in csv.DictReader((out / EXPORT_NAME).open(encoding="utf-8"))
               if r["record_id"] == sid and not r["redcap_repeat_instrument"])
    assert (row["comorbidities___1"], row["comorbidities___2"], row["comorbidities___3"]) == ("0", "1", "1")


# =============================================================================================
# 4. diagnosis date from the first note stating NMOSD
# =============================================================================================
def _dx(doc, cat):
    return Item(f"i-{doc}", "diagnosis", doc, None, 0, {"dx_category": cat}, evidence_ids=[f"e-{doc}"],
                field_evidence={"dx_category": f"e-{doc}"})


def test_no_diagnosis_date_proposes_the_first_note_stating_nmosd():
    from nmosd.config import load_rules

    docs = {"c1": DocMeta(date(2024, 1, 20), "clinical_note"),
            "c2": DocMeta(date(2024, 6, 1), "clinical_note"),
            "c0": DocMeta(date(2023, 5, 1), "clinical_note")}
    el = reconcile_eligibility([_dx("c1", "nmosd"), _dx("c2", "nmosd"), _dx("c0", "ms")], {}, [], docs, {},
                               load_rules("eligibility.yaml"))
    assert el["first_nmosd"] == {"date": "2024-01-20", "evidence": ["e-c1"]}, "the MS note does not count"
    dx = {"values": {"otherdx": "1"}, "evidence": ["e-x"], "conflict": "", "date_evidence": []}
    got = diagnosis_date_fallback(dx, el["first_nmosd"])
    assert got["values"] == {"otherdx": "1", "date_nmo_diagnosis": "2024-01-20", "date_nmo_estimate": "4"}
    assert got["evidence"] == ["e-x", "e-c1"] and "first note that states NMOSD" in got["conflict"]
    stated = {**dx, "values": {"date_nmo_diagnosis": "2016-09-15", "date_nmo_estimate": "2"}}
    assert diagnosis_date_fallback(stated, el["first_nmosd"]) is stated, "a stated date is never replaced"
    assert diagnosis_date_fallback(dx, None) is dx


# =============================================================================================
# 7. evidence counts, and the review screen
# =============================================================================================
def test_evidence_button_counts_passages(pipeline, tmp_path):
    s = ReviewStore(tmp_path / "review.sqlite", pipeline["extract"], pipeline["deid"], DICT)
    try:
        pairs = [(len(fs.evidence_ids), s.passage_count(fs.evidence_ids, sid), len(s.evidence(fs.evidence_ids, sid)))
                 for sid in s.patients() for key in s.instances(sid, "relapse")
                 for fs in s.fields(sid, "relapse", key) if fs.evidence_ids]
    finally:
        s.close()
    assert pairs and all(n == shown for _, n, shown in pairs)
    assert any(rows > n for rows, n, _ in pairs), "one model answer gives several rows but one passage"


@pytest.mark.streamlit
def test_review_screen_shows_conditions_counts_and_scrolled_note(pipeline, tmp_path):
    st_testing = pytest.importorskip("streamlit.testing.v1")
    review = tmp_path / "deid" / "review.sqlite"
    s = ReviewStore(review, pipeline["extract"], pipeline["deid"], DICT)
    sid = next(p for p in s.patients() if s.fields(p, GATE, GATE)[0].proposed == "nmosd")
    for fs in s.fields(sid, GATE, GATE):
        s.record(REVIEWER, "accept", sid, GATE, GATE, fs.field)
    counts = {f"{sid}:{key}:{fs.field}:ev": s.passage_count(fs.evidence_ids, sid)
              for key in s.instances(sid, "relapse") for fs in s.fields(sid, "relapse", key) if fs.evidence_ids}
    s.close()
    at = st_testing.AppTest.from_file(str(REPO / "src/nmosd/review/app.py"), default_timeout=60)
    argv = sys.argv
    sys.argv = ["app.py", "--extract", str(pipeline["extract"]), "--deid", str(pipeline["deid"]), "--review",
                str(review), "--dictionary", str(DICT)]
    try:
        at.run()
        at.text_input(key="login_name").input(REVIEWER)
        at.button(key="login").click().run()
        at.selectbox(key="patient").set_value(sid).run()
        at.radio(key="instrument").set_value("demographics").run()
        assert not at.exception
        assert any(e.label == "Conditions found in the notes (3)" for e in at.expander)
        assert any("structured Demographics table" in c.value for c in at.caption), "ethnicity: structured only"
        at.radio(key="instrument").set_value("relapse").run()
        buttons = {b.key: b.label for b in at.button if b.key in counts}
        assert buttons and all(label == f"Evidence ({counts[k]})" for k, label in buttons.items())
        at.button(key=next(iter(buttons))).click().run()
        at.button(key="evnext").click().run()
        assert not at.exception and not at.warning, "every synthetic quote is found in its note"
    finally:
        sys.argv = argv


# =============================================================================================
# run hashes per group
# =============================================================================================
class _Client:
    model, num_ctx = "synthetic", 8192


def test_editing_one_groups_rules_does_not_rerun_the_others(monkeypatch):
    settings = load_settings(os.environ["NMOSD_SETTINGS"])
    h = lambda g: extract_run.run_config_hash(_Client(), {"schema": g}, settings, g)  # noqa: E731
    before = {g: h(g) for g in ("treatment", "comorbidity")}
    real = extract_run.load_rules

    def edited(rel):
        r = real(rel)
        if rel == "fields/pilot_fields.yaml":
            r["groups"]["comorbidity"]["fields"]["comorbidities"]["definition"] += " (edited)"
        return r

    monkeypatch.setattr(extract_run, "load_rules", edited)
    assert h("treatment") == before["treatment"] and h("comorbidity") != before["comorbidity"]


def test_groups_unchanged_since_per_group_hashing_keep_their_old_run_hash(monkeypatch):
    settings = load_settings(os.environ["NMOSD_SETTINGS"])
    g = "relapse"
    monkeypatch.setattr(extract_run, "LEGACY_GROUP_RULES", {g: extract_run.group_rules_hash(g)})
    legacy = extract_run.run_config_hash(_Client(), {}, settings, g)
    monkeypatch.setattr(extract_run, "LEGACY_GROUP_RULES", {})
    assert extract_run.run_config_hash(_Client(), {}, settings, g) != legacy, "the old whole-file hash is kept"
