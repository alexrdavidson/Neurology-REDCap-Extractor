"""The guided app (`nmosd app`): background jobs, the settings layout, and a full walk through the screens on
synthetic data (no NER, the synthetic stand-in instead of a model). Nothing here starts a server."""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import time
import zipfile
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from nmosd.app import jobs
from nmosd.app import workspace as ws
from nmosd.config import REPO_ROOT
from nmosd.eval.accuracy import SHAREABLE, shareable_zip, snapshots

GUIDE = REPO_ROOT / "src" / "nmosd" / "app" / "guide.py"
PASS = "a long synthetic passphrase"


# ============================================================================================== jobs
def test_a_job_runs_its_steps_and_records_the_outcome(tmp_path):
    j = jobs.start(tmp_path, "t1", "Test", [jobs.nmosd("Help", "--help"), jobs.Step("Two", ["-c", "print('second')"])],
                   None)
    j = jobs.wait(tmp_path, "t1", timeout=120)
    assert j.state == "done" and j.rc == 0 and j.rcs == [0, 0]
    text = jobs.tail(j)
    assert "usage: nmosd" in jobs.section(text, "Help") and jobs.section(text, "Two") == "second"


def test_a_failing_step_stops_the_job_unless_allowed(tmp_path):
    fail = jobs.Step("Fail", ["-c", "import sys; sys.exit(3)"])
    after = jobs.Step("After", ["-c", "print('ran')"])
    j = jobs.wait(tmp_path, jobs.start(tmp_path, "a", "A", [fail, after], None).name, timeout=120)
    assert j.state == "failed" and j.rcs == [3] and "ran" not in jobs.tail(j)
    fail.allow_fail = True
    j = jobs.wait(tmp_path, jobs.start(tmp_path, "b", "B", [fail, after], None).name, timeout=120)
    assert j.state == "failed" and j.rcs == [3, 0] and j.step_rc("After") == 0 and "ran" in jobs.tail(j)


def test_one_job_at_a_time_and_stop(tmp_path):
    sleeper = jobs.Step("Sleep", ["-c", "import time; print('sleeping', flush=True); time.sleep(120)"])
    jobs.start(tmp_path, "long", "Long task", [sleeper], None)
    deadline = time.monotonic() + 60
    while "sleeping" not in jobs.tail(jobs.read(tmp_path, "long")) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert jobs.read(tmp_path, "long").state == "running"
    with pytest.raises(jobs.JobError, match="Long task"):
        jobs.start(tmp_path, "other", "Other", [jobs.Step("x", ["-c", "pass"])], None)
    jobs.stop(tmp_path, "long")
    assert jobs.read(tmp_path, "long").state == "stopped"
    assert jobs.running([tmp_path]) is None


def test_the_passphrase_reaches_the_job_but_is_never_written_down(tmp_path):
    step = jobs.Step("Env", ["-c", "import os; print(len(os.environ['NMOSD_TEST_SECRET']))"])
    j = jobs.wait(tmp_path, jobs.start(tmp_path, "env", "Env", [step], None,
                                       env_extra={"NMOSD_TEST_SECRET": PASS}).name, timeout=120)
    assert jobs.section(jobs.tail(j), "Env") == str(len(PASS))
    for f in tmp_path.iterdir():
        assert PASS not in f.read_text(encoding="utf-8")


def test_progress_lines_become_a_progress_bar():
    p = jobs.progress("Progress: 10/440 documents · ETA 01:02:03\nProgress: 20/440 documents · ETA 01:00:00\n")
    assert (p.done, p.total, p.eta_s) == (20, 440, 3600) and p.eta_text == "about 1 h 00 min left"
    p = jobs.progress("1 (document, group) pairs already done; 9 to run.\n[3/9] DOC-ABC treatment: 1 chunk(s) · ETA 00:04:30\n")
    assert (p.done, p.total) == (3, 9) and p.eta_text == "about 4 min left"
    assert jobs.progress("Downloading qwen3:8b: 45%\n").fraction == 0.45
    assert jobs.progress("nothing here") is None


# ============================================================================================== workspace
def test_layout_puts_everything_under_the_private_folder(tmp_path):
    lay = ws.layout(tmp_path / "private")
    for key, value in lay.items():
        assert Path(value).is_relative_to(tmp_path / "private"), key
    assert Path(lay["linkage"]).parent != Path(lay["deid_db"]).parent


def test_saving_keeps_hand_made_settings_and_existing_paths(tmp_path):
    data_dir = tmp_path / "private"
    raw = {"paths": {"empi_list": str(data_dir / "work" / "pilot_empis.txt")}, "deid": {"tokenise_hospitals": False}}
    src = tmp_path / "SB1_20261104_1.accdb"
    src.write_bytes(b"")
    out = ws.build_settings(raw, src, data_dir, None, "M/D/YYYY")
    assert out["deid"] == {"tokenise_hospitals": False}
    assert out["paths"]["empi_list"].endswith("pilot_empis.txt")       # kept: already inside the data folder
    assert out["paths"]["source"] == str(src) and out["export"]["date_format"] == "M/D/YYYY"
    ws.save(tmp_path / "s.yaml", out)
    s = ws.load(tmp_path / "s.yaml")
    assert ws.is_configured(s) and ws.data_folder(s) == data_dir


def test_problems_refuse_patient_data_inside_the_app_folder(tmp_path):
    src = tmp_path / "x.accdb"
    src.write_bytes(b"")
    assert ws.problems(src, tmp_path / "ok") == []
    assert any("outside the app folder" in p for p in ws.problems(src, REPO_ROOT / "data"))
    assert any("not found" in p for p in ws.problems(tmp_path / "missing.accdb", tmp_path / "ok"))
    assert ws.synced_folder_warning(Path("C:/Users/x/OneDrive - MGB/nmosd")) is not None


def test_patient_list_parsing():
    lst = ws.parse_empis("100000001\n 100000002 \n100000001\nnot an empi\n\n# comment\n100000003,100000004")
    assert lst.empis == ["100000001", "100000002", "100000003", "100000004"] and lst.rejected == 1


def test_unconfigured_app_has_no_source():
    s = ws.load(Path("/nonexistent/settings.yaml"))
    assert s.source is None and not ws.is_configured(s)


# ============================================================================================== the screens
@pytest.mark.streamlit
def test_walk_through_every_step_on_synthetic_data(synthetic_dir, tmp_path, monkeypatch):
    st_testing = pytest.importorskip("streamlit.testing.v1")
    settings_path = tmp_path / "settings.yaml"
    data_dir = tmp_path / "private"
    monkeypatch.setattr(sys, "argv", ["guide.py", "--settings", str(settings_path)])

    def app():
        at = st_testing.AppTest.from_file(str(GUIDE), default_timeout=120)
        at.session_state["condition"] = "nmosd"     # chosen on the start screen (tests/test_conditions_window.py)
        return at

    def run(at):
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        return at

    def goto(at, step):
        at.radio(key="g_step").set_value(step)
        return run(at)

    def finish(name):
        j = jobs.wait(data_dir / "work" / "app_jobs", name, timeout=300)
        assert j.state == "done", jobs.tail(j)
        return j

    def texts(at):
        return " ".join(x.value for kind in ("success", "warning", "error", "info", "markdown", "caption")
                        for x in getattr(at, kind))

    # 2 · files
    at = goto(run(app()), "files")
    at.text_input(key="g_src").input(str(synthetic_dir / "synthetic_rpdr.sqlite"))
    at.text_input(key="g_dir").input(str(data_dir))
    at.button(key="g_save_files").click()
    at = run(at)
    assert "Saved." in texts(at) and settings_path.exists()
    raw = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    manifest = json.loads((synthetic_dir / "manifest.json").read_text(encoding="utf-8"))
    raw["source"] = {"export_date": manifest["export_date"]}
    raw["deid"] = {"ner": {"enabled": False}}
    raw["llm"] = {"backend": "synthetic-oracle", "synthetic_annotations": str(synthetic_dir / "annotations.json")}
    raw["paths"]["redcap_template"] = str(synthetic_dir / "gold_redcap.csv")
    ws.save(settings_path, raw)

    # 3 · patients
    at = goto(run(app()), "patients")
    empis = (synthetic_dir / "pilot_empis.txt").read_text(encoding="utf-8")
    at.text_area(key="g_empis").input(empis)
    at.button(key="g_stage").click()
    run(at)
    finish("stage")
    at = run(at)
    assert "Notes copied." in texts(at)

    # 4 · identifiers: find, then confirm (with the synthetic confirmed list)
    at = goto(at, "identifiers")
    at.button(key="g_suggest").click()
    run(at)
    finish("identifiers")
    at = run(at)
    assert "patients:" in at.text_area(key="g_ident_text").value
    at.text_area(key="g_ident_text").input("patients: [")                 # broken YAML is refused
    at.button(key="g_ident_save").click()
    at = run(at)
    assert "can't be read" in texts(at) and not (data_dir / "work" / "identifiers.yaml").exists()
    at.text_area(key="g_ident_text").input((synthetic_dir / "identifiers.yaml").read_text(encoding="utf-8"))
    at.button(key="g_ident_save").click()
    at = run(at)
    assert "Saved and confirmed" in texts(at)

    # 5 · de-identify: the passphrase must be long enough and typed twice the same
    at = goto(at, "deid")
    at.text_input(key="g_pass1").input("short")
    at.text_input(key="g_pass2").input("short")
    at.button(key="g_deid").click()
    at = run(at)
    assert "at least 12 characters" in texts(at)
    at.text_input(key="g_pass1").input(PASS)
    at.text_input(key="g_pass2").input(PASS)
    at.button(key="g_deid").click()
    run(at)
    j = finish("deid")
    assert j.rcs == [0, 0, 0]
    at = run(at)
    assert "Leak test passed" in texts(at)
    assert (data_dir / "work" / "reports" / "deid_report.html").exists()
    for f in (data_dir / "work" / "app_jobs").iterdir():
        assert PASS not in f.read_text(encoding="utf-8", errors="replace")

    # 6 · a missing report (step 5 run outside the app) can be made here with the passphrase
    report = data_dir / "work" / "reports" / "deid_report.html"
    report.unlink()
    at = goto(at, "signoff")
    at.text_input(key="g_pass_report").input(PASS)
    at.button(key="g_make_report").click()
    run(at)
    finish("deid_report")
    at = run(at)
    assert report.exists() and "Open the report" in [b.label for b in at.button]

    # 6 · sign-off (the upload itself is not testable here: import a complete sign-off directly)
    from nmosd.deid.review_report import import_signoff

    con = sqlite3.connect(data_dir / "deid" / "deid.sqlite")
    docs = [r[0] for r in con.execute("SELECT doc_id FROM documents")]
    con.close()
    signoff = data_dir / "work" / "reports" / "deid_signoff.json"
    signoff.write_text(json.dumps({"reviewer": "Test", "documents": {d: {"reviewed": True} for d in docs}}),
                       encoding="utf-8")
    import_signoff(data_dir / "deid" / "deid.sqlite", signoff)
    at = goto(at, "signoff")
    assert "notes reviewed, 0 with misses" in texts(at)

    # 7 · extraction (the synthetic stand-in), then reconcile
    at = goto(at, "extract")
    assert "Test mode" in texts(at)
    at.button(key="g_extract").click()
    run(at)
    j = finish("extract")
    assert j.rcs == [0, 0, 0, 0], "extract, combine, disability notes, combine again"
    at = run(at)
    assert "Extraction finished" in texts(at)

    # 10 · the tool's accuracy needs review decisions first
    at = goto(at, "accuracy")
    assert "Review some patients first" in texts(at) and not [b for b in at.button if b.key == "g_accuracy"]

    # 8 · a few review decisions (the review screens have tests of their own): every patient's diagnosis check
    from nmosd.review.store import GATE, ReviewStore

    s = ReviewStore(data_dir / "deid" / "review.sqlite", data_dir / "deid" / "extract.sqlite",
                    data_dir / "deid" / "deid.sqlite")
    try:
        for sid in s.patients():
            for fs in s.fields(sid, GATE, GATE):
                s.record("Synthetic Reviewer", "accept", sid, GATE, GATE, fs.field, reason="synthetic test")
    finally:
        s.close()
    time.sleep(1.1)   # the export below must be newer than the review database to count as done

    # 9 · export
    at = goto(at, "export")
    at.button(key="g_export").click()
    run(at)
    finish("export")
    at = run(at)
    assert (data_dir / "deid" / "export" / "redcap_import_deidentified.csv").exists()
    assert "File made." in texts(at)

    # 9b · compare with the REDCap reference, and make the final file (both need the record map and the passphrase)
    at.text_input(key="g_rmap").input(str(synthetic_dir / "record_map.csv"))
    at.text_input(key="g_pass_reid").input("not the passphrase")
    at.button(key="g_eval").click()
    at = run(at)
    assert "does not open the key" in texts(at)
    at.text_input(key="g_pass_reid").input(PASS)
    at.button(key="g_eval").click()
    run(at)
    finish("evaluate")
    at = run(at)
    assert "Comparison done" in texts(at) and "Single-value fields" in texts(at)
    assert (data_dir / "work" / "reports" / "evaluation.md").exists()
    at.text_input(key="g_pass_reid").input(PASS)
    at.button(key="g_reid").click()
    run(at)
    finish("reidentify")
    assert (data_dir / "work" / "reidentified" / "redcap_import_final.csv").exists()
    at = run(at)

    # 10 · export the tool's accuracy (tests/test_accuracy.py checks the tables themselves)
    at = goto(at, "accuracy")
    at.text_input(key="g_acc_label").input("synthetic-oracle")
    at.button(key="g_accuracy").click()
    run(at)
    j = finish("accuracy")
    assert "--label" in json.dumps([s.args for s in j.steps])
    at = run(at)
    assert "Accuracy table exported" in texts(at) and "Diagnosis check (per patient)" in texts(at)
    (snap,) = snapshots(data_dir / "work" / "reports" / "accuracy")
    assert (snap / "decisions_with_study_ids.csv").exists() and "Latest export" in " ".join(h.value for h in at.subheader)
    assert [d for d in at.get("download_button") if d.proto.label.startswith("Download the accuracy tables")]
    names = set(zipfile.ZipFile(io.BytesIO(shareable_zip(snap))).namelist())
    assert names == {f"{snap.name}/{n}" for n in SHAREABLE}, "never the file with study IDs"

    # every step is ticked except the review itself
    labels = at.radio(key="g_step").options
    assert all(lbl.startswith("✅") for lbl in labels if "Review" not in lbl and "Check this computer" not in lbl)


def test_commands_started_outside_the_app_are_recognised():
    from nmosd.app.checks import pipeline_command

    assert pipeline_command(["/x/.venv/bin/python", "-u", "-m", "nmosd", "extract", "--model", "qwen3:8b"]) == "extract"
    assert pipeline_command(["python", "-m", "nmosd", "--settings", "s.yaml", "deid"]) == "deid"
    assert pipeline_command(["C:\\app\\.venv\\Scripts\\nmosd.exe", "leak-test"]) == "leak-test"
    assert pipeline_command(["python", "-m", "streamlit", "run", "/x/src/nmosd/app/guide.py"]) is None
    assert pipeline_command(["python", "-m", "nmosd.app.runner", "job.json"]) is None


def test_review_outcomes_count_the_last_decision_per_field():
    from nmosd.eval.compare_gold import review_outcomes
    from nmosd.eval.report import render_report

    acts = [
        {"study_id": "A", "instrument": "relapse", "instance_key": "1", "field": "date_onset", "action": "correct"},
        {"study_id": "A", "instrument": "relapse", "instance_key": "1", "field": "date_onset", "action": "accept"},
        {"study_id": "A", "instrument": "relapse", "instance_key": "1", "field": "relapse_phenotype", "action": "correct"},
        {"study_id": "A", "instrument": "relapse", "instance_key": "new1", "field": None, "action": "add_instance"},
        {"study_id": "A", "instrument": "treatment", "instance_key": "2", "field": None, "action": "delete_instance"},
        {"study_id": "A", "instrument": "treatment", "instance_key": "3", "field": None, "action": "delete_instance"},
        {"study_id": "A", "instrument": "treatment", "instance_key": "3", "field": None, "action": "restore_instance"},
        {"study_id": "A", "instrument": "treatment", "instance_key": "3", "field": "maintenance_therapy", "action": "unresolved"},
        {"study_id": "A", "instrument": "treatment", "instance_key": "3", "field": None, "action": "comment"},
    ]
    out = review_outcomes(acts)
    assert out == {"relapse": {"accept": 1, "correct": 1, "episodes_added": 1},
                   "treatment": {"unresolved": 1, "episodes_deleted": 1}}
    text = render_report({"records": 1, "fields": {}, "episodes": {}, "dates": {}, "checkbox": {},
                          "review_outcomes": out})
    assert "| relapse | 1 | 1 | 0 | 50% | 1 | 0 |" in text


# ============================================================================================== launcher
class _FakeServer:
    pid = 4242
    started: ClassVar[list] = []

    def __init__(self, cmd, env):
        _FakeServer.started.append(cmd)

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


def _launch(monkeypatch, tmp_path, busy_ports, running_port=None):
    import nmosd.app.launch as L
    from nmosd.config import load_settings

    opened: list[str] = []
    _FakeServer.started = []
    monkeypatch.setattr(L, "SERVER_FILE", tmp_path / "server.json")
    monkeypatch.setattr(L, "port_in_use", lambda port: port in busy_ports)
    monkeypatch.setattr(L, "running_guide_port", lambda: running_port)
    monkeypatch.setattr(L, "wait_until_serving", lambda port, proc: True)
    monkeypatch.setattr(L.subprocess, "Popen", _FakeServer)
    monkeypatch.setattr(L.webbrowser, "open", opened.append)
    rc = L.launch_app(load_settings(REPO_ROOT / "config" / "settings.example.yaml"), tmp_path / "s.yaml")
    return rc, opened


def test_launcher_reopens_its_own_running_app(monkeypatch, tmp_path):
    rc, opened = _launch(monkeypatch, tmp_path, busy_ports={8501}, running_port=8501)
    assert rc == 0 and opened == ["http://127.0.0.1:8501"] and _FakeServer.started == []


def test_launcher_avoids_a_port_held_by_another_app(monkeypatch, tmp_path, capsys):
    """E.g. `nmosd review` still running on 8501: never open the browser on it; start on the next free port."""
    rc, opened = _launch(monkeypatch, tmp_path, busy_ports={8501})
    assert rc == 0 and opened == ["http://127.0.0.1:8502"]
    cmd = _FakeServer.started[0]
    assert cmd[cmd.index("--server.port") + 1] == "8502" and cmd[cmd.index("--server.address") + 1] == "127.0.0.1"
    assert "perhaps a review app" in capsys.readouterr().out
    assert json.loads((tmp_path / "server.json").read_text()) == {"pid": 4242, "port": 8502}


def test_no_server_file_means_no_running_app(monkeypatch, tmp_path):
    import nmosd.app.launch as L

    monkeypatch.setattr(L, "SERVER_FILE", tmp_path / "missing.json")
    assert L.running_guide_port() is None
