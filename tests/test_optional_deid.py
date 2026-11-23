"""De-identification is optional (13.11.26), on synthetic data only.

With `deid.enabled: false` (or `nmosd deid --keep-identifiers`) the notes go into the same database layout as they
are: original text and real dates, under study IDs. Extraction, review, export and re-identification then work
unchanged, there is nothing to leak-test or sign off, and work done on de-identified notes never counts as done for
the original ones. A patient never switches between the two ways without --switch-mode.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import sys
from datetime import date

import pytest
import yaml

from nmosd.app import jobs
from nmosd.app import workspace as ws
from nmosd.cli import main as cli_main
from nmosd.deid.linkage import load_linkage
from nmosd.deid.pipeline import is_deidentified, run_deid
from nmosd.extract.run import run_extraction
from nmosd.extract.stage2_reconcile import run_reconcile
from nmosd.ingest.load_staging import stage
from nmosd.redcap.export import EXPORT_NAME, run_export
from nmosd.redcap.reidentify import FINAL_NAME, run_reidentify
from nmosd.review.store import GATE, INSTRUMENT_FIELDS, ReviewStore
from tests.conftest import PASSPHRASE, REPO

DICT = REPO / "tests/fixtures/redcap/data_dictionary_pilot.csv"
GUIDE = REPO / "src" / "nmosd" / "app" / "guide.py"
REVIEWER = "Synthetic Reviewer"


def _con(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture(scope="module")
def kept(synthetic_dir, pipeline, tmp_path_factory):
    """The synthetic chain with the identifiers kept: stage → deid --keep-identifiers → extract → reconcile."""
    root = tmp_path_factory.mktemp("kept")
    p = {"root": root, "staging": root / "work" / "staging.sqlite", "deid": root / "deid" / "deid.sqlite",
         "linkage": root / "work" / "linkage" / "link.enc", "extract": root / "deid" / "extract.sqlite",
         "review": root / "deid" / "review.sqlite", "settings": pipeline["settings"]}
    stage(synthetic_dir / "synthetic_rpdr.sqlite", synthetic_dir / "pilot_empis.txt", p["staging"], p["settings"],
          stdout=io.StringIO())
    buf = io.StringIO()
    p["deid_rc"] = run_deid(p["staging"], p["deid"], p["linkage"], None, p["settings"], keep_identifiers=True,
                            passphrase=PASSPHRASE, stdout=buf)
    p["deid_out"] = buf.getvalue()
    run_extraction(p["deid"], p["extract"], p["settings"], llm="synthetic-oracle",
                   annotations=synthetic_dir / "annotations.json", stdout=io.StringIO())
    run_reconcile(p["extract"], p["deid"], p["settings"], stdout=io.StringIO())
    p["link"] = load_linkage(p["linkage"], PASSPHRASE)
    return p


def test_notes_are_kept_as_they_are_under_study_ids(kept):
    assert kept["deid_rc"] == 0 and "WITHOUT de-identification" in kept["deid_out"]
    assert "no confirmed identifiers" not in kept["deid_out"], "no identifiers list is needed"
    assert not is_deidentified(kept["deid"])
    staged = {r["doc_pk"]: dict(r) for r in _con(kept["staging"]).execute("SELECT * FROM documents")}
    con = _con(kept["deid"])
    docs = [dict(r) for r in con.execute("SELECT doc_id, study_id, report_date, text FROM documents")]
    sids = {r[0] for r in con.execute("SELECT study_id FROM patients")}
    con.close()
    assert docs and all(s.startswith("NMO-") for s in sids)
    for d in docs:
        src = staged[kept["link"]["documents"][d["doc_id"]]["doc_pk"]]
        assert d["text"] == src["text"], "original text"
        assert d["report_date"] == src["report_datetime"][:10], "real dates"
    for entry in kept["link"]["patients"].values():
        assert entry["offset_days"] == 0 and entry["dates_shifted"] is False


def test_extraction_reruns_for_original_notes_and_finds_the_real_dates(kept, pipeline):
    def runs(path):
        con = _con(path)
        try:
            return {r[0] for r in con.execute("SELECT run_id FROM runs")}
        finally:
            con.close()

    assert runs(kept["extract"]).isdisjoint(runs(pipeline["extract"])), "never 'done' from de-identified notes"

    def onsets(p):
        out = {}
        con = _con(p["extract"])
        for r in con.execute("SELECT study_id, value FROM proposals WHERE field='date_onset' AND value IS NOT NULL "
                             "AND run_id=(SELECT MAX(run_id) FROM proposals)"):
            out.setdefault(p["link"]["patients"][r["study_id"]]["empi"], set()).add(r["value"])
        con.close()
        return out

    from nmosd.deid.date_shift import Offsets, unshift_by_precision

    real, shifted = onsets(kept), onsets(pipeline)
    by_empi = {v["empi"]: v for v in pipeline["link"]["patients"].values()}
    assert real.keys() == shifted.keys() and real
    for empi, dates in shifted.items():
        off = Offsets(int(by_empi[empi]["offset_days"]))
        exact = {unshift_by_precision(date.fromisoformat(d), off, "1").isoformat() for d in dates}
        assert exact & real[empi], empi   # the exact-date relapses are the same events, on their real dates


def test_review_export_and_final_file_carry_the_real_dates(kept, synthetic_dir, tmp_path):
    s = ReviewStore(kept["review"], kept["extract"], kept["deid"], DICT)
    try:
        assert not s.notes_deidentified()
        for sid in s.patients():
            for fs in s.fields(sid, GATE, GATE):
                s.record(REVIEWER, "accept", sid, GATE, GATE, fs.field)
            for inst in INSTRUMENT_FIELDS:
                for key in s.instances(sid, inst):
                    for fs in s.fields(sid, inst, key):
                        s.record(REVIEWER, "accept" if fs.proposed is not None else "unresolved", sid, inst, key,
                                 fs.field)
    finally:
        s.close()
    out = tmp_path / "deid" / "export"
    buf = io.StringIO()
    assert run_export(kept["extract"], kept["review"], DICT, out, deid=kept["deid"], stdout=buf) == 0, buf.getvalue()
    final_dir = tmp_path / "work" / "final"
    assert run_reidentify(out / EXPORT_NAME, kept["linkage"], final_dir,
                          record_map=synthetic_dir / "record_map.csv", passphrase=PASSPHRASE,
                          stdout=io.StringIO()) == 0
    final = list(csv.DictReader((final_dir / FINAL_NAME).open(encoding="utf-8")))
    gold = list(csv.DictReader((synthetic_dir / "gold_redcap.csv").open(encoding="utf-8")))
    exact = [g for g in gold if g["redcap_repeat_instrument"] == "relapse" and g["date_attack_estimate"] == "1"
             and g["record_id"] != "SYN-03"]
    assert exact
    for g in exact:
        assert any(r["record_id"] == g["record_id"] and r["date_onset"] == g["date_onset"] for r in final), g


def test_original_notes_read_hospital_names(kept, pipeline):
    """18.11.26: hospital names are only in the original notes, so only there does the `hospital` group run and does
    the review ask for hx_opcare, nr_hosp and nr_hosp_2 (tests/test_hospital_names.py has the details)."""
    def groups(path):
        con = _con(path)
        try:
            return {json.loads(r[0])["group"] for r in con.execute("SELECT config_json FROM runs")}
        finally:
            con.close()

    assert "hospital" in groups(kept["extract"]) and "hospital" not in groups(pipeline["extract"])
    s = ReviewStore(kept["review"], kept["extract"], kept["deid"], DICT)
    d = ReviewStore(pipeline["review"], pipeline["extract"], pipeline["deid"], DICT)
    try:
        assert s.hospital_names and not d.hospital_names
        assert "hx_opcare" in s.instrument_fields["demographics"] and "hx_opcare" not in d.instrument_fields["demographics"]
        assert "nr_hosp_2" in s.instrument_fields["relapse"] and "nr_hosp" in s.instrument_fields["non_relapse_hospitalization"]
        sid = s.patients()[0]
        assert "hx_opcare" in {fs.field for fs in s.fields(sid, "demographics", "demographics")}
        assert "hx_opcare" not in {fs.field for fs in d.fields(d.patients()[0], "demographics", "demographics")}
    finally:
        s.close()
        d.close()


def test_leak_test_report_and_signoff_have_nothing_to_do(kept, tmp_path, capsys):
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump({"paths": {}}), encoding="utf-8")
    common = ["--settings", str(settings)]
    assert cli_main([*common, "leak-test", "--deid", str(kept["deid"]), "--linkage", str(kept["linkage"]),
                     "--no-ner"]) == 0
    assert cli_main([*common, "deid-report", "--deid", str(kept["deid"]), "--linkage", str(kept["linkage"]),
                     "--staging", str(kept["staging"]), "--out", str(tmp_path / "r.html")]) == 0
    out = capsys.readouterr().out
    assert out.count("nothing to leak-test") == 2 and not (tmp_path / "r.html").exists()


def test_a_patient_switches_only_when_asked(synthetic_dir, pipeline, tmp_path):
    settings = pipeline["settings"]
    staging, deid, linkage = tmp_path / "work" / "staging.sqlite", tmp_path / "deid" / "deid.sqlite", \
        tmp_path / "work" / "linkage" / "link.enc"
    stage(synthetic_dir / "synthetic_rpdr.sqlite", synthetic_dir / "pilot_empis.txt", staging, settings,
          stdout=io.StringIO())

    def go(keep, switch=False):
        buf = io.StringIO()
        rc = run_deid(staging, deid, linkage, synthetic_dir / "identifiers.yaml", settings, use_ner=False,
                      keep_identifiers=keep, switch_mode=switch, passphrase=PASSPHRASE, stdout=buf)
        return rc, buf.getvalue(), load_linkage(linkage, PASSPHRASE)

    rc, _, link = go(False)
    offsets = {k: v["offset_days"] for k, v in link["patients"].items()}
    assert rc == 0 and all(abs(o) >= 30 for o in offsets.values()) and is_deidentified(deid)
    rc, out, link = go(True)
    assert rc == 2 and "--switch-mode" in out and "5 of these patients" in out
    assert is_deidentified(deid), "the de-identified database is left alone"
    assert {k: v["offset_days"] for k, v in link["patients"].items()} == offsets, "and so is the key"
    rc, out, link = go(True, switch=True)
    assert rc == 0 and not is_deidentified(deid) and "5 patients were de-identified before" in out
    assert set(link["patients"]) == set(offsets), "same study IDs"
    assert all(v["offset_days"] == 0 for v in link["patients"].values())
    rc, out, _ = go(False)
    assert rc == 2 and "kept as they are last time" in out
    rc, _, link = go(False, switch=True)
    assert rc == 0 and all(abs(v["offset_days"]) >= 30 for v in link["patients"].values()), "new date shifts"


# ============================================================================================== the guided app
@pytest.mark.streamlit
def test_guided_app_without_de_identification(synthetic_dir, tmp_path, monkeypatch):
    st_testing = pytest.importorskip("streamlit.testing.v1")
    settings_path, data_dir = tmp_path / "settings.yaml", tmp_path / "private"
    monkeypatch.setattr(sys, "argv", ["guide.py", "--settings", str(settings_path)])
    src = synthetic_dir / "synthetic_rpdr.sqlite"
    raw = ws.build_settings({}, src, data_dir, None, "YYYY-MM-DD")
    manifest = json.loads((synthetic_dir / "manifest.json").read_text(encoding="utf-8"))
    raw |= {"source": {"export_date": manifest["export_date"]},
            "llm": {"backend": "synthetic-oracle", "synthetic_annotations": str(synthetic_dir / "annotations.json")}}
    ws.save(settings_path, raw)
    ws.write_empis(data_dir / "work" / "patients.txt",
                   (synthetic_dir / "pilot_empis.txt").read_text(encoding="utf-8").split())

    def run(at):
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        return at

    def goto(at, step):
        at.radio(key="g_step").set_value(step)
        return run(at)

    def texts(at):
        return " ".join(x.value for kind in ("success", "warning", "error", "info", "markdown", "caption")
                        for x in getattr(at, kind))

    def finish(name):
        j = jobs.wait(data_dir / "work" / "app_jobs", name, timeout=300)
        assert j.state == "done", jobs.tail(j)
        return j

    at = st_testing.AppTest.from_file(str(GUIDE), default_timeout=120)
    at.session_state["condition"] = "nmosd"         # chosen on the start screen
    at = goto(run(at), "patients")
    at.button(key="g_stage").click()
    run(at)
    finish("stage")
    # 4 · choose not to de-identify: saved in the settings, steps 4 and 6 not needed
    at = goto(run(at), "identifiers")
    at.radio(key="g_deid_choice_4").set_value(next(v for v in at.radio(key="g_deid_choice_4").options
                                                   if v.startswith("No")))
    at = run(at)
    assert yaml.safe_load(settings_path.read_text(encoding="utf-8"))["deid"]["enabled"] is False
    assert "Not needed" in texts(at)
    # 5 · prepare the notes as they are
    at = goto(at, "deid")
    assert "as they are" in texts(at)
    at.text_input(key="g_pass1").input("a long synthetic passphrase")
    at.text_input(key="g_pass2").input("a long synthetic passphrase")
    at.button(key="g_prepare").click()
    run(at)
    assert finish("deid").rcs == [0]
    at = run(at)
    assert "Notes prepared." in texts(at) and not is_deidentified(data_dir / "deid" / "deid.sqlite")
    # 6 · nothing to sign off; 7 · extraction runs; 9 · export without the sign-off gate
    at = goto(at, "signoff")
    assert "nothing to check" in texts(at)
    at = goto(at, "extract")
    assert "de-identified" not in texts(at).split("Test mode")[0]
    at.button(key="g_extract").click()
    run(at)
    assert finish("extract").rcs == [0, 0, 0, 0]
    at = goto(run(at), "export")
    assert "g_export_test" not in [c.key for c in at.checkbox], "no sign-off needed"
    at.button(key="g_export").click()
    run(at)
    finish("export")
    at = run(at)
    assert "File with study IDs" in texts(at)
