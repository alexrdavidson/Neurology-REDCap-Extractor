"""`nmosd lab-shapes` (18.11.26): how lab results are written, as masked shapes and counts, never a value.
Synthetic data only."""

from __future__ import annotations

import io
import sqlite3

from nmosd.eval.lab_shapes import run_lab_shapes, shape


def test_shape_masks_values_and_names_but_keeps_lab_words():
    s = shape("CD19 Absolute Count      12 cells/uL     03/14/2024     Dr Smithson")
    assert s == "CD9 Absolute Count | 9 cells/uL | 9/9/9 | a…", "a run of masked words is one mark"
    assert "Smithson" not in s and "12" not in s and "2024" not in s
    s = shape("IgG 650 mg/dL (L) drawn by Johnson at Springfield General on 1/2/2023")
    assert "650" not in s and "Johnson" not in s and "Springfield" not in s and s.startswith("IgG 9 mg/dL (L)")
    assert shape("AQP4 positive 1:100 per [CLINICIAN_04] on [[2024 D05]]") == "AQP9 positive 9:9 per [#] on <#>"


def _db(tmp_path, rows):
    p = tmp_path / "staging.sqlite"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE documents (empi TEXT, text TEXT)")
    con.executemany("INSERT INTO documents VALUES (?, ?)", rows)
    con.commit()
    con.close()
    return p


def test_shapes_shown_only_when_shared_and_no_value_is_printed(tmp_path):
    rows = []
    for pat in ("P1", "P2", "P3"):
        for k in range(2):
            rows.append((pat, f"Labs\nCD19 Absolute Count      {10 + k} cells/uL     0{k + 1}/14/2024\n"
                              f"IgG      {600 + k} mg/dL\nSeen by Dr Abernathy{pat}.\n"))
    rows.append(("P9", "CD20 was 777 when Zebedee Quixote called from 555-0199 about her B cells"))   # once only
    buf = io.StringIO()
    res = run_lab_shapes(_db(tmp_path, rows), min_notes=3, min_patients=2, stdout=buf)
    out = buf.getvalue()
    assert "7 notes of 4 patients searched" in out
    assert res["CD19 / CD20 / B cells"] == {"notes": 7, "patients": 4, "table row": {"shapes": 1, "shapes_shown": 1},
                                           "sentence": {"shapes": 1, "shapes_shown": 0}}
    assert "- 6 notes, 3 patients: `CD9 Absolute Count | 9 cells/uL | 9/9/9`" in out
    assert "- 6 notes, 3 patients: `IgG | 9 mg/dL`" in out
    for secret in ("Abernathy", "Zebedee", "Quixote", "555", "777", "600", "2024", "P1", "P9"):
        assert secret not in out, secret


def test_source_mode_lists_lab_table_test_names_never_results(tmp_path):
    p = tmp_path / "export.sqlite"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE Vis (EMPI TEXT, Report_Text TEXT)")
    con.execute("CREATE TABLE Lab (EMPI TEXT, Test_Description TEXT, Group_Id TEXT, Result TEXT, Seq_Date_Time TEXT, "
                "Result_Text TEXT)")
    for pat in ("1001", "1002", "1003"):
        con.execute("INSERT INTO Vis VALUES (?, ?)", (pat, "IgG      612 mg/dL\n"))
        con.executemany("INSERT INTO Lab VALUES (?,?,?,?,?,?)", [
            (pat, "CD19 Absolute", "FLOW", "14", "03/14/2024 08:00", "see Dr Abernathy"),
            (pat, "IGG", "IMMUNO", "612", "03/14/2024 08:00", ""),
            (pat, "Sodium", "CHEM", "140", "03/14/2024 08:00", "")])
    con.execute("INSERT INTO Lab VALUES ('1001', 'CD20 rare send-out for Quixote', 'FLOW', '3', '', '')")
    con.commit()
    con.close()
    buf = io.StringIO()
    res = run_lab_shapes(source=p, min_notes=3, min_patients=2, stdout=buf)
    out = buf.getvalue()
    assert "3 notes of 3 patients searched" in out and "## Laboratory table `Lab`: 10 rows, 3 patients" in out
    assert "- 3 rows, 3 patients · `Test_Description` = `CD19 Absolute`" in out
    assert "- 3 rows, 3 patients · `Test_Description` = `IGG`" in out
    assert "Sodium" not in out and "Quixote" not in out and "Abernathy" not in out and "612" not in out
    assert "1001" not in out and "03/14" not in out
    assert res["lab_tables"]["Lab"]["rows"] == 10


def test_shapes_covers_the_other_structured_data_and_lists_table_columns(tmp_path):
    p = tmp_path / "export.sqlite"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE Vis (EMPI TEXT, Report_Text TEXT)")
    con.execute("CREATE TABLE Med (EMPI TEXT, Medication TEXT, Medication_Date TEXT, Clinic TEXT)")
    note = ("Administrations This Visit\nravulizumab-cwvz (ULTOMIRIS) 3,300 mg in sodium chloride 0.9% 66 mL IVPB\n"
            "Admin Date      Action     Dose       Rate       Route      Documented By\n"
            "01/10/2025      New Bag    3,300 mg   95 mL/hr   Intravenous   Nurse Abernathy\n"
            "Wt 68.2 kg (150 lb)   Ht 167.6 cm\nEDSS 3.5 today per Dr Quixote\n"
            "Neuromyelitis optica (Primary)      G36.0      noted 03/14/2019\nAdmission Date: 02/01/2025\n")
    for pat in ("1001", "1002", "1003"):
        con.execute("INSERT INTO Vis VALUES (?, ?)", (pat, note))
        con.execute("INSERT INTO Med VALUES (?, 'Rituximab 1000 mg IV', '03/14/2019', 'Springfield Neuro')", (pat,))
    con.commit()
    con.close()
    buf = io.StringIO()
    res = run_lab_shapes(source=p, min_notes=3, min_patients=2, stdout=buf, everything=True)
    out = buf.getvalue()
    assert "# How structured data is written in the notes" in out
    assert "`ravulizumab-a (ULTOMIRIS) 9 mg in sodium chloride 9% 9 mL IVPB`" in out
    assert "`Admin Date | Action | Dose | Rate | Route | Documented By`" in out
    assert "`9/9/9 | New Bag | 9 mg | 9 mL/hr | Intravenous | a…`" in out
    assert "`Wt 9 kg (9 lb) | Ht 9 cm`" in out and "`EDSS 9 a per a…`" in out
    assert "`Neuromyelitis optica (Primary) | G9 | noted 9/9/9`" in out and "`Admission Date: 9/9/9`" in out
    assert "- `Med` (Medications): 3 rows, 3 patients. Columns: `EMPI`, `Medication`, `Medication_Date`, `Clinic`" in out
    assert "  - rituximab: 3 rows, 3 patients" in out
    for secret in ("Abernathy", "Quixote", "Springfield", "1001", "68.2", "2019", "3,300"):
        assert secret not in out, secret
    assert res["Weight and height"]["notes"] == 3 and "structured_tables" in res
    # labs only (`lab-shapes`) leaves the other families out
    buf = io.StringIO()
    run_lab_shapes(source=p, stdout=buf)
    assert "Weight and height" not in buf.getvalue() and "Structured tables" not in buf.getvalue()
