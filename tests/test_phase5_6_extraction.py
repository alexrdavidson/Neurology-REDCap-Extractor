"""Phases 5 and 6 on synthetic data.

Phase 5 accept: >= 90% of evidence quotes verify; "plan to start rituximab" is never emitted as a start.
Phase 6 accept: relapse counts match the gold standard for all 5 fake patients; treatment episodes match.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections import defaultdict
from datetime import date

import pytest

from nmosd.deid.date_shift import Offsets, shift_by_precision
from nmosd.extract.chunking import find_sections, select_view, split_view
from nmosd.extract.ollama_client import LLMResult
from nmosd.extract.prefilter import is_candidate
from nmosd.extract.quote_verify import verify_quote
from nmosd.extract.run import default_groups, run_extraction
from nmosd.extract.stage1_evidence import normalise_date
from nmosd.extract.stage2_reconcile import (
    Item,
    reconcile_relapses,
    reconcile_structured,
    reconcile_treatments,
    run_reconcile,
)
from nmosd.extract.synthetic_oracle import SyntheticOracleClient
from nmosd.redcap.schemas import STRUCTURED_ONLY
from nmosd.textproc.view import TextView


def _con(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


# =============================================================================================
# Phase 5 units
# =============================================================================================
def test_sections_and_selection(pipeline):
    con = _con(pipeline["deid"])
    text = con.execute("SELECT text FROM documents WHERE report_description='Office Visit' LIMIT 1 OFFSET 5").fetchone()[0]
    con.close()
    types = [s.stype for s in find_sections(text)]
    for t in ["dx_summary", "tx_summary", "hpi", "medications", "exam", "data", "assessment_plan"]:
        assert t in types, t
    # 18.11.26: a note of at most `whole_note_under` characters is read whole; section rules apply to longer notes
    assert len(text) <= 4000 and select_view(TextView.full(text), "treatment").text == text
    text = text + "\r\n" + "Follow-up as scheduled; call with any new symptoms.\r\n" * 60
    assert len(text) > 4000
    tx = select_view(TextView.full(text), "treatment").text
    assert "Tx:" in tx and "Current medication regimen" in tx and "ASSESSMENT & PLAN" in tx
    assert "Neurologic examination" not in tx
    rel = select_view(TextView.full(text), "relapse").text
    assert "Dx:" in rel and "HPI" in rel and "Reflexes" not in rel


def test_split_view_respects_budget_and_offsets(pipeline):
    con = _con(pipeline["deid"])
    text = max((r[0] for r in con.execute("SELECT text FROM documents")), key=len)
    con.close()
    chunks = split_view(TextView.full(text), 1200, 200)
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c.text) <= 1200
        assert c.text == "".join(text[a:b] for a, b in c.pieces)
    assert chunks[1].pieces[0][0] < chunks[0].pieces[-1][1], "overlap between consecutive chunks"


def test_quote_verification():
    v = TextView.full("Plan:\r\nShe started   rituximab on 9/20/2019 at the infusion center.")
    m = verify_quote("she started rituximab on 9/20/2019", v)
    assert m.verified and m.method == "exact" and v.original[m.start:m.end].startswith("She started")
    f = verify_quote("She started rituximab on 9/20/2019 at the infusion centre", v)
    assert f.verified and f.method == "fuzzy"
    assert not verify_quote("He stopped eculizumab in 2015", v).verified


@pytest.mark.parametrize("value,est,expected", [
    ("2019-03-04", "1", ("2019-03-04", "1")), ("2019-03", "1", ("2019-03-15", "2")), ("2019", "not_established", ("2019-07-01", "3")),
    ("not_established", "1", ("not_established", "not_established")), ("2019-02-30", "1", ("not_established", "not_established")),
])
def test_normalise_date(value, est, expected):
    assert normalise_date(value, est) == expected


# =============================================================================================
# Phase 5 acceptance
# =============================================================================================
def test_plan_summary_is_counts_only(pipeline):
    out = pipeline["extract_stdout"]
    assert "After copy-forward removal" in out and "| treatment |" in out
    con = _con(pipeline["deid"])
    sample = con.execute("SELECT text FROM documents LIMIT 1").fetchone()[0]
    con.close()
    for line in sample.splitlines()[3:10]:
        if len(line) > 25:
            assert line not in out


def test_quotes_verify(pipeline):
    con = _con(pipeline["extract"])
    n, v = con.execute("SELECT COUNT(*), SUM(verified) FROM (SELECT item_id, MAX(verified) verified FROM evidence "
                       "GROUP BY item_id)").fetchone()
    con.close()
    assert n > 200 and v / n >= 0.90


def _start_evidence(con, study_id, drug_code):
    return con.execute(
        "SELECT COUNT(*) FROM evidence e WHERE study_id=? AND field_group='treatment' AND field='event' AND value='start' "
        "AND item_id IN (SELECT item_id FROM evidence WHERE field='maintenance_therapy' AND value=?)",
        (study_id, drug_code)).fetchone()[0]


def _sid(pipeline, empi):
    return next(k for k, v in pipeline["link"]["patients"].items() if v["empi"] == empi)


def test_plan_to_start_is_not_a_start(pipeline):
    con = _con(pipeline["extract"])
    p3 = _sid(pipeline, "100002286")
    assert _start_evidence(con, p3, "1") == 0, "P3 only ever had rituximab planned"
    planned = con.execute("SELECT COUNT(*) FROM evidence WHERE study_id=? AND field='event' AND value='planned'", (p3,)).fetchone()[0]
    assert planned >= 1
    con.close()


def test_plan_guard_against_careless_model(pipeline, tmp_path):
    client = SyntheticOracleClient(pipeline["synthetic"] / "annotations.json", mislabel_plans=True)
    out = tmp_path / "deid" / "extract.sqlite"
    buf = io.StringIO()
    run_extraction(pipeline["deid"], out, pipeline["settings"], groups=["treatment"], client=client, stdout=buf)
    assert "plans demoted from start/stop" in buf.getvalue()
    con = _con(out)
    p3 = _sid(pipeline, "100002286")
    assert _start_evidence(con, p3, "1") == 0
    con.close()
    run_reconcile(out, pipeline["deid"], pipeline["settings"], stdout=io.StringIO())
    con = _con(out)
    drugs = [r[0] for r in con.execute("SELECT value FROM proposals WHERE study_id=? AND field='maintenance_therapy'", (p3,))]
    assert "1" not in drugs
    con.close()


def test_resumable_without_duplicates(pipeline, tmp_path):
    out = tmp_path / "deid" / "resume.sqlite"
    s = pipeline["settings"]
    ann = pipeline["synthetic"] / "annotations.json"
    run_extraction(pipeline["deid"], out, s, llm="synthetic-oracle", annotations=ann, limit_docs=10, stdout=io.StringIO())
    con = _con(out)
    assert con.execute("SELECT COUNT(*) FROM done").fetchone()[0] == 10
    con.close()
    buf = io.StringIO()
    client = SyntheticOracleClient(ann)
    run_extraction(pipeline["deid"], out, s, client=client, stdout=buf)
    assert "10 (document, group) pairs already done" in buf.getvalue()
    c1, c2 = _con(out), _con(pipeline["extract"])
    sig = "SELECT doc_id, field_group, field, value, event_date FROM evidence ORDER BY 1,2,3,4,5"
    assert [tuple(r) for r in c1.execute(sig)] == [tuple(r) for r in c2.execute(sig)]
    c1.close()
    c2.close()
    client2 = SyntheticOracleClient(ann)
    buf = io.StringIO()
    run_extraction(pipeline["deid"], out, s, client=client2, stdout=buf)
    assert client2.calls == 0 and " 0 to run" in buf.getvalue()


def test_preview_says_how_much_is_left(pipeline, tmp_path):
    """The dry run (step 7's preview) tells whether the model needs to run again, per field group (counts only)."""
    s, ann = pipeline["settings"], pipeline["synthetic"] / "annotations.json"
    out = tmp_path / "deid" / "preview.sqlite"

    def preview() -> str:
        buf = io.StringIO()
        client = SyntheticOracleClient(ann)
        assert run_extraction(pipeline["deid"], out, s, client=client, dry_run=True, stdout=buf) == 0
        assert client.calls == 0
        return buf.getvalue()

    assert "Nothing has been extracted yet" in preview() and not out.exists()
    run_extraction(pipeline["deid"], out, s, client=SyntheticOracleClient(ann), limit_docs=10, stdout=io.StringIO())
    text = preview()
    rows = {r[0]: (int(r[1]), int(r[2])) for r in re.findall(r"^\| (\w+) \| (\d+) \| (\d+) \|$", text, re.M)}
    # de-identified notes with hospital names tokenised: the `hospital` group is not read (18.11.26)
    assert set(rows) == set(default_groups()) - {"hospital"} and sum(d for d, _ in rows.values()) == 10
    assert "Not reading hospital" in text
    left = sum(t for _, t in rows.values())
    assert f"**{left} (document, group) pairs to run.**" in text and "does not need to run" not in text
    run_extraction(pipeline["deid"], out, s, client=SyntheticOracleClient(ann), stdout=io.StringIO())
    assert "**0 (document, group) pairs to run.** The model does not need to run" in preview()


def test_llm_calls_are_recorded(pipeline):
    con = _con(pipeline["extract"])
    rec = json.loads(con.execute("SELECT record_json FROM llm_calls LIMIT 1").fetchone()[0])
    assert {"model", "prompt_eval_count", "eval_count", "truncation_suspected", "options"} <= set(rec)
    assert con.execute("SELECT COUNT(*) FROM paragraph_repeats").fetchone()[0] > 100
    con.close()


def test_small_context_forces_chunking(pipeline, tmp_path):
    client = SyntheticOracleClient(pipeline["synthetic"] / "annotations.json")
    client.num_ctx = 2700
    out = tmp_path / "deid" / "small.sqlite"
    run_extraction(pipeline["deid"], out, pipeline["settings"], groups=["treatment"], client=client, stdout=io.StringIO())
    con = _con(out)
    assert con.execute("SELECT MAX(seq) FROM chunks").fetchone()[0] >= 2
    n, v = con.execute("SELECT COUNT(*), SUM(verified) FROM (SELECT item_id, MAX(verified) verified FROM evidence "
                       "GROUP BY item_id)").fetchone()
    assert v / n >= 0.9
    con.close()


# =============================================================================================
# Phase 6 acceptance: compare proposals with the gold standard in shifted (de-identified) space
# =============================================================================================
def _gold(pipeline):
    rows = list(csv.DictReader((pipeline["synthetic"] / "gold_redcap.csv").open(encoding="utf-8")))
    rmap = dict(csv.reader((pipeline["synthetic"] / "record_map.csv").open(encoding="utf-8")))
    by_rid = defaultdict(list)
    for r in rows:
        by_rid[r["record_id"]].append(r)
    return by_rid, rmap


def _proposals(pipeline, sid, instrument):
    con = _con(pipeline["extract"])
    run = con.execute("SELECT MAX(run_id) FROM proposals").fetchone()[0]
    out = defaultdict(dict)
    for r in con.execute("SELECT instance_key, field, value, conflict_note FROM proposals WHERE run_id=? AND study_id=? "
                         "AND instrument=?", (run, sid, instrument)):
        out[r["instance_key"]][r["field"]] = r["value"]
        out[r["instance_key"]]["_conflict"] = r["conflict_note"]
    con.close()
    return list(out.values())


def _same_date(pred: str | None, gold: str, est: str, off: Offsets) -> bool:
    if not pred or not gold:
        return False
    g = shift_by_precision(date.fromisoformat(gold), off, est)
    p = date.fromisoformat(pred)
    if est == "3":
        return p.year == g.year
    if est == "2":
        return (p.year, p.month) == (g.year, g.month) or abs((p - g).days) <= 31
    return abs((p - g).days) <= 1


def test_relapse_counts_and_dates_match_gold(pipeline):
    gold, rmap = _gold(pipeline)
    for sid, p in pipeline["link"]["patients"].items():
        off = Offsets(p["offset_days"])
        g = sorted([r for r in gold[rmap[p["empi"]]] if r["redcap_repeat_instrument"] == "relapse"], key=lambda r: r["date_onset"])
        pr = sorted(_proposals(pipeline, sid, "relapse"), key=lambda r: r["date_onset"])
        assert len(pr) == len(g), (rmap[p["empi"]], len(pr), len(g))
        for a, b in zip(pr, g, strict=True):
            assert _same_date(a["date_onset"], b["date_onset"], b["date_attack_estimate"], off), (a, b)
            gp = {c for c in range(1, 8) if b[f"relapse_phenotype___{c}"] == "1"}
            assert gp <= {int(x) for x in json.loads(a["relapse_phenotype"])}
            if b["admission"] == "1" and b["date_admission"]:
                assert a["admission"] == "1"
                assert _same_date(a["date_admission"], b["date_admission"], "1", off)
                assert _same_date(a["date_discharge"], b["date_discharge"], "1", off)


def test_treatment_episodes_match_gold(pipeline):
    gold, rmap = _gold(pipeline)
    for sid, p in pipeline["link"]["patients"].items():
        off = Offsets(p["offset_days"])
        g = sorted([r for r in gold[rmap[p["empi"]]] if r["redcap_repeat_instrument"] == "treatment"],
                   key=lambda r: r["date_starting_maint"])
        pr = sorted(_proposals(pipeline, sid, "treatment"), key=lambda r: r["date_starting_maint"] or "9999")
        assert [x["maintenance_therapy"] for x in pr] == [x["maintenance_therapy"] for x in g], rmap[p["empi"]]
        for a, b in zip(pr, g, strict=True):
            assert _same_date(a["date_starting_maint"], b["date_starting_maint"], b["date_maintenance_estimate"], off), (a, b)
            assert a["still_on_therapy"] == b["still_on_therapy"], (a, b)
            if b["date_stopping_maint"]:
                assert _same_date(a["date_stopping_maint"], b["date_stopping_maint"], b["date_stopping_estimate"], off), (a, b)
                assert a["reason_for_stopping_tx"] == b["reason_for_stopping_tx"], (a, b)
                assert not a["_conflict"] or "overlap" in a["_conflict"], a["_conflict"]


def test_antibodies_and_single_value_fields(pipeline):
    gold, rmap = _gold(pipeline)
    for sid, p in pipeline["link"]["patients"].items():
        off = Offsets(p["offset_days"])
        rows = gold[rmap[p["empi"]]]
        g_ab = [r for r in rows if r["redcap_repeat_instrument"] == "antibody_lab_data"]
        pr_ab = _proposals(pipeline, sid, "antibody_lab_data")
        assert sorted(x["type_test"] for x in pr_ab) == sorted(x["type_test"] for x in g_ab)
        for g in g_ab:
            match = [x for x in pr_ab if x["type_test"] == g["type_test"]]
            if g["type_test"] == "aqp4":
                assert any(x["aqp4_assay_status"] == g["aqp4_assay_status"] and
                           _same_date(x["aqp4_assay_date"], g["aqp4_assay_date"], "1", off) for x in match)
            else:
                assert any(x["mog_test_positive"] == g["mog_test_positive"] for x in match)
        demo = _proposals(pipeline, sid, "demographics")[0]
        gd = next(r for r in rows if not r["redcap_repeat_instrument"])
        assert demo["sex"] == gd["sex"] and demo["living_deceased"] == gd["living_deceased"]
        assert _same_date(demo["date_nmo_diagnosis"], gd["date_nmo_diagnosis"], gd["date_nmo_estimate"], off)
        if gd["otherdx"] != "5":
            assert demo["otherdx"] == gd["otherdx"]
            if gd["dx_other"]:
                assert demo["dx_other"] == gd["dx_other"]


def test_instance_keys_are_stable_across_runs(pipeline):
    run_reconcile(pipeline["extract"], pipeline["deid"], pipeline["settings"], stdout=io.StringIO())
    con = _con(pipeline["extract"])
    runs = [r[0] for r in con.execute("SELECT DISTINCT run_id FROM proposals ORDER BY run_id")]
    keys = [{(r[0], r[1]) for r in con.execute("SELECT study_id, instance_key FROM proposals WHERE run_id=?", (run,))}
            for run in runs[-2:]]
    con.close()
    assert len(runs) >= 2 and keys[0] == keys[1]


# --- reconciliation rules on hand-made evidence ------------------------------------------------
def _item(i, group, fields, d=None, est="1", doc="D1", pos=0, doc_date=None, hyp=False, neg=False):
    it = Item(f"i{i}", group, doc, doc_date or d, pos, dict(fields), d, est, neg, hyp, [f"e{i}"])
    return it


def test_relapse_clustering_and_conflicts():
    items = [
        _item(1, "relapse", {"date_onset": "2018-03-04", "relapse_phenotype": ["2"]}, "2018-03-04", "1"),
        _item(2, "relapse", {"date_onset": "2018-03-15", "relapse_phenotype": ["2"]}, "2018-03-15", "2"),
        _item(3, "relapse", {"date_onset": "2018-07-01", "relapse_phenotype": ["1"]}, "2018-07-01", "3"),
        _item(4, "relapse", {"date_onset": "2020-11-20", "relapse_phenotype": ["1"]}, "2020-11-20", "1"),
        _item(5, "relapse", {"date_onset": "2020-11-22", "relapse_phenotype": ["3"]}, "2020-11-22", "1"),
        _item(6, "relapse", {"admission": "1", "date_admission": "2018-03-05", "date_discharge": "2018-03-12"}, None),
        _item(7, "relapse", {"date_onset": "2021-06-01"}, "2021-06-01", "1", neg=True),
    ]
    eps = reconcile_relapses(items, 30, 120)
    assert [e["values"]["date_onset"] for e in eps] == ["2018-03-04", "2020-11-20"]
    assert eps[0]["values"]["admission"] == "1" and eps[0]["values"]["date_discharge"] == "2018-03-12"
    assert "phenotypes differ" in eps[0]["conflict"], "the low-precision ON mention merged into the TM episode"
    assert eps[1]["values"]["relapse_phenotype"] == ["1", "3"] and "phenotypes differ" in eps[1]["conflict"]


def test_treatment_episodes_switches_and_plans():
    items = [
        _item(1, "treatment", {"maintenance_therapy": "1", "event": "planned"}, None, hyp=True, doc_date="2018-03-01"),
        _item(2, "treatment", {"maintenance_therapy": "1", "event": "start"}, "2018-04-16", "1"),
        _item(3, "treatment", {"maintenance_therapy": "1", "event": "start"}, "2018-04-15", "2"),
        _item(4, "treatment", {"maintenance_therapy": "1", "event": "stop", "reason_for_stopping_tx": "2"}, "2021-10-18"),
        _item(5, "treatment", {"maintenance_therapy": "5", "event": "start"}, "2021-09-01"),
        _item(6, "treatment", {"maintenance_therapy": "5", "event": "ongoing"}, None, doc_date="2023-01-01"),
        _item(7, "treatment", {"maintenance_therapy": "2", "event": "start"}, "2019-01-01", neg=True),
    ]
    eps = reconcile_treatments(items)
    assert [e["values"]["maintenance_therapy"] for e in eps] == ["1", "5"]
    rtx, ineb = eps
    assert rtx["values"]["date_starting_maint"] == "2018-04-16" and rtx["values"]["still_on_therapy"] == "0"
    assert rtx["values"]["reason_for_stopping_tx"] == "2" and "overlaps with drug 5" in rtx["conflict"]
    assert ineb["values"]["still_on_therapy"] == "1"
    structured = reconcile_treatments(items, {"5": [date(2021, 8, 30)]})
    assert structured[1]["values"]["date_starting_maint"] == "2021-08-30", "structured administration wins"


def _demo(i, fields, neg=False, hyp=False, quote=None):
    it = Item(f"i{i}", "demographics", "D1", None, 0, dict(fields), negated=neg, hypothetical=hyp, quote=quote)
    it.field_evidence = {f: f"e{i}:{f}" for f in fields}
    it.evidence_ids = list(it.field_evidence.values())
    return it


NOTE_CHECKED = frozenset({"ethnicity"})   # the 13.11.26 setting: sex, race and living/deceased checked in the notes


def test_structured_demographics_are_checked_against_the_notes():
    """The note check, for fields with `note_check: true` (off for all four since 16.11.26)."""
    items = [_demo(1, {"sex": "2"}), _demo(2, {"sex": "2", "race": "1", "ethnicity": "1"}), _demo(3, {"sex": "1"}),
             _demo(4, {"race": "2"}, neg=True), _demo(5, {"living_deceased": "2"}),
             _demo(6, {"race": "UNK"}), Item("i7", "diagnosis", "D1", None, 0, {"otherdx": "1"})]
    out = reconcile_structured(items, {"sex": "2", "ethnicity": "2", "living_deceased": "1"},
                               structured_only=NOTE_CHECKED)
    # the structured value stays the proposal; evidence is that field's own mentions only
    assert out["sex"]["value"] == "2" and out["sex"]["evidence"] == ["e1:sex", "e2:sex", "e3:sex"]
    assert out["sex"]["conflict"] == "sex: notes say 1 (1 mention), structured data says 2"
    # ethnicity is structured only (13.11.26): a note mention is neither evidence nor a conflict
    assert out["ethnicity"] == {"value": "2", "evidence": [], "conflict": ""}
    # no usable structured value: proposed from the notes (negated mentions ignored), and said so
    assert out["race"]["value"] == "1" and out["race"]["evidence"] == ["e2:race"]
    assert "race: no usable structured value; proposed from the notes" in out["race"]["conflict"]
    # a death in the notes that the structured record does not show
    assert out["living_deceased"]["value"] == "1"
    assert "notes say 2 (1 mention), structured data says 1" in out["living_deceased"]["conflict"]
    empty = reconcile_structured([], {}, structured_only=NOTE_CHECKED)
    assert all(v["value"] is None and v["evidence"] == [] and "no usable structured value" in v["conflict"]
               for v in empty.values())
    assert empty["ethnicity"]["conflict"] == "ethnicity: no usable structured value", "never proposed from notes"
    split = reconcile_structured([_demo(1, {"race": "1"}), _demo(2, {"race": "2"})], {}, structured_only=NOTE_CHECKED)
    assert "race: notes disagree (1 (1 mention), 2 (1 mention))" in split["race"]["conflict"]


def test_demographic_mentions_need_the_fields_own_wording():
    """Regression (13.11.26 review): a race 'mention' quoting a medication list was shown as race evidence."""
    items = [_demo(1, {"race": "5"}, quote="calcium carbonate (500 mg elemental) tablet"),
             _demo(2, {"race": "1"}, quote="Race: White"),
             _demo(3, {"race": "1"}, quote="45-year-old white woman with NMOSD"),
             _demo(4, {"sex": "2"}, quote="She reports fatigue."),
             _demo(5, {"sex": "2"}, quote="36-year-old woman"),
             _demo(6, {"race": "4"}, quote="")]
    out = reconcile_structured(items, {"race": "1", "sex": "2"}, structured_only=NOTE_CHECKED)
    assert out["race"]["evidence"] == ["e2:race", "e3:race"] and out["race"]["conflict"] == ""
    assert out["sex"]["evidence"] == ["e5:sex"], "a pronoun alone is not evidence of sex"


def test_demographics_come_from_the_table_only():
    """16.11.26: sex, race, ethnicity and living/deceased are taken from the RPDR Demographics table alone."""
    assert {"sex", "race", "ethnicity", "living_deceased"} == STRUCTURED_ONLY
    items = [_demo(1, {"sex": "1", "race": "2"}, quote="45-year-old Black man"), _demo(2, {"living_deceased": "2"})]
    out = reconcile_structured(items, {"sex": "2", "race": "1", "ethnicity": "2", "living_deceased": "1"})
    assert all(v["evidence"] == [] and v["conflict"] == "" for v in out.values()), "notes are neither evidence nor conflict"
    assert {f: v["value"] for f, v in out.items()} == {"sex": "2", "race": "1", "ethnicity": "2", "living_deceased": "1"}
    # a fallback column or a missing value is flagged for the reviewer, never filled in from the notes
    out = reconcile_structured(items, {"sex": "2"}, {"sex": "sex: taken from Gender_Legal_Sex 'Female' (Sex_At_Birth is empty)"})
    assert out["sex"] == {"value": "2", "evidence": [], "conflict": "sex: taken from Gender_Legal_Sex 'Female' "
                                                                    "(Sex_At_Birth is empty)"}
    assert out["race"]["value"] is None and out["race"]["conflict"] == "race: no usable structured value"
    # the extraction no longer asks the model about demographics, unless asked to
    assert "demographics" not in default_groups()
    assert {"diagnosis", "relapse", "treatment", "antibody", "followup", "comorbidity"} <= set(default_groups())


def test_demographics_prefilter_wants_a_statement_not_any_word():
    hit = lambda t: is_candidate("demographics", "clinical_note", t, set())[0]  # noqa: E731
    assert hit("She is a 36-year-old woman with NMOSD.") and hit("45 yo F with optic neuritis")
    assert hit("Self-reported race: Black or African American")
    assert not hit("Ethnicity: Hispanic"), "ethnicity is structured only: not looked for in the notes"
    assert hit("The patient passed away on [DATE].")
    assert not hit("MRI shows scattered white matter lesions. Plan: continue rituximab.")
    assert not is_candidate("demographics", "low_value", "36-year-old woman", set())[0]


class _OllamaGoesAway(SyntheticOracleClient):
    """Answers the first `ok` calls, then fails like a stopped Ollama (connection refused)."""

    def __init__(self, annotations, ok: int):
        super().__init__(annotations)
        self.ok = ok

    def generate_json(self, system, prompt, schema):
        if self.calls >= self.ok:
            self.calls += 1
            return LLMResult(content="", parsed=None, model=self.model, digest=None, options={},
                             error="ConnectError: [Errno 61] Connection refused")
        return super().generate_json(system, prompt, schema)


def test_unanswered_documents_are_retried_not_marked_done(pipeline, tmp_path):
    """If Ollama stops mid-run, the run stops after a few failures and the next run picks up every unanswered pair."""
    out = tmp_path / "deid" / "gone.sqlite"
    s, ann = pipeline["settings"], pipeline["synthetic"] / "annotations.json"
    buf = io.StringIO()
    rc = run_extraction(pipeline["deid"], out, s, client=_OllamaGoesAway(ann, ok=5), stdout=buf)
    text = buf.getvalue()
    assert rc == 1 and "Stopped: the model did not answer 3 documents in a row" in text
    assert "no answer from the model; left for the next run" in text
    con = _con(out)
    n_done = con.execute("SELECT COUNT(*) FROM done").fetchone()[0]
    # groups read with text patterns make no model call, so only the other groups' pairs count against ok=5
    from nmosd.extract.run import REGEX_READERS

    marks = ",".join("?" * len(REGEX_READERS))
    n_model = con.execute(f"SELECT COUNT(*) FROM done WHERE field_group NOT IN ({marks})", list(REGEX_READERS)).fetchone()[0]
    con.close()
    assert 0 < n_model <= 5
    client = SyntheticOracleClient(ann)
    buf = io.StringIO()
    assert run_extraction(pipeline["deid"], out, s, client=client, stdout=buf) == 0
    assert f"{n_done} (document, group) pairs already done" in buf.getvalue()
    c1, c2 = _con(out), _con(pipeline["extract"])
    sig = "SELECT doc_id, field_group, field, value, event_date FROM evidence ORDER BY 1,2,3,4,5"
    assert [tuple(r) for r in c1.execute(sig)] == [tuple(r) for r in c2.execute(sig)]
    c1.close()
    c2.close()


def test_a_single_unanswered_document_does_not_stop_the_run(pipeline, tmp_path):
    class _OneFailure(SyntheticOracleClient):
        def generate_json(self, system, prompt, schema):
            self.calls += 1
            if self.calls == 2:
                return LLMResult(content="", parsed=None, model=self.model, digest=None, options={}, error="ReadTimeout")
            self.calls -= 1
            return super().generate_json(system, prompt, schema)

    out = tmp_path / "deid" / "one.sqlite"
    buf = io.StringIO()
    rc = run_extraction(pipeline["deid"], out, pipeline["settings"], groups=["treatment"],
                        client=_OneFailure(pipeline["synthetic"] / "annotations.json"), stdout=buf)
    text = buf.getvalue()
    assert rc == 1 and "Stopped" not in text and "left for the next run 1." in text
    assert "Run the same command again" in text
