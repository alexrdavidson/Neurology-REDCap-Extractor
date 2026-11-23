"""18.11.26: hospital names come from the notes when the notes still name hospitals. Synthetic data only.

De-identification replaces hospital names with [HOSPITAL_nn], so hx_opcare (#39), nr_hosp_2 (#61) and nr_hosp (#94)
were left to the reviewer. When the notes are kept as they are (`deid.enabled: false`) or de-identified with
`deid.tokenise_hospitals: false`, the `hospital` field group reads them: an admission's hospital goes to the stay the
admission groups found (never a stay of its own), and hx_opcare is the hospital named most often.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import sqlite3

import pytest

from nmosd.extract.stage2_instruments import (
    admissions,
    demographics_extra,
    finish,
    merge_relapse_details,
    route_admissions,
    stay_hospitals,
)
from nmosd.extract.stage2_reconcile import Item, run_reconcile
from nmosd.redcap.dictionary import load_dictionary
from nmosd.redcap.sources import (
    NO_HOSPITAL_NAMES,
    hospital_names_kept,
    not_filled,
    review_fields,
    source,
)
from tests.conftest import REPO

DICT = REPO / "config/redcap/data_dictionary.csv"


@pytest.fixture(scope="module")
def dic():
    return load_dictionary(DICT)


def _item(n, group, fields, *, doc_date="2018-03-10", est=None, neg=False, hyp=False):
    ev = fields.get("nr_admission")
    return Item(f"i{n}", group, f"D{n}", doc_date, n, dict(fields), ev, est, neg, hyp, [f"e{n}"],
                {k: f"e{n}" for k in fields}, "q")


# ---------------------------------------------------------------------------------------------
# Where the fields come from
# ---------------------------------------------------------------------------------------------
def test_hospital_names_kept_reads_the_manifest():
    assert not hospital_names_kept(None) and not hospital_names_kept({})
    assert not hospital_names_kept({"deidentified": True, "tokenise_hospitals": True})
    assert hospital_names_kept({"deidentified": False, "tokenise_hospitals": True}), "notes kept as they are"
    assert hospital_names_kept({"deidentified": True, "tokenise_hospitals": False}), "names not tokenised"


def test_hospital_fields_are_read_from_the_notes_only_when_they_name_hospitals(dic):
    for f in ("hx_opcare", "nr_hosp", "nr_hosp_2"):
        assert source(dic, f) == ("reviewer", NO_HOSPITAL_NAMES)
        assert source(dic, f, hospital_names=True) == ("notes", "the notes (field group hospital)")
    assert source(dic, "nr_admission", True)[1] == "the notes (field group admission)", "dates stay with admission"
    deid, kept = review_fields(dic), review_fields(dic, True)
    assert "hx_opcare" not in deid["demographics"] and "hx_opcare" in kept["demographics"]
    assert "nr_hosp_2" in kept["relapse"] and "nr_hosp" in kept["non_relapse_hospitalization"]
    assert {f for _, f, _ in not_filled(dic, "demographics")} - {f for _, f, _ in not_filled(dic, "demographics", True)} \
        == {"hx_opcare"}
    # everything else is unchanged
    for form in dic.forms:
        assert [f for f in kept[form] if f not in ("hx_opcare", "nr_hosp", "nr_hosp_2")] == deid[form]


# ---------------------------------------------------------------------------------------------
# Admissions: the hospital of each stay
# ---------------------------------------------------------------------------------------------
def test_an_admission_gets_its_hospital_without_changing_its_dates(dic):
    stay = [_item(1, "admission", {"nr_admission": "2018-03-05", "nr_discharge": "2018-03-12", "for_attack": "0",
                                   "nr_cause": ["1"]})]
    hosp = [_item(2, "hospital", {"nr_admission": "2018-03-03", "nr_hosp": "Lowell  General\nHospital"}, est="1"),
            _item(3, "hospital", {"nr_hosp": "Lowell General Hospital"}, doc_date="2018-03-12")]
    nr = route_admissions([], stay + hosp, dic)
    assert len(nr) == 1
    v = finish(nr[0], dic)["values"]
    assert v["nr_hosp"] == "Lowell General Hospital", "whitespace made uniform"
    assert v["nr_admission"] == "2018-03-05", "the admission group's date, not the hospital mention's"
    assert set(finish(nr[0], dic)["evidence"]["nr_hosp"]) == {"e2", "e3"}
    assert route_admissions([], stay, dic)[0]["date"] == nr[0]["date"]


def test_a_hospital_mention_never_makes_an_admission(dic):
    hosp = [_item(1, "hospital", {"nr_admission": "2018-03-05", "nr_hosp": "MGH"}, est="1")]
    assert route_admissions([], hosp, dic) == []
    assert admissions(hosp) == []


def test_an_attacks_admission_gets_nr_hosp_2(dic):
    rel = merge_relapse_details([{"anchor": "2018-03-01", "values": {"date_onset": "2018-03-01"}, "evidence": ["r1"],
                                  "conflict": "", "members": []}], [], dic, 30, 120)
    items = [_item(1, "admission", {"nr_admission": "2018-03-05", "for_attack": "1"}),
             _item(2, "hospital", {"nr_admission": "2018-03-05", "nr_hosp": "MGH"}, est="1"),
             _item(3, "hospital", {"nr_admission": "2018-03-05", "nr_hosp": "Wexley Falls Community Hospital"},
                   est="1"),
             _item(4, "hospital", {"nr_admission": "2018-03-06", "nr_hosp": "MGH"}, est="1")]
    assert route_admissions(rel, items, dic) == []
    out = finish(rel[0], dic)
    assert out["values"]["admission"] == "1" and out["values"]["nr_hosp_2"] == "MGH", "the name given most often"
    assert any("2 different hospitals" in n for n in out["info"]), "a transfer is pointed out"
    assert "nr_hosp" not in out["values"], "nr_hosp belongs to the non-relapse form"


def test_a_vague_date_is_placed_only_when_one_stay_fits(dic):
    def stays_of(adm_dates):
        return [[_item(k, "admission", {"nr_admission": d, "for_attack": "0"})] for k, d in enumerate(adm_dates)]

    year = _item(9, "hospital", {"nr_admission": "2018-07-01", "nr_hosp": "MGH"}, est="3")
    month = _item(8, "hospital", {"nr_admission": "2018-03-15", "nr_hosp": "MGH"}, est="2")
    one = stays_of(["2018-03-05"])
    assert stay_hospitals(one, [year, month]) == [[year, month]]
    two = stays_of(["2018-03-05", "2018-10-20"])
    assert stay_hospitals(two, [year, month]) == [[month], []], "a year with two stays in it: not placed"
    assert stay_hospitals(stays_of(["2016-03-05"]), [year, month]) == [[]]


def test_negated_or_planned_mentions_are_left_out(dic):
    stay = [[_item(1, "admission", {"nr_admission": "2018-03-05", "for_attack": "0"})]]
    neg = _item(2, "hospital", {"nr_admission": "2018-03-05", "nr_hosp": "MGH"}, est="1", neg=True)
    hyp = _item(3, "hospital", {"nr_admission": "2018-03-05", "nr_hosp": "BWH"}, est="1", hyp=True)
    assert stay_hospitals(stay, [neg, hyp]) == [[]]


def test_two_undated_admissions_far_apart_do_not_fail():
    """Before 18.11.26 a second admission mention without an admission date failed once the first had started a
    stay of its own (its start date was None)."""
    items = [_item(1, "admission", {"for_attack": "0"}, doc_date="2019-01-01"),
             _item(2, "admission", {"for_attack": "0"}, doc_date="2021-06-01"),
             _item(3, "admission", {"for_attack": "0"}, doc_date="2019-01-05")]
    stays = admissions(items)
    assert [[i.item_id for i in s] for s in stays] == [["i1", "i3"], ["i2"]]


# ---------------------------------------------------------------------------------------------
# hx_opcare
# ---------------------------------------------------------------------------------------------
def _demo(items, dic):
    return demographics_extra(items, dic, living="1", dx_date=None, relapse_insts=[], nr_insts=[], courses=[],
                              antibodies=[], edss=[], last_contact=None, gate_dx=None)


def test_hx_opcare_is_the_hospital_named_most_often(dic):
    items = [_item(1, "hospital", {"hx_opcare": "Lahey Clinic"}, doc_date="2015-01-01"),
             _item(2, "hospital", {"hx_opcare": "MGH"}, doc_date="2019-01-01"),
             _item(3, "hospital", {"hx_opcare": "MGH"}, doc_date="2020-01-01"),
             _item(4, "hospital", {"nr_hosp": "BWH", "nr_admission": "2020-02-01"}, est="1")]
    out = _demo(items, dic)
    assert out["values"]["hx_opcare"] == "MGH"
    assert set(out["evidence"]["hx_opcare"]) == {"e1", "e2", "e3"}, "an admission's hospital is not outpatient care"
    assert any("hx_opcare: the notes name 2 different hospitals" in n for n in out["info"])
    tie = _demo(items[:2], dic)
    assert tie["values"]["hx_opcare"] == "MGH", "a tie goes to the latest note"
    assert "hx_opcare" not in _demo([], dic)["values"]


# ---------------------------------------------------------------------------------------------
# End to end: extraction skips the group, reconcile never uses old mentions on de-identified notes
# ---------------------------------------------------------------------------------------------
def test_deidentified_pipeline_does_not_read_hospital_names(pipeline):
    assert "Not reading hospital" in pipeline["extract_stdout"]
    con = sqlite3.connect(pipeline["extract"])
    try:
        groups = {json.loads(r[0])["group"] for r in con.execute("SELECT config_json FROM runs")}
    finally:
        con.close()
    assert "hospital" not in groups and "admission" in groups


def test_reconcile_uses_hospital_mentions_only_while_the_notes_name_hospitals(pipeline, tmp_path):
    root = tmp_path / "deid"
    root.mkdir()
    deid, extract = root / "deid.sqlite", root / "extract.sqlite"
    shutil.copy(pipeline["deid"], deid)
    shutil.copy(pipeline["extract"], extract)
    con = sqlite3.connect(extract)
    doc = sqlite3.connect(deid).execute("SELECT doc_id, study_id, report_date FROM documents LIMIT 1").fetchone()
    con.execute("INSERT INTO runs VALUES (?,?,?,?)", ("hosp-run", "2099-01-01T00:00:00",
                                                     json.dumps({"group": "hospital"}), "hosp-run"))
    cols = ["evidence_id", "run_id", "doc_id", "chunk_id", "study_id", "field_group", "item_id", "field", "value",
            "quote", "char_start", "char_end", "certainty", "negated", "hypothetical", "verified", "doc_date"]
    con.execute(f"INSERT INTO evidence ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                ("hosp-ev", "hosp-run", doc[0], f"{doc[0]}:hospital:1", doc[1], "hospital", "hosp-item", "hx_opcare",
                 "Wexley Falls Community Hospital", "q", 0, 1, "high", 0, 0, 1, doc[2]))
    con.commit()
    con.close()

    def hx_opcare() -> set[str]:
        buf = io.StringIO()
        assert run_reconcile(extract, deid, pipeline["settings"], stdout=buf) == 0
        run = re.search(r"run (rec-\S+)", buf.getvalue()).group(1)   # two runs in one second: MAX(run_id) can't tell
        c = sqlite3.connect(extract)
        try:
            return {r[0] for r in c.execute("SELECT value FROM proposals WHERE field='hx_opcare' AND run_id=?", (run,))}
        finally:
            c.close()

    assert hx_opcare() == set(), "hospital names tokenised: an old mention is never proposed"
    c = sqlite3.connect(deid)
    c.execute("UPDATE manifest SET value='false' WHERE key='tokenise_hospitals'")
    c.commit()
    c.close()
    assert hx_opcare() == {"Wexley Falls Community Hospital"}
