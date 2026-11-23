"""Phase 0: guardrail files, deterministic synthetic data that mimics the real export (§1, §1b)."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import date

import yaml

from nmosd.synth.build import build
from nmosd.textproc.dates import parse_dob
from tests.conftest import NOTE_TABLES, REPO, SEED


# --- guardrails ------------------------------------------------------------------------------
def test_guardrail_files_exist():
    claude_md = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "NEVER read, print or query real patient data" in claude_md
    assert "Map REDCap choices by CODE" in claude_md
    settings = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    deny = settings["permissions"]["deny"]
    for rule in ["Read(../data/**)", "Read(./data/**)", "Read(./outputs/**)", "Read(**/*.accdb)", "Read(**/*.sqlite)",
                 "Read(./config/settings.yaml)", "Bash(mdb-export:*)", "Bash(sqlite3:*)"]:
        assert rule in deny
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8").split()
    for pat in ["data/", "outputs/", "*.accdb", "*.mdb", "*.sqlite", "*.db", "config/settings.yaml", "*.key",
                "linkage*", ".venv/"]:
        assert pat in ignore, pat


def test_settings_example_has_no_real_paths():
    text = (REPO / "config" / "settings.example.yaml").read_text(encoding="utf-8")
    assert "Private Documents" not in text
    cfg = yaml.safe_load(text)
    assert cfg["llm"]["host"].startswith("http://127.0.0.1")
    assert cfg["llm"]["temperature"] == 0


# --- determinism -----------------------------------------------------------------------------
def test_synthetic_is_deterministic(tmp_path):
    a = build(tmp_path / "a", seed=SEED)
    b = build(tmp_path / "b", seed=SEED)
    c = build(tmp_path / "c", seed=SEED + 1)
    assert a["content_sha256"] == b["content_sha256"]
    assert a["content_sha256"] != c["content_sha256"]
    for f in ["gold_redcap.csv", "seeded_phi.json", "annotations.json", "identifiers.yaml"]:
        h = [hashlib.sha256((tmp_path / d / f).read_bytes()).hexdigest() for d in "ab"]
        assert h[0] == h[1], f


# --- schema mimics the real export -----------------------------------------------------------
def test_schema_matches_confirmed_export(synthetic_tables):
    for t in NOTE_TABLES:
        assert t in synthetic_tables
        assert list(synthetic_tables[t][0].keys()) == ["EMPI", "Report_Number", "Report_Date_Time",
                                                       "Report_Description", "Report_Status", "Report_Type",
                                                       "Report_Text"]
    dem = synthetic_tables["Demographics"]
    assert {"EMPI", "Date_of_Birth", "Age", "Sex_At_Birth", "Race1", "Ethnic_Group", "Vital_status",
            "Date_Of_Death", "Zip_code"} <= set(dem[0])
    placeholder = [t for t in synthetic_tables if t.startswith("<--")]
    assert placeholder and synthetic_tables[placeholder[0]] == []
    assert all(r["Race2"] == "" for r in dem)
    assert len({r["EMPI"] for r in dem}) == 6


def test_text_format_crlf_and_report_end(synthetic_tables):
    for t in NOTE_TABLES:
        for r in synthetic_tables[t]:
            txt = r["Report_Text"]
            assert txt.endswith("[report_end]")
            assert "\r\n" in txt
            assert "\n" not in txt.replace("\r\n", "")


def test_report_datetime_formats(synthetic_tables):
    for t in NOTE_TABLES:
        lengths = {len(r["Report_Date_Time"]) for r in synthetic_tables[t]}
        if t in ("VisitNote", "ProgressNote", "HistoryAndPhysical"):
            assert lengths == {17}, t
        else:
            assert lengths <= set(range(19, 23)), (t, lengths)


def test_dob_two_digit_year_resolved_by_age(synthetic_tables):
    dem = {r["EMPI"]: r for r in synthetic_tables["Demographics"]}
    elderly = dem["100009912"]
    assert re.fullmatch(r"\d{2}/\d{2}/\d{2} 00:00:00", elderly["Date_of_Birth"])
    res = parse_dob(elderly["Date_of_Birth"], elderly["Age"], reference=date(2026, 11, 4))
    assert res.dob == date(1934, 5, 12) and res.resolved_by == "age"
    young = dem["100005563"]
    assert parse_dob(young["Date_of_Birth"], young["Age"], reference=date(2026, 11, 4)).dob == date(1999, 1, 30)


def test_status_and_site_and_duplicates(synthetic_tables):
    rad = synthetic_tables["Radiology"]
    statuses = Counter(r["Report_Status"] for r in rad)
    assert statuses["P"] >= 1 and statuses["F"] >= 1
    pairs = defaultdict(set)
    for r in rad:
        pairs[r["Report_Number"]].add(r["Report_Status"])
    assert any(s == {"P", "F"} for s in pairs.values()), "a preliminary + final version of one report"
    texts = Counter((r["EMPI"], r["Report_Text"]) for r in rad)
    assert any(c >= 2 for c in texts.values()), "the same report text under two report numbers"
    sites = {r["Report_Type"][0] for t in NOTE_TABLES for r in synthetic_tables[t]}
    assert sites <= set("BMFWNSV") and {"M", "B"} <= sites


def test_note_types_cover_real_categories(synthetic_tables):
    vis = {r["Report_Description"] for r in synthetic_tables["VisitNote"]}
    assert {"Office Visit", "Telemedicine", "ED Provider Note", "Patient Email", "MyChart Renewal", "Procedure",
            "Nurse Triage"} <= vis
    prg = {r["Report_Description"] for r in synthetic_tables["ProgressNote"]}
    assert {"Nursing Note", "Lactation Note"} <= prg
    pat = {r["Report_Description"] for r in synthetic_tables["Pathology"]}
    assert "Flow Cytometry" in pat


# --- §1b text features ------------------------------------------------------------------------
def _texts(tables, table):
    return [r["Report_Text"].replace("\r\n", "\n") for r in tables[table]]


def test_header_blocks_follow_profiled_shapes(synthetic_tables):
    patterns = yaml.safe_load((REPO / "config/deid/patterns.yaml").read_text(encoding="utf-8"))["header"]
    banner = re.compile(patterns["banner"])
    layouts = {lay["name"]: [re.compile(x["optional"] if isinstance(x, dict) else x) for x in lay["lines"]]
               for lay in patterns["layouts"]}
    for t, name in [("Discharge_Summaries", "inpatient_4line"), ("HistoryAndPhysical", "inpatient_4line"),
                    ("VisitNote", "visit_3line")]:
        for txt in _texts(synthetic_tables, t):
            lines = txt.split("\n")
            assert banner.match(lines[0]), t
            assert re.search(r"'\d{6}\.txt'\.$", lines[0])
            for rx, line in zip(layouts[name], lines[1:], strict=False):
                assert rx.match(line), (t, line)
    assert any("“" in t.split("\n")[1] for t in _texts(synthetic_tables, "VisitNote")), "preferred name"
    path = _texts(synthetic_tables, "Pathology")
    assert all(layouts["pathology"][0].match(t.split("\n")[0]) for t in path)
    rad = _texts(synthetic_tables, "Radiology")
    assert all(re.search(r"Exam Date: \d{2}-[A-Z]{3}-\d{4}", t) for t in rad)


def test_sections_dx_tx_and_boilerplate(synthetic_tables):
    vis = "\n".join(_texts(synthetic_tables, "VisitNote"))
    for h in ["HPI", "INTERVAL EVENTS", "Past Medical History", "Current medication regimen", "PHYSICAL EXAM",
              "Neurologic examination", "DATA / RESULTS", "Selected relevant imaging",
              "Selected relevant laboratory data", "ASSESSMENT & PLAN", "TB T spot", "Hep B virus serologies"]:
        assert h in vis, h
    assert re.search(r"^Dx: ", vis, re.M) and re.search(r"^Tx: ", vis, re.M)
    for b in ["Encounter-Level Documents", "Order-Level Documents", "Patient-Level Documents",
              "ESTABLISHED HIGH MDM 40 MIN", "BP Readings from Last 3 Encounters", "TRIVALENT PF"]:
        assert b in vis, b
    dis = "\n".join(_texts(synthetic_tables, "Discharge_Summaries"))
    for h in ["Principal Problem", "Brief Summary of Hospitalization", "Hospital Course by Problem", "INITIAL HPI",
              "ITEMS FOR FOLLOW UP"]:
        assert h in dis
    hnp = "\n".join(_texts(synthetic_tables, "HistoryAndPhysical"))
    for h in ["NEUROLOGY ADMISSION NOTE", "History of Presenting Illness", "Relevant Prior to Admission Medications",
              "DATA & IMAGING", "Active Neurologic Issue(s) & Plan"]:
        assert h in hnp
    rad = "\n".join(_texts(synthetic_tables, "Radiology"))
    for h in ["FINDINGS", "IMPRESSION", "TECHNIQUE", "Findings by level", "CERVICAL SPINE"]:
        assert h in rad


def test_date_lookalikes_present(synthetic_tables):
    allt = "\n".join(t for tab in NOTE_TABLES for t in _texts(synthetic_tables, tab))
    for s in ["20/20", "20/400", "CF at", "5/5", "4+/5", "118/76", "2+", "1/2 tab", "1:100", "C2-C5", "T4-T9",
              "EDSS", "2/4", "3/10"]:
        assert s in allt, s


def test_nasty_dates_present(synthetic_tables):
    allt = "\n".join(t for tab in NOTE_TABLES for t in _texts(synthetic_tables, tab))
    assert re.search(r"ON OD 3/19\b", allt), "ambiguous short form"
    assert "March '19" in allt
    assert re.search(r"\b20\d{2}-\d{2}-\d{2}\b", allt), "ISO date"
    assert "summer 2016" in allt


def test_copy_forward_in_visit_notes(synthetic_tables):
    rows = sorted(synthetic_tables["VisitNote"], key=lambda r: (r["EMPI"], r["Report_Date_Time"][6:8], r["Report_Date_Time"]))
    seen, ratios = defaultdict(set), []
    for r in rows:
        hs = [hashlib.sha1(re.sub(r"\s+", " ", p).strip().lower().encode()).hexdigest()
              for p in re.split(r"\n\s*\n", r["Report_Text"].replace("\r\n", "\n"))
              if len(re.sub(r"\s+", " ", p).strip()) >= 80]
        if hs:
            ratios.append(sum(h in seen[r["EMPI"]] for h in hs) / len(hs))
            seen[r["EMPI"]].update(hs)
    assert 0.30 <= statistics.median(ratios) <= 0.60


def test_seeded_phi_is_nasty(seeded_phi):
    p1 = {(x["category"], x["value"]) for x in seeded_phi["100004417"]}
    cats = {c for c, _ in p1}
    assert {"PATIENT", "ID", "DOB", "PHONE", "EMAIL", "LOCATION", "RELATIVE", "STAFF", "HOSPITAL",
            "ORGANIZATION"} <= cats
    dobs = {v for c, v in p1 if c == "DOB"}
    assert len(dobs) >= 5, dobs
    ids = {v for c, v in p1 if c == "ID"}
    assert any("-" in v for v in ids) and any(v.isdigit() for v in ids), "MRNs with and without dashes"
    assert ("PATIENT", "Roz") in p1
    p4 = {(x["category"], x["value"]) for x in seeded_phi["100009912"]}
    assert any(c == "AGE" and v.startswith("9") for c, v in p4), "ages over 89"


def test_gold_standard(synthetic_dir):
    rows = list(csv.DictReader((synthetic_dir / "gold_redcap.csv").open(encoding="utf-8")))
    recs = {r["record_id"] for r in rows}
    assert len(recs) == 5
    relapses = Counter(r["record_id"] for r in rows if r["redcap_repeat_instrument"] == "relapse")
    assert relapses == {"SYN-01": 3, "SYN-02": 3, "SYN-03": 2, "SYN-04": 3, "SYN-05": 2}
    tx = [r for r in rows if r["redcap_repeat_instrument"] == "treatment"]
    assert [r["maintenance_therapy"] for r in tx if r["record_id"] == "SYN-01"] == ["7", "1", "5"]
    assert not any(r["maintenance_therapy"] == "1" for r in tx if r["record_id"] == "SYN-03"), \
        "SYN-03 only ever had rituximab planned"


def test_annotations_are_exact_quotes(synthetic_tables, annotations):
    by_num = {(r["EMPI"], r["Report_Number"], r["Report_Status"]): r["Report_Text"]
              for t in NOTE_TABLES for r in synthetic_tables[t]}
    n = 0
    for a in annotations:
        txt = by_num[(a["empi"], a["report_number"], a["status"])]
        for ann in a["annotations"]:
            assert ann["quote"] in txt
            n += 1
    assert n > 200
    planned = [ann for a in annotations for ann in a["annotations"]
               if any(i.get("event") == "planned" for i in ann["items"])]
    assert any("Plan to start rituximab" in p["quote"] for p in planned)
