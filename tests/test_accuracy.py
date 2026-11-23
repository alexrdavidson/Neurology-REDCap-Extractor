"""18.11.26: accuracy tables from the reviewer's decisions (`nmosd accuracy`), for a paper. Synthetic data only.

Each field's proposal is compared with the reviewer's final value: correct, correctly empty, wrong value, filled
when it should be empty, missed; unresolved and not-reviewed fields are counted but left out of the rates. Episodes
(attacks, admissions, ...) are scored separately: confirmed, deleted (wrongly proposed), added (missed).
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, timedelta

import pytest
import yaml

from nmosd.cli import main as cli_main
from nmosd.eval.accuracy import collect, norm, outcome, run_accuracy, tables, wilson
from nmosd.review.store import GATE, ReviewStore
from tests.conftest import REPO

DICT = REPO / "config/redcap/data_dictionary.csv"
REVIEWER = "Synthetic Reviewer"


# ---------------------------------------------------------------------------------------------
# comparing values
# ---------------------------------------------------------------------------------------------
def test_values_are_compared_as_codes_dates_and_text():
    assert norm(None) is norm("") is norm("UNK") is norm("NA") is norm("[]") is norm("not_established") is None
    assert norm(" Massachusetts  General ") == norm("massachusetts general")
    assert norm('["2", "10", "1"]', checkbox=True) == ("1", "2", "10") and norm('["UNK"]', checkbox=True) is None
    assert norm("2", checkbox=True) == ("2",)
    assert norm("2019-3-5") == norm("2019-03-05") == "2019-03-05"
    assert norm("3") == "3" and norm("2019") == "2019"


@pytest.mark.parametrize("status, proposed, final, expected", [
    ("accepted", "3", "3", "correct"), ("corrected", "3", "3", "correct"), ("accepted", None, None, "correct_empty"),
    ("corrected", "3", "4", "wrong_value"), ("corrected", "3", None, "wrongly_filled"),
    ("corrected", None, "4", "missed"), ("unresolved", "3", "3", "unresolved"), ("pending", "3", "3", "not_reviewed")])
def test_outcomes(status, proposed, final, expected):
    assert outcome(status, proposed, final) == expected


def test_wilson_interval():
    assert wilson(0, 0) == (None, None)
    lo, hi = wilson(9, 10)
    assert round(lo, 3) == 0.596 and round(hi, 3) == 0.982
    assert wilson(10, 10)[1] == pytest.approx(1.0) and wilson(0, 10)[0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------------------------
# a review with known decisions
# ---------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def decided(pipeline, tmp_path_factory):
    """The diagnosis check accepted for every patient; on the first included patient's first attack one decision
    of each kind; its second attack deleted and one attack added."""
    review = tmp_path_factory.mktemp("acc") / "deid" / "review.sqlite"
    s = ReviewStore(review, pipeline["extract"], pipeline["deid"], DICT)
    try:
        for sid in s.patients():
            for fs in s.fields(sid, GATE, GATE):
                s.record(REVIEWER, "accept", sid, GATE, GATE, fs.field, reason="synthetic test")
        sid = next(p for p in s.patients() if s.gate(p).included)
        proposed = sum(len(s.instances(p, "relapse")) for p in s.patients() if s.gate(p).included)
        keys = s.instances(sid, "relapse")
        assert len(keys) >= 2, "the synthetic patient has several attacks"
        f = {fs.field: fs for fs in s.visible(s.fields(sid, "relapse", keys[0]))}
        dic = s.dictionary
        onset = date.fromisoformat(f["date_onset"].proposed)
        pheno = json.loads(f["relapse_phenotype"].proposed)
        extra = next(c for c in dic["relapse_phenotype"].choices if c not in pheno)
        empty = [n for n, fs in f.items() if fs.proposed is None and not dic[n].is_checkbox]
        s.record(REVIEWER, "accept", sid, "relapse", keys[0], "date_attack_estimate")                     # correct
        s.record(REVIEWER, "correct", sid, "relapse", keys[0], "date_onset",
                 new_value=(onset + timedelta(days=10)).isoformat())                                     # wrong, 10 d
        s.record(REVIEWER, "correct", sid, "relapse", keys[0], "relapse_phenotype",
                 new_value=json.dumps(sorted([*pheno, extra])))                                          # wrong, 1 code
        s.record(REVIEWER, "correct", sid, "relapse", keys[0], "dmt_relapse", new_value="[]")            # wrongly filled
        s.record(REVIEWER, "accept", sid, "relapse", keys[0], empty[0])                                  # correct empty
        miss = empty[1]
        s.record(REVIEWER, "correct", sid, "relapse", keys[0], miss,
                 new_value=next(iter(dic[miss].choices), "seen in clinic") if dic[miss].choices else "seen in clinic")
        s.record(REVIEWER, "unresolved", sid, "relapse", keys[0], empty[2])
        s.record(REVIEWER, "delete_instance", sid, "relapse", keys[1], reason="duplicate")
        s.add_instance(REVIEWER, sid, "relapse", reason="missed attack")
        return {"review": review, "sid": sid, "pheno": len(pheno), "proposed_attacks": proposed}
    finally:
        s.close()


def test_each_kind_of_decision_is_counted(pipeline, decided):
    s = ReviewStore(decided["review"], pipeline["extract"], pipeline["deid"], DICT)
    try:
        decisions, episodes, patients = collect(s)
        t = tables(s, decisions, episodes)
    finally:
        s.close()
    assert patients["included"] >= 1 and patients["excluded"] >= 1, "the MOGAD patient is excluded"
    rel = next(r for r in t["by_instrument"] if r["instrument"] == "relapse")
    assert (rel["correct"], rel["correct_empty"], rel["wrong_value"], rel["wrongly_filled"], rel["missed"],
            rel["unresolved"]) == (1, 1, 2, 1, 1, 1)
    assert rel["reviewed"] == 6 and rel["accuracy"] == pytest.approx(2 / 6)
    assert rel["precision"] == pytest.approx(1 / 4) and rel["recall"] == pytest.approx(1 / 4)
    assert rel["not_reviewed"] > 0, "every other field of the included patients is still pending"
    assert (rel["date_pairs"], rel["date_exact"], rel["date_within_30_days"], rel["date_median_error_days"]) == \
        (1, 0, 1, 10)
    ph = next(r for r in t["by_field"] if r["field"] == "relapse_phenotype")
    assert (ph["code_tp"], ph["code_fp"], ph["code_fn"]) == (decided["pheno"], 0, 1)
    dmt = next(r for r in t["by_field"] if r["field"] == "dmt_relapse")
    assert dmt["source"] == "derived" and dmt["wrongly_filled"] == 1
    ep = next(e for e in t["episodes"] if e["instrument"] == "relapse")
    assert (ep["confirmed"], ep["deleted"], ep["added"]) == (1, 1, 1)
    assert ep["precision"] == ep["recall"] == 0.5
    assert ep["proposed"] == decided["proposed_attacks"]
    gate = next(r for r in t["overall"] if r["group"].startswith("Diagnosis check"))
    assert gate["correct"] == gate["reviewed"] == 2 * sum(patients.values()), "both checks accepted for everyone"
    assert {d["source"] for d in decisions} >= {"gate", "notes", "derived"}


def test_report_and_tables_are_counts_only_and_each_run_is_a_snapshot(pipeline, decided, tmp_path):
    out = tmp_path / "work" / "reports" / "accuracy"
    buf = io.StringIO()
    assert run_accuracy(decided["review"], pipeline["extract"], pipeline["deid"], out, DICT, label="synthetic",
                        stdout=buf) == 0
    text = buf.getvalue()
    assert "# Extraction accuracy — synthetic" in text and "| Read from the notes (model) |" in text
    assert "## Episodes" in text and "## Dates" in text and "## Checkbox fields" in text
    assert "Model: synthetic-oracle" in text and "notes: de-identified" in text
    assert not re.search(r"NMO-|SYN-|DOC-|\b\d{4}-\d{2}-\d{2}\b|1000\d{5}|Synthetic Reviewer", text), "counts only"
    (folder,) = out.iterdir()
    names = {p.name for p in folder.iterdir()}
    assert names == {"accuracy_report.md", "accuracy_overall.csv", "accuracy_by_instrument.csv",
                     "accuracy_by_field.csv", "episodes.csv", "decisions_with_study_ids.csv"}
    for n in names - {"decisions_with_study_ids.csv"}:
        body = (folder / n).read_text(encoding="utf-8")
        assert not re.search(r"NMO-|SYN-|\b\d{4}-\d{2}-\d{2}\b", body), n
    rows = list(csv.DictReader((folder / "decisions_with_study_ids.csv").open(encoding="utf-8")))
    assert {r["study_id"] for r in rows} and all(r["study_id"].startswith("NMO-") for r in rows)
    assert not any(re.search(r"\d{4}-\d{2}-\d{2}", v or "") for r in rows for v in r.values()), "no values or dates"
    by_inst = {r["instrument"]: r for r in csv.DictReader((folder / "accuracy_by_instrument.csv").open(encoding="utf-8"))}
    assert by_inst["relapse"]["wrong_value"] == "2" and float(by_inst["relapse"]["accuracy"]) == pytest.approx(1 / 3,
                                                                                                             abs=1e-4)
    assert run_accuracy(decided["review"], pipeline["extract"], pipeline["deid"], out, DICT, stdout=io.StringIO()) == 0
    assert len(list(out.iterdir())) == 2, "a second run never overwrites the first"


def test_cli_and_nothing_to_measure(pipeline, decided, tmp_path, capsys):
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump({"paths": {}}), encoding="utf-8")
    common = ["--settings", str(settings), "accuracy", "--extract", str(pipeline["extract"]), "--deid",
              str(pipeline["deid"]), "--dictionary", str(DICT), "--out", str(tmp_path / "acc")]
    assert cli_main([*common, "--review-db", str(decided["review"])]) == 0
    assert "Saved in" in capsys.readouterr().out
    empty = tmp_path / "deid" / "empty_review.sqlite"
    ReviewStore(empty, pipeline["extract"], pipeline["deid"], DICT).close()
    assert cli_main([*common, "--review-db", str(empty)]) == 1
    assert "No review decisions yet" in capsys.readouterr().out
    assert cli_main([*common, "--review-db", str(tmp_path / "missing.sqlite")]) == 1
