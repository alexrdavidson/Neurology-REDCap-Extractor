"""Phase 2: staging round-trip on synthetic data that mimics the real schema."""

from __future__ import annotations

import io
import json
import os
import sqlite3
from collections import Counter

import pytest

from nmosd.config import load_settings
from nmosd.extract.dedupe import dedupe_patient
from nmosd.ingest.load_staging import stage
from nmosd.ingest.select_pilot import select_pilot
from nmosd.safety import SafetyError
from nmosd.synth.build import build
from nmosd.textproc.boilerplate import find_boilerplate
from nmosd.textproc.view import TextView
from tests.conftest import NOTE_TABLES, REPO, SEED

SETTINGS = load_settings(os.environ["NMOSD_SETTINGS"])


@pytest.fixture(scope="module")
def staged(synthetic_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("phi") / "staging" / "staging.sqlite"
    buf = io.StringIO()
    res = stage(synthetic_dir / "synthetic_rpdr.sqlite", synthetic_dir / "pilot_empis.txt", out, SETTINGS, stdout=buf)
    con = sqlite3.connect(out)
    con.row_factory = sqlite3.Row
    yield {"path": out, "con": con, "stdout": buf.getvalue(), "res": res}
    con.close()


def test_only_pilot_patients_staged(staged, pilot_empis):
    empis = {r[0] for r in staged["con"].execute("SELECT DISTINCT empi FROM documents")}
    assert empis == set(pilot_empis)
    assert "100001111" not in empis
    assert staged["res"]["patients_found"] == 5


def test_stdout_is_counts_only(staged, seeded_phi, pilot_empis):
    out = staged["stdout"]
    assert "Staged 5 of 5 requested patients" in out and "pt1:" in out
    for e in pilot_empis:
        assert e not in out
    for v in {x["value"] for vs in seeded_phi.values() for x in vs if len(x["value"]) >= 4}:
        assert v not in out


def test_round_trip_text_is_byte_for_byte(staged, synthetic_tables, pilot_empis):
    con = staged["con"]
    docs = {r["doc_pk"]: r["text"] for r in con.execute("SELECT doc_pk, text FROM documents")}
    sources = list(con.execute("SELECT * FROM document_sources"))
    by_key = {(s["source_table"], s["report_number"], s["report_status"]): docs[s["doc_pk"]] for s in sources}
    n = 0
    for t in NOTE_TABLES:
        for r in synthetic_tables[t]:
            if r["EMPI"] not in pilot_empis:
                continue
            key = (t, r["Report_Number"], r["Report_Status"])
            if r["Report_Status"] == "P":
                assert key not in by_key, "preliminary dropped because a final version exists"
                continue
            assert by_key[key] == r["Report_Text"][: -len("[report_end]")]
            assert "\r\n" in by_key[key] and "[report_end]" not in by_key[key]
            n += 1
    assert n == len(sources)


def test_duplicates_collapsed_with_all_sources(staged):
    con = staged["con"]
    multi = list(con.execute("SELECT doc_pk, n_sources FROM documents WHERE n_sources > 1"))
    assert multi, "the duplicated radiology text collapses into one document"
    for r in multi:
        nums = [s[0] for s in con.execute("SELECT report_number FROM document_sources WHERE doc_pk=?", (r["doc_pk"],))]
        assert len(nums) == r["n_sources"] and len(set(nums)) == len(nums)
    out = staged["stdout"]
    assert "| Radiology |" in out


def test_dates_and_dob(staged):
    con = staged["con"]
    assert con.execute("SELECT COUNT(*) FROM documents WHERE report_datetime IS NULL").fetchone()[0] == 0
    two = con.execute("SELECT report_datetime_raw, report_datetime FROM documents WHERE source_table='VisitNote' LIMIT 1").fetchone()
    four = con.execute("SELECT report_datetime_raw, report_datetime FROM documents WHERE source_table='Radiology' LIMIT 1").fetchone()
    assert len(two[0]) == 17 and two[1][:2] == "20"
    assert four[0].endswith(("AM", "PM")) and four[1][:2] == "20"
    dob = dict(con.execute("SELECT empi, dob FROM demographics"))
    assert dob["100009912"] == "1934-05-12" and dob["100005563"] == "1999-01-30"


def test_relevance_and_site(staged):
    con = staged["con"]
    cls = {(r["report_description"], r["relevance_class"]) for r in con.execute(
        "SELECT report_description, relevance_class FROM documents")}
    expect = {("MRI BRAIN W/WO CONTRAST", "neuro_mri"), ("XR CHEST 2 VIEWS", "low_value"),
              ("Flow Cytometry", "flow_cytometry"), ("Surgical Pathology", "low_value"),
              ("Patient Email", "patient_message"), ("MyChart Renewal", "patient_message"),
              ("Lactation Note", "pregnancy_related"), ("Nursing Note", "low_value"),
              ("Nurse Triage", "low_value"), ("Office Visit", "clinical_note"),
              ("Discharge Summary", "clinical_note"), ("History and Physical", "clinical_note")}
    assert expect <= cls
    sites = Counter(r[0] for r in con.execute("SELECT site FROM documents"))
    assert set(sites) <= set("BMFWNSVU") and sites["M"] > 0


def test_refuses_output_inside_repo(synthetic_dir):
    with pytest.raises(SafetyError):
        stage(synthetic_dir / "synthetic_rpdr.sqlite", synthetic_dir / "pilot_empis.txt",
              REPO / "data" / "staging.sqlite", SETTINGS, stdout=io.StringIO())


def test_classic_rpdr_names_and_extra_tables(tmp_path):
    d = tmp_path / "classic"
    build(d, seed=SEED, extra_tables=True, table_names="rpdr")
    out = tmp_path / "phi" / "staging.sqlite"
    res = stage(d / "synthetic_rpdr.sqlite", d / "pilot_empis.txt", out, SETTINGS, stdout=io.StringIO())
    assert {"VisitNote", "ProgressNote", "Radiology", "Medications", "Labs", "Mrn", "Contact"} <= set(res["counts"])
    con = sqlite3.connect(out)
    tabs = {r[0] for r in con.execute("SELECT DISTINCT table_name FROM structured_rows")}
    assert {"Medications", "Labs", "Mrn", "Contact"} <= tabs
    con.close()


def test_preliminary_kept_when_no_final(tmp_path):
    db = tmp_path / "src.sqlite"
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE Radiology ("EMPI","Report_Number","Report_Date_Time","Report_Description",'
                '"Report_Status","Report_Type","Report_Text")')
    con.executemany("INSERT INTO Radiology VALUES (?,?,?,?,?,?,?)", [
        ("1", "A", "3/4/2019 9:00:00 AM", "MRI BRAIN", "P", "MRAD", "prelim only\r\n[report_end]"),
        ("1", "B", "3/5/2019 9:00:00 AM", "MRI BRAIN", "P", "MRAD", "draft\r\n[report_end]"),
        ("1", "B", "3/5/2019 9:00:00 AM", "MRI BRAIN", "R", "MRAD", "revised\r\n[report_end]"),
    ])
    con.commit()
    con.close()
    (tmp_path / "e.txt").write_text("1\n")
    out = tmp_path / "phi" / "s.sqlite"
    res = stage(db, tmp_path / "e.txt", out, SETTINGS, stdout=io.StringIO())
    assert res["counts"]["Radiology"] == {"rows": 3, "dropped_status": 1, "kept": 2, "documents": 2, "collapsed": 0}


def test_select_pilot_aggregates_only(synthetic_dir, tmp_path, seeded_phi):
    all_staging = tmp_path / "phi" / "all.sqlite"
    stage(synthetic_dir / "synthetic_rpdr.sqlite", None, all_staging, SETTINGS, all_patients=True, stdout=io.StringIO())
    buf = io.StringIO()
    rows = select_pilot(all_staging, tmp_path / "phi" / "pilot.sqlite", SETTINGS, stdout=buf)
    assert len(rows) == 6
    assert all(r["tokens_after"] < r["tokens_before"] for r in rows)
    assert all(r["keyword_flags"]["aqp4"] for r in rows)
    assert sum(r["keyword_flags"]["mog"] for r in rows) >= 1
    out = buf.getvalue()
    assert "Est. tokens before filtering" in out and "| rituximab |" in out
    for r in rows:
        assert r["empi"] not in out
    con = sqlite3.connect(tmp_path / "phi" / "pilot.sqlite")
    assert con.execute("SELECT COUNT(*) FROM pilot_candidates").fetchone()[0] == 6
    flags = json.loads(con.execute("SELECT keyword_flags FROM pilot_candidates LIMIT 1").fetchone()[0])
    assert "pregnancy" in flags
    con.close()


# --- text utilities ---------------------------------------------------------------------------
def test_text_view_maps_offsets_back():
    text = "AAAA\r\nBBBB\r\n\r\nCCCC DDDD"
    v = TextView.full(text).remove([(6, 12)])
    assert v.text == "AAAA\r\n\r\nCCCC DDDD"
    i = v.text.index("CCCC")
    assert text[v.to_original(i):v.to_original(i) + 4] == "CCCC"
    r = v.restrict([(14, 18)])
    assert r.text == "CCCC"


def test_boilerplate_and_copy_forward(synthetic_tables):
    p1 = sorted([r for r in synthetic_tables["VisitNote"] if r["EMPI"] == "100004417"
                 and r["Report_Description"] == "Office Visit"],
                key=lambda r: (r["Report_Date_Time"][6:8], r["Report_Date_Time"]))
    text = p1[1]["Report_Text"]
    names = {n for _, _, n in find_boilerplate(text)}
    assert {"vitals_table", "billing", "encounter_level_documents", "immunisations", "confidentiality_banner"} <= names
    res = dedupe_patient([(str(i), r["Report_Text"]) for i, r in enumerate(p1)])
    assert res.stats["repeated_paragraphs"] > 0
    assert res.stats["chars_after_dedupe"] < res.stats["chars_after_boilerplate"] < res.stats["chars_in"]
    later = res.views["2"].text
    assert "BP Readings from Last" not in later
    ref = res.repeats[0]
    assert ref.first_doc_id < ref.doc_id


def test_paths_with_spaces_and_hashes(synthetic_dir, tmp_path):
    """The real PHI folder ("Private Documents") has spaces; Windows paths need proper file URIs."""
    import shutil

    from nmosd.ingest.access_reader import SqliteReader
    from nmosd.store.db import connect

    weird = tmp_path / "Private Documents #1" / "nmosd extractor"
    weird.mkdir(parents=True)
    shutil.copy(synthetic_dir / "synthetic_rpdr.sqlite", weird / "source copy.sqlite")
    assert "VisitNote" in SqliteReader(weird / "source copy.sqlite").tables()
    out = weird / "work" / "staging db.sqlite"
    stage(weird / "source copy.sqlite", synthetic_dir / "pilot_empis.txt", out, SETTINGS, stdout=io.StringIO())
    con = connect(out, readonly=True)
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] > 100
    con.close()
