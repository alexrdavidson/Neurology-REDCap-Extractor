"""Phases 7–9 on synthetic data.

Phase 7 accept: full review of one synthetic patient end to end; state persists across restarts.
Phase 8 accept: export passes validation; un-shifting restores the original dates exactly; re-running export
                produces identical instance numbers.
Phase 9 accept: report generated on synthetic data; output is aggregate-only.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from datetime import date

import pytest

from nmosd.deid.date_shift import Offsets, shift_by_precision, unshift_by_precision
from nmosd.eval.compare_gold import deidentify_gold, run_evaluate
from nmosd.redcap.export import EXPORT_NAME, REPEATING, _eval_branching, run_export
from nmosd.redcap.reidentify import DATE_ESTIMATE, FINAL_NAME, run_reidentify
from nmosd.review.launch import streamlit_command
from nmosd.review.store import GATE, INSTRUMENT_FIELDS, ReviewStore
from tests.conftest import PASSPHRASE, REPO

DICT = REPO / "tests/fixtures/redcap/data_dictionary_pilot.csv"
REVIEWER = "Synthetic Reviewer"
MOGAD_EMPI = "100002286"   # synthetic P3: MOG antibody disease, excluded at the diagnosis check


def _store(p, review=None):
    return ReviewStore(review or p["review"], p["extract"], p["deid"], DICT)


def accept_gate(s: ReviewStore, sid: str) -> None:
    """Step 1 of every review: accept the diagnosis-check proposals."""
    for fs in s.fields(sid, GATE, GATE):
        s.record(REVIEWER, "accept", sid, GATE, GATE, fs.field)


def nmosd_patients(s: ReviewStore) -> list[str]:
    return [sid for sid in s.patients() if s.fields(sid, GATE, GATE)[0].proposed == "nmosd"]


@pytest.fixture(scope="module")
def reviewed(pipeline):
    """Review every proposal of every patient (accept), with a few corrections on patient 1."""
    s = _store(pipeline)
    first = s.patients()[0]
    for sid in s.patients():
        accept_gate(s, sid)
        for inst in INSTRUMENT_FIELDS:
            for key in s.instances(sid, inst):
                for fs in s.fields(sid, inst, key):
                    if fs.proposed is None:
                        s.record(REVIEWER, "unresolved", sid, inst, key, fs.field, reason="not documented")
                    else:
                        s.record(REVIEWER, "accept", sid, inst, key, fs.field)
    s.close()
    return {"first": first}


# =============================================================================================
# Phase 7
# =============================================================================================
def test_full_review_of_one_patient_and_persistence(pipeline, tmp_path):
    review = tmp_path / "deid" / "review.sqlite"
    s = _store(pipeline, review)
    sid = nmosd_patients(s)[0]
    s.start_session(REVIEWER, sid)
    assert s.gate(sid).status == "pending"
    accept_gate(s, sid)
    assert s.gate(sid).included
    before = [(r["proposal_id"], r["value"]) for r in s.extract.execute("SELECT proposal_id, value FROM proposals")]
    keys = s.instances(sid, "relapse")
    target = keys[0]
    own_doc = s.search(sid, "")[0]["doc_id"]
    s.record(REVIEWER, "correct", sid, "relapse", target, "date_attack_estimate", new_value="2",
             reason="month is exact", evidence_doc_id=own_doc, evidence_start=0, evidence_end=10)
    new_key = s.add_instance(REVIEWER, sid, "relapse", reason="missed relapse")
    s.record(REVIEWER, "correct", sid, "relapse", new_key, "date_onset", new_value="2015-02-01", estimate_code="2")
    s.record(REVIEWER, "delete_instance", sid, "relapse", keys[-1], reason="duplicate episode")
    s.record(REVIEWER, "comment", sid, "demographics", "demographics", "sex", reason="checked against note")
    for inst in INSTRUMENT_FIELDS:
        for key in s.instances(sid, inst):
            for fs in s.fields(sid, inst, key):
                if fs.status == "pending":
                    s.record(REVIEWER, "accept" if fs.value is not None else "unresolved", sid, inst, key, fs.field)
    done, total = s.progress(sid)
    assert done == total > 20
    s.close()
    # "restart": a new store over the same files sees the same state
    s2 = _store(pipeline, review)
    assert s2.progress(sid) == (done, total)
    assert keys[-1] not in s2.instances(sid, "relapse") and new_key in s2.instances(sid, "relapse")
    corrected = next(f for f in s2.fields(sid, "relapse", target) if f.field == "date_attack_estimate")
    assert corrected.status == "corrected" and corrected.value == "2"
    log = s2.history(sid)
    assert all(r["reviewer"] == REVIEWER and r["at"] for r in log)
    assert any(r["old_value"] is not None and r["action"] == "correct" for r in log)
    with pytest.raises(sqlite3.DatabaseError):
        s2.review.execute("UPDATE reviews SET new_value='x'")
    with pytest.raises(sqlite3.DatabaseError):
        s2.review.execute("DELETE FROM reviews")
    after = [(r["proposal_id"], r["value"]) for r in s2.extract.execute("SELECT proposal_id, value FROM proposals")]
    assert before == after, "original proposals never overwritten"
    s2.close()


def test_evidence_and_search(pipeline):
    s = _store(pipeline)
    sid = s.patients()[0]
    key = s.instances(sid, "treatment")[0]
    fs = next(f for f in s.fields(sid, "treatment", key) if f.field == "date_starting_maint")
    ev = s.evidence(fs.evidence_ids, sid)
    assert ev and all(e["verified"] for e in ev), (fs.field, fs.proposed, len(fs.evidence_ids), s.run_id, s.instances(sid, "treatment"))
    doc = s.document(ev[0]["doc_id"], sid)
    assert doc["text"][ev[0]["char_start"]:ev[0]["char_end"]].lower().startswith(ev[0]["quote"][:10].lower())
    # Study IDs are random, so "first patient" varies per run; not every synthetic patient has rituximab.
    hits = [h for p in s.patients() for h in s.search(p, "rituximab", doc_types=["VisitNote"])]
    assert hits and all(h["source_table"] == "VisitNote" for h in hits)
    dates = [h["report_date"] for h in s.search(sid, "")]
    assert dates == sorted(dates), "chronological browsing"
    assert s.labels("maintenance_therapy")["1"] == "Rituximab"
    s.close()


def test_evidence_never_crosses_patients(pipeline, tmp_path):
    s = _store(pipeline, tmp_path / "deid" / "review.sqlite")
    owner = {r["doc_id"]: r["study_id"] for r in s.deid.execute("SELECT doc_id, study_id FROM documents")}
    # stored data: every proposal's evidence comes from its own patient's notes
    for r in s.extract.execute("SELECT study_id, evidence_ids FROM proposals WHERE run_id=?", (s.run_id,)):
        for e in s.evidence(json.loads(r["evidence_ids"] or "[]"), r["study_id"]):
            assert owner[e["doc_id"]] == r["study_id"]
    for r in s.extract.execute("SELECT doc_id, first_doc_id FROM paragraph_repeats"):
        assert owner[r["doc_id"]] == owner[r["first_doc_id"]], "copy-forward links stay within a patient"
    # store API: asking with the wrong patient returns nothing and attaching another patient's note is refused
    a, b = s.patients()[:2]
    b_ev = [e for r in s.extract.execute("SELECT evidence_ids FROM proposals WHERE run_id=? AND study_id=?",
                                         (s.run_id, b)) for e in json.loads(r["evidence_ids"] or "[]")]
    assert b_ev and s.evidence(b_ev, b) and s.evidence(b_ev, a) == []
    b_doc = s.search(b, "")[0]["doc_id"]
    assert s.document(b_doc, b) and s.document(b_doc, a) is None
    assert s.repeats_of(b_doc, 0, 10, a) == []
    with pytest.raises(ValueError, match="this patient"):
        s.record(REVIEWER, "correct", a, "demographics", "demographics", "race", new_value="5",
                 evidence_doc_id=b_doc, evidence_start=0, evidence_end=10)
    s.close()


def test_structured_demographics_come_from_the_table(pipeline):
    """16.11.26: sex, race, ethnicity and living/deceased from the Demographics table only, never the notes."""
    s = _store(pipeline)
    for sid in s.patients():
        fields = {f.field: f for f in s.fields(sid, "demographics", "demographics")}
        for f in ("sex", "race", "ethnicity", "living_deceased"):
            fs = fields[f]
            assert fs.proposed and fs.evidence_ids == [] and not fs.conflict, (sid, f, fs.conflict)
        if fields["date_nmo_diagnosis"].proposed:
            dx = s.evidence(fields["date_nmo_diagnosis"].evidence_ids, sid)
            assert dx and all(e["field_group"] == "diagnosis" for e in dx)
    s.close()


def test_streamlit_launch_is_localhost_only(tmp_path):
    cmd = streamlit_command(tmp_path / "e", tmp_path / "d", tmp_path / "r", DICT)
    assert cmd[cmd.index("--server.address") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--server.headless") + 1] == "true"
    assert cmd[cmd.index("--browser.gatherUsageStats") + 1] == "false"
    cfg = (REPO / ".streamlit/config.toml").read_text()
    assert 'address = "127.0.0.1"' in cfg and "gatherUsageStats = false" in cfg



def test_launch_refuses_when_port_taken(monkeypatch, tmp_path, capsys):
    import nmosd.review.launch as launch_mod
    from nmosd.config import load_settings

    monkeypatch.setattr(launch_mod, "port_in_use", lambda port: True)
    monkeypatch.setattr(launch_mod.subprocess, "call", lambda *a, **k: pytest.fail("must not start a second server"))
    monkeypatch.setattr(launch_mod, "ensure_phi_output", lambda p, what: p)
    settings = load_settings(REPO / "config" / "settings.example.yaml")
    assert launch_mod.launch(tmp_path / "e", tmp_path / "d", tmp_path / "r", settings) == 1
    assert "already in use" in capsys.readouterr().err


def test_code_change_detection(monkeypatch):
    import nmosd.review as review_pkg

    assert review_pkg.code_changed_since_start() is False
    monkeypatch.setattr(review_pkg, "LOADED_AT", 0.0)
    assert review_pkg.code_changed_since_start() is True

@pytest.mark.streamlit
def test_app_smoke(pipeline, tmp_path):
    st_testing = pytest.importorskip("streamlit.testing.v1")
    review = tmp_path / "deid" / "review.sqlite"
    app = REPO / "src/nmosd/review/app.py"
    at = st_testing.AppTest.from_file(str(app), default_timeout=60)
    import sys

    argv = sys.argv
    sys.argv = ["app.py", "--extract", str(pipeline["extract"]), "--deid", str(pipeline["deid"]), "--review", str(review),
                "--dictionary", str(DICT)]
    s = _store(pipeline, review)
    sid = nmosd_patients(s)[0]
    s.close()
    try:
        at.run()
        at.text_input(key="login_name").input(REVIEWER)
        at.button(key="login").click().run()
        assert not at.exception
        assert at.selectbox(key="patient").options
        at.selectbox(key="patient").set_value(sid).run()
        # step 1: only the diagnosis check is available until it confirms NMOSD
        assert at.radio(key="instrument").options == ["Diagnosis check"]
        assert any("Does this patient have NMOSD" in h.value for h in at.subheader)
        for f in ("dx_category", "followup_12m"):
            at.button(key=f"{sid}:{GATE}:{f}:acc").click().run()
            assert not at.exception
        assert any("Included" in x.value for x in at.success)
        at.radio(key="instrument").set_value("treatment").run()
        acc = [b for b in at.button if b.key and b.key.endswith(":acc")]
        assert acc
        acc[0].click().run()
        assert not at.exception
    finally:
        sys.argv = argv
    con = sqlite3.connect(review)
    assert con.execute("SELECT COUNT(*) FROM reviews WHERE action='accept' AND instrument=?", (GATE,)).fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM reviews WHERE action='accept' AND instrument='treatment'").fetchone()[0] == 1
    con.close()


# =============================================================================================
# Phase 8
# =============================================================================================
def _export(pipeline, out_dir, review=None):
    buf = io.StringIO()
    rc = run_export(pipeline["extract"], review or pipeline["review"], DICT, out_dir, deid=pipeline["deid"], stdout=buf)
    rows = list(csv.DictReader((out_dir / EXPORT_NAME).open(encoding="utf-8")))
    return rc, rows, buf.getvalue()


@pytest.mark.streamlit
def test_app_switching_patient_clears_evidence_and_attachment(pipeline, tmp_path):
    """Regression: evidence opened (or a passage attached) for one patient stayed on screen for the next."""
    st_testing = pytest.importorskip("streamlit.testing.v1")
    review = tmp_path / "deid" / "review.sqlite"
    s = _store(pipeline, review)
    a, b = nmosd_patients(s)[:2]
    for sid in (a, b):
        accept_gate(s, sid)
    s.close()
    at = st_testing.AppTest.from_file(str(REPO / "src/nmosd/review/app.py"), default_timeout=60)
    import sys

    argv = sys.argv
    sys.argv = ["app.py", "--extract", str(pipeline["extract"]), "--deid", str(pipeline["deid"]), "--review", str(review),
                "--dictionary", str(DICT)]
    try:
        at.run()
        at.text_input(key="login_name").input(REVIEWER)
        at.button(key="login").click().run()
        at.selectbox(key="patient").set_value(a).run()
        at.radio(key="instrument").set_value("treatment").run()
        ev_buttons = [x for x in at.button if x.key and x.key.endswith(":ev")]
        assert ev_buttons and all(x.key.startswith(f"{a}:") for x in ev_buttons), "widget keys carry the patient"
        ev_buttons[0].click().run()
        at.button(key="attach_btn").click().run()
        assert at.session_state["evidence_ids"] and at.session_state["attach"][0] == a
        shown_a = [c.value for c in at.caption if "DOC-" in c.value]
        assert shown_a
        at.selectbox(key="patient").set_value(b).run()
        assert not at.exception
        assert "evidence_ids" not in at.session_state and "attach" not in at.session_state
        assert not [c.value for c in at.caption if c.value in shown_a]
        assert any("Click an Evidence button" in c.value for c in at.caption)
        assert all(x.key.startswith(f"{b}:") for x in at.button if x.key and x.key.endswith(":ev"))
    finally:
        sys.argv = argv


def test_export_validates_and_is_redcap_shaped(pipeline, reviewed, tmp_path):
    rc, rows, out = _export(pipeline, tmp_path / "deid" / "export")
    assert rc == 0 and "Validation errors: 0" in out, out
    # the diagnosis check excluded the MOGAD patient: never exported, counted only
    mogad = next(k for k, v in pipeline["link"]["patients"].items() if v["empi"] == MOGAD_EMPI)
    assert mogad not in {r["record_id"] for r in rows} and len({r["record_id"] for r in rows}) == 4
    assert "patients not exported: excluded at the diagnosis check: 1" in out and "patients exported: 4" in out
    # completeness warnings, counts only
    assert "not exported: marked unresolved (warning)" in out and "never reviewed" not in out
    # the Demographics-table fields (16.11.26) are not expected to have note evidence
    for f in ("sex", "race", "ethnicity", "living_deceased", "date_nmo_diagnosis", "comorbidities"):
        assert f"no note evidence (warning): {f}:" not in out, out
    cols = list(rows[0])
    assert cols[:3] == ["record_id", "redcap_repeat_instrument", "redcap_repeat_instance"]
    assert "relapse_phenotype___1" in cols and "acute_rx___6" in cols
    for banned in ["los", "bmi", "treatment_number", "duration_of_therapy", "mrn", "dob", "mrn_id3"]:
        assert banned not in cols
    assert all(r[c] != "2" for r in rows for c in cols if c.endswith("_complete"))
    assert {r["redcap_repeat_instrument"] for r in rows} >= {"", "relapse", "treatment", "antibody_lab_data"}
    assert {r["redcap_repeat_instrument"] for r in rows} <= {"", *REPEATING}
    for r in rows:
        for c, v in r.items():
            if c.startswith("date_") and not c.endswith("estimate") and v:
                assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)
    assert all(r["record_id"].startswith("NMO-") for r in rows)


def test_export_instance_numbers_are_stable(pipeline, reviewed, tmp_path):
    import shutil

    review = tmp_path / "deid" / "review_copy.sqlite"
    review.parent.mkdir(parents=True)
    src = sqlite3.connect(pipeline["review"])
    src.execute("PRAGMA wal_checkpoint(FULL)")
    src.close()
    shutil.copy(pipeline["review"], review)
    _, rows1, _ = _export(pipeline, tmp_path / "deid" / "e1", review)
    s = _store(pipeline, review)
    sid = nmosd_patients(s)[1]   # an exported patient (study IDs are random, so not a fixed index)
    k = s.add_instance(REVIEWER, sid, "relapse", reason="late addition")
    s.record(REVIEWER, "correct", sid, "relapse", k, "date_onset", new_value="2010-01-15")
    s.close()
    _, rows2, out2 = _export(pipeline, tmp_path / "deid" / "e2", review)
    assert "exported with no note evidence (warning): date_onset: 1" in out2, "added with no passage attached"
    sig = lambda rows: {(r["record_id"], r["redcap_repeat_instrument"], r["redcap_repeat_instance"], r.get("date_onset", ""),  # noqa: E731
                         r.get("maintenance_therapy", "")) for r in rows}
    assert sig(rows1) <= sig(rows2), "existing instance numbers unchanged"
    new = sig(rows2) - sig(rows1)
    n_before = max(int(r["redcap_repeat_instance"]) for r in rows1 if r["record_id"] == sid and r["redcap_repeat_instrument"] == "relapse")
    assert any(x[2] == str(n_before + 1) for x in new)


def test_branching_evaluator():
    assert _eval_branching("[admission] = '1'", {"admission": "1"}) is True
    assert _eval_branching("[admission] = '1'", {"admission": "0"}) is False
    assert _eval_branching("[acute_rx(1)] = '1' or [acute_rx(2)] = '1'", {"acute_rx___2": "1"}) is True
    assert _eval_branching("[type_test] = 'aqp4' and [aqp4_test_location] = '5'", {"type_test": "aqp4"}) is False


@pytest.mark.parametrize("days", [30, -30, 200, -200, 365, -365, 183, -183])
@pytest.mark.parametrize("est", ["1", "2", "3", "4"])
def test_unshift_restores_original_dates_exactly(days, est):
    off = Offsets(days)
    for d in [date(2016, 7, 15), date(2018, 3, 4), date(2020, 2, 29), date(1998, 7, 1), date(2021, 12, 31)]:
        if est in ("2", "3"):
            d = d.replace(day=min(d.day, 28))
        assert unshift_by_precision(shift_by_precision(d, off, est), off, est) == d


def test_reidentify_round_trip(pipeline, reviewed, tmp_path):
    _export(pipeline, tmp_path / "deid" / "export")
    out_dir = tmp_path / "work" / "final"
    buf = io.StringIO()
    rc = run_reidentify(tmp_path / "deid" / "export" / EXPORT_NAME, pipeline["linkage"], out_dir,
                        record_map=pipeline["synthetic"] / "record_map.csv", passphrase=PASSPHRASE,
                        add_identifiers=True, stdout=buf)
    assert rc == 0
    final = list(csv.DictReader((out_dir / FINAL_NAME).open(encoding="utf-8")))
    assert {r["record_id"] for r in final} == {"SYN-01", "SYN-02", "SYN-04", "SYN-05"}, "SYN-03 (MOGAD) excluded"
    assert "mrn" in final[0] and any(r["dob"] == "1988-03-04" for r in final)
    gold = list(csv.DictReader((pipeline["synthetic"] / "gold_redcap.csv").open(encoding="utf-8")))
    exact = 0
    for g in gold:
        if g["redcap_repeat_instrument"] != "relapse" or g["record_id"] == "SYN-03":
            continue
        cand = [r for r in final if r["record_id"] == g["record_id"] and r["redcap_repeat_instrument"] == "relapse"]
        est = g["date_attack_estimate"]
        match = [r for r in cand if r["date_onset"] == g["date_onset"]]
        if est == "1":
            assert match, (g["record_id"], g["date_onset"])
            exact += 1
        elif est == "3":
            assert any(r["date_onset"][:4] == g["date_onset"][:4] for r in cand)
    assert exact >= 8
    out = buf.getvalue()
    assert "SYN-" not in out and "NMO-" not in out
    # gold dates de-identified with the linkage key, then re-identified, are restored exactly
    rmap = {r["record_id"]: r["empi"] for r in csv.DictReader((pipeline["synthetic"] / "record_map.csv").open())}
    shifted = deidentify_gold(gold, pipeline["link"], rmap)
    for g, s in zip(gold, shifted, strict=True):
        off = Offsets(int(pipeline["link"]["patients"][s["record_id"]]["offset_days"]))
        for f, ef in DATE_ESTIMATE.items():
            if g.get(f):
                assert unshift_by_precision(date.fromisoformat(s[f]), off, g.get(ef) if ef else "1").isoformat() == g[f]


def test_reidentify_refuses_unmapped_records(pipeline, reviewed, tmp_path):
    _export(pipeline, tmp_path / "deid" / "export")
    partial = tmp_path / "work" / "partial_map.csv"
    partial.parent.mkdir(parents=True)
    partial.write_text("empi,record_id\n100004417,SYN-01\n")
    rc = run_reidentify(tmp_path / "deid" / "export" / EXPORT_NAME, pipeline["linkage"], tmp_path / "work" / "out",
                        record_map=partial, passphrase=PASSPHRASE, stdout=io.StringIO())
    assert rc == 2 and not (tmp_path / "work" / "out" / FINAL_NAME).exists()


# =============================================================================================
# Phase 9
# =============================================================================================
def test_evaluation_report_is_aggregate_only(pipeline, reviewed, tmp_path):
    _export(pipeline, tmp_path / "deid" / "export")
    rmap = tmp_path / "work" / "record_map.csv"
    rmap.parent.mkdir(parents=True)
    rmap.write_text((pipeline["synthetic"] / "record_map.csv").read_text())
    buf = io.StringIO()
    report = tmp_path / "work" / "eval.md"
    run_evaluate(pipeline["synthetic"] / "gold_redcap.csv", tmp_path / "deid" / "export" / EXPORT_NAME,
                 pipeline["review"], pipeline["linkage"], rmap, pipeline["settings"], out=report,
                 extract=pipeline["extract"], passphrase=PASSPHRASE, label="synthetic-oracle", stdout=buf)
    out = buf.getvalue()
    assert "Episodes" in out and "| relapse |" in out and "Review time per patient" in out
    assert not re.search(r"NMO-|SYN-|DOC-|\b\d{4}-\d{2}-\d{2}\b|1000\d{5}", out), "aggregate only"
    m = json.loads(report.read_text().split("```json\n", 1)[1].rsplit("```", 1)[0])
    # the MOGAD patient is in the gold file but excluded at the diagnosis check: counted, not scored as misses
    assert m["patients"] == {"gold": 5, "exported": 4, "compared": 4, "gold_only": 1, "export_only": 0}
    assert "In the gold file but not exported" in out
    assert m["episodes"]["relapse"]["recall"] == 1.0 and m["episodes"]["relapse"]["precision"] == 1.0
    assert m["episodes"]["treatment"]["recall"] == 1.0 and m["episodes"]["treatment"]["precision"] == 1.0
    assert m["episodes"]["antibody_lab_data"]["recall"] == 1.0
    assert m["fields"]["sex"]["accuracy"] == 1.0
    assert m["checkbox"]["relapse_phenotype"]["recall"] == 1.0
