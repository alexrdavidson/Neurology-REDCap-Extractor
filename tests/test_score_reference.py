"""`nmosd score` and `nmosd fast-test` (18.11.26): the proposals against reference answers, on the whole chart or on a
fast set of notes. Synthetic data only: the reference here is built from the synthetic proposals themselves."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
from datetime import date

import pytest

from nmosd.eval.quality_packet import build_packet
from nmosd.eval.score_reference import (
    Ref,
    compare,
    default_key,
    fast_db,
    fast_docs,
    load_proposals,
    load_reference,
    pair_instances,
    run_fast_test,
    run_score,
    scored_field,
)
from nmosd.redcap.dictionary import load_dictionary
from nmosd.store.db import connect
from tests.conftest import PASSPHRASE, REPO

DICT = REPO / "config/redcap/data_dictionary.csv"


def test_compare_by_kind_and_precision():
    assert compare(Ref("code", "2"), "2") == "correct" and compare(Ref("code", "2"), "3") == "wrong"
    assert compare(Ref("code", "2"), None) == "missed" and compare(Ref("code", "2"), "[]") == "missed"
    assert compare(Ref("codes", frozenset({"1", "3"})), '["3", "1"]') == "correct"
    assert compare(Ref("codes", frozenset({"1", "3"})), '["1"]') == "wrong"
    assert compare(Ref("codes", frozenset({"1"})), "1", is_checkbox=True) == "correct"
    d = date(2019, 3, 14)
    assert compare(Ref("date", d), "2019-03-16") == "correct" and compare(Ref("date", d), "2019-03-30") == "wrong"
    assert compare(Ref("date", d, "month"), "2019-03-01") == "correct"
    assert compare(Ref("date", d, "month"), "2019-06-15") == "wrong"
    assert compare(Ref("date", date(2019, 7, 1), "year"), "2019-01-02") == "correct"
    assert compare(Ref("date", None, "unusable"), "2019-01-02") == "not_comparable", "a date the packet cannot give"
    assert compare(Ref("date", None, "unusable"), None) == "missed"
    assert compare(Ref("text", "67.1"), "67") == "correct" and compare(Ref("text", "3000"), "300") == "wrong"
    assert compare(Ref("text", "Leflunomide"), "leflunomide 20 mg") == "correct"
    assert compare(Ref("text", "2 g"), "1000 mg twice") == "not_comparable", "two free texts are not judged"


def test_pairing_by_date_drug_and_antibody():
    r = lambda d, drug, prec="exact": {"date_starting_maint": Ref("date", d, prec), "maintenance_therapy": Ref("code", drug)}  # noqa: E731
    refs = [r(date(2019, 4, 2), "1"), r(date(2021, 7, 1), "4", "year")]
    props = [({"maintenance_therapy": "4", "date_starting_maint": "2025-01-10"}, None),      # same drug, other date
             ({"maintenance_therapy": "1", "date_starting_maint": "2019-04-05"}, None),
             ({"maintenance_therapy": "7", "date_starting_maint": "2019-04-02"}, None)]      # another drug: extra
    assert pair_instances("treatment", refs, props) == [(0, 1), (1, 0), (None, 2)]
    rel = [{"date_onset": Ref("date", date(2018, 5, 1), "month")}, {"date_onset": Ref("date", None, "unusable")}]
    got = pair_instances("relapse", rel, [({"date_onset": "2018-05-20"}, None), ({"date_onset": "2023-01-01"}, None)])
    assert got == [(0, 0), (1, None), (None, 1)], "an undated attack is not paired by guesswork"
    ab = [{"aqp4_assay_date": Ref("date", date(2020, 1, 5)), "aqp4_assay_status": Ref("code", "1")},
          {"mog_assay_date": Ref("date", date(2020, 1, 5)), "mog_test_positive": Ref("code", "0")}]
    got = pair_instances("antibody_lab_data", ab, [({"type_test": "mog", "mog_assay_date": "2020-01-06"}, None),
                                                   ({"type_test": "aqp4", "aqp4_assay_date": "2020-01-04"}, None)])
    assert got == [(0, 1), (1, 0)]
    assert pair_instances("demographics", [{}], [({}, None)]) == [(0, 0)]


@pytest.fixture()
def scored(pipeline, tmp_path):
    """Copies of the synthetic databases, a packet key, and a reference folder that repeats the proposals exactly."""
    d = tmp_path / "deid"
    d.mkdir()
    for k in ("deid", "extract"):
        shutil.copy(pipeline[k], d / pipeline[k].name)
    deid, extract = d / pipeline["deid"].name, d / pipeline["extract"].name
    con = sqlite3.connect(deid)
    con.execute("INSERT OR REPLACE INTO manifest(key, value) VALUES ('signoff_complete', 'true')")
    con.commit()
    con.close()
    packet = tmp_path / "review" / "packet_test"
    res = build_packet(extract, deid, d / "review.sqlite", pipeline["linkage"], packet, pipeline["settings"],
                       passphrase=PASSPHRASE, dictionary=DICT, stdout=io.StringIO())
    key = json.loads(res["key"].read_text(encoding="utf-8"))
    assert res["key"] == default_key(packet / "Claude_reference", deid)
    dic = load_dictionary(DICT)
    ref_dir = packet / "Claude_reference"
    ref_dir.mkdir()
    con = connect(extract, readonly=True)
    for label, entry in key["patients"].items():
        token = {iso: t for t, iso in entry["dates"].items()}
        instruments = {form: [] for form in dic.forms}
        for instrument, insts in load_proposals(con, entry["study_id"]).items():
            if instrument not in instruments:
                continue
            for fields, _anchor in insts:
                out = {}
                for f, v in fields.items():
                    if not scored_field(dic, f):
                        continue
                    if dic[f].is_date:
                        out[f] = {"date": {"token": token[v[:10]], "year": int(v[:4]), "precision": "exact"}, "conf": "high"}
                    elif dic[f].is_checkbox:
                        out[f] = {"value": json.loads(v), "conf": "high"}
                    elif dic[f].is_coded:
                        out[f] = {"value": v, "conf": "high"}
                    else:
                        out[f] = {"value": v, "conf": "high", "kind": "text"}
                instruments[instrument].append({"fields": out})
        notes = sorted(int(n.split()[1]) for n in entry["notes"])
        (ref_dir / f"{label.replace(' ', '_')}.json").write_text(json.dumps(
            {"patient": label, "instruments": instruments, "fast_set": {"notes": notes[:3], "traps": notes[3:4]}}),
            encoding="utf-8")
    con.close()
    return {"deid": deid, "extract": extract, "reference": ref_dir, "key": res["key"], "keyd": key}


def _table(text: str, row: str) -> dict[str, str]:
    head = next(ln for ln in text.splitlines() if ln.startswith("| Instrument"))
    line = next(ln for ln in text.splitlines() if ln.startswith(f"| {row} |"))
    cells = lambda ln: [c.strip() for c in ln.strip("|").split("|")]  # noqa: E731
    return dict(zip(cells(head), cells(line), strict=True))


def test_a_reference_equal_to_the_proposals_scores_perfectly(scored):
    buf = io.StringIO()
    assert run_score(scored["extract"], scored["reference"], scored["key"], DICT, list_fields=True, stdout=buf) == 0
    out = buf.getvalue()
    allrow = _table(out, "**All**")
    assert int(allrow["Correct"]) > 50 and allrow["Wrong"] == allrow["Missed"] == allrow["Unsupported"] == "0", out
    assert allrow["Recall"] == allrow["Precision"] == "100%" and allrow["Ref."] == allrow["Paired"]
    assert "NMO-" not in out and "DOC-" not in out, "letters and field names only"
    buf2 = io.StringIO()
    assert run_score(scored["extract"], scored["reference"], scored["key"], DICT, stdout=buf2, back=5) == 1
    assert "nothing 5 run(s) back" in buf2.getvalue()
    import re

    assert not re.search(r"\d{4}-\d{2}-\d{2}", out), "no dates"


def test_wrong_missed_and_unsupported_are_counted_and_listed(scored):
    p = scored["reference"] / "Patient_A.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    inst = data["instruments"]
    rel = inst["relapse"]
    assert len(rel) >= 2, "the synthetic first patient has attacks"
    coded = next(f for f, v in rel[0]["fields"].items() if isinstance(v.get("value"), list))
    rel[0]["fields"][coded]["value"] = ["99"]                       # wrong
    dropped = rel.pop()                                             # the tool's last attack is now unsupported
    inst["imaging"].append({"fields": {"date_of_study": {"date": {"year": 1999, "precision": "year"}, "conf": "high"},
                                       "mri_brain_abnl": {"value": "1", "conf": "low"}}})   # missed instance
    p.write_text(json.dumps(data), encoding="utf-8")
    buf = io.StringIO()
    run_score(scored["extract"], scored["reference"], scored["key"], DICT, list_fields=True, stdout=buf)
    out = buf.getvalue()
    relrow = _table(out, "relapse")
    assert int(relrow["Wrong"]) == 1 and int(relrow["Unsupported"]) >= len(dropped["fields"])
    assert int(relrow["Proposed"]) == int(relrow["Ref."]) + 1
    img = _table(out, "imaging")
    assert int(img["Missed"]) >= 2 and int(img["Ref."]) == int(img["Paired"]) + 1
    assert f"Patient A · relapse #1 · #{load_dictionary(DICT)[coded].number} {coded}: wrong" in out
    assert "Patient A · imaging" in out and "mri_brain_abnl: missed" in out
    buf = io.StringIO()
    run_score(scored["extract"], scored["reference"], scored["key"], DICT, min_conf="medium", stdout=buf)
    assert int(_table(buf.getvalue(), "imaging")["Missed"]) == int(img["Missed"]) - 1, "low-confidence miss counted apart"


def test_fast_test_reads_only_the_fast_set_and_leaves_the_full_results_alone(scored, pipeline):
    ref = load_reference(scored["reference"])
    docs = fast_docs(ref, scored["keyd"])
    assert len(docs) == 4 * len(ref), "three notes and one trap per patient"
    before = scored["extract"].read_bytes()
    buf = io.StringIO()
    rc = run_fast_test(scored["deid"], scored["extract"], scored["reference"], scored["key"], pipeline["settings"],
                       llm="synthetic-oracle", annotations=pipeline["synthetic"] / "annotations.json", dictionary=DICT,
                       stdout=buf)
    out = buf.getvalue()
    assert rc == 0 and "Fast test: 20 notes" in out and "# Score against the reference answers" in out, out[-600:]
    assert scored["extract"].read_bytes() == before, "the full results are untouched"
    fast = fast_db(scored["extract"])
    con = sqlite3.connect(fast)
    read = {r[0] for r in con.execute("SELECT DISTINCT doc_id FROM done d JOIN runs r ON r.run_id = d.run_config_hash "
                                      "WHERE json_extract(r.config_json, '$.group') NOT LIKE 'edss%'")}
    con.close()
    assert read and read <= docs, "only the chosen notes were read (the disability groups pick their own)"
    allrow = _table(out, "**All**")
    assert int(allrow["Missed"]) > 0, "a fast set of a few notes cannot answer everything the whole chart does"
    # nothing to run the second time: the same model, prompts and rules
    buf = io.StringIO()
    run_fast_test(scored["deid"], scored["extract"], scored["reference"], scored["key"], pipeline["settings"],
                  llm="synthetic-oracle", annotations=pipeline["synthetic"] / "annotations.json", dictionary=DICT,
                  stdout=buf)
    assert "; 0 to run." in buf.getvalue()
