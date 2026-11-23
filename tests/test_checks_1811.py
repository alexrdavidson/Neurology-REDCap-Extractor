"""Code checks on the extraction (18.11.26), from the pilot's reference review: fixed rules, no model.

* a treatment mention must name its drug in the quote or close to it in the note; a drug no passage names is still
  proposed but marked; unnamed mentions are left out when others name the drug;
* the end of one infusion ("stop" on the day of the note that also starts the drug that day) does not end the course;
* a start date taken from an administration note that does not speak of starting is marked;
* keywords match from the start of a word.
Synthetic text only.
"""

from __future__ import annotations

import re

from nmosd.extract.checks import drug_named, mark_unnamed, names_drug, says_first_dose
from nmosd.extract.chunking import keyword_windows, select_view
from nmosd.extract.prefilter import group_specs, keyword_rx
from nmosd.extract.stage2_reconcile import Item, reconcile_treatments
from nmosd.textproc.view import TextView

INFUSION_NOTE = ("Dx: Neuromyelitis Optica\n\nMed: Ravulizumab (Ultomiris)\n\nFrequency: every 8 weeks\n\n"
                 "Dose: 3,000 mg\n\nPt arrived feeling well. Infused per protocol over 1 hour. Infusion stopped at 14:05.")


def _tx(n, drug, event, d=None, *, doc=None, doc_date=None, quote="q", pos=0, est="1", other=None):
    fields = {"maintenance_therapy": drug, "event": event}
    if other:
        fields["other_maintenance_tx"] = other
    return Item(f"t{n}", "treatment", doc or f"D{n}", doc_date or d, pos, fields, d, est if d else None, False, False,
                [f"e{n}"], {}, quote)


def test_names_drug_whole_words_and_brands():
    assert names_drug("Continue Ultomiris 3,000 mg", "4") and names_drug("on rituximab-abbs", "1")
    assert names_drug("MMF 1 g BID", "2") and not names_drug("COMMFORT measures", "2"), "an abbreviation stands alone"
    assert names_drug("high-dose steroids", "16") and names_drug("Cytoxan #2 of 6", "14")
    assert not names_drug("Continue duloxetine 60 mg daily", "7"), "azathioprine is not named"
    assert not names_drug("Azithromycin prophylaxis per protocol", "2")
    assert not names_drug("mentioned Rituxan in passing", "12"), "natalizumab is not Rituxan"
    assert names_drug("started leflunomide", "15", "Leflunomide") and not names_drug("started a new drug", "15", "Leflunomide")
    assert not names_drug("anything", "15", None)


def test_drug_named_looks_close_to_the_quote_in_the_note():
    start = INFUSION_NOTE.index("Dose: 3,000 mg")
    assert drug_named("4", "Dose: 3,000 mg", INFUSION_NOTE, start, start + 14), "'Med:' line two lines above"
    unnamed_note = "Dx: Neuromyelitis Optica\nDose: 300 mg\nFrequency: Every 26 weeks\nPre-meds: PO Tylenol 650mg"
    s = unnamed_note.index("Dose")
    assert not drug_named("1", "Dose: 300 mg", unnamed_note, s, s + 12), "no drug named: rituximab was a guess"
    far = "rituximab was discussed last year. " + "x " * 300 + "Dose: 300 mg"
    assert not drug_named("1", "Dose: 300 mg", far, far.index("Dose"), len(far)), "too far away to count"


def test_invented_drug_is_marked_and_unnamed_mentions_are_left_out():
    items = [_tx(1, "7", "ongoing", doc_date="2024-03-01", quote="Continue duloxetine 60 mg daily"),
             _tx(2, "4", "start", "2024-03-01", quote="Ultomiris loading dose given today"),
             _tx(3, "4", "ongoing", doc_date="2024-06-01", quote="Dose: 3,000 mg"),          # named nearby
             _tx(4, "4", "ongoing", doc_date="2024-07-01", quote="continue current regimen")]  # not named
    texts = {"D1": "Plan: Continue duloxetine 60 mg daily for neuropathic pain.", "D3": INFUSION_NOTE,
             "D4": "Assessment: stable. continue current regimen."}
    items[2].char_start = INFUSION_NOTE.index("Dose: 3,000 mg")
    items[0].char_start, items[3].char_start = 6, texts["D4"].index("continue")
    assert mark_unnamed(items, texts) == 2 and [i.unnamed for i in items] == [True, False, False, True]
    by = {e["drug"]: e for e in reconcile_treatments(items)}
    assert "none of the quoted passages names this drug" in by["7"]["conflict"], "proposed, but marked"
    assert "names this drug" not in by["4"]["conflict"]
    assert any("1 mention(s) left out" in x for x in by["4"]["info"])
    assert by["4"]["values"]["date_starting_maint"] == "2024-03-01" and by["4"]["values"]["still_on_therapy"] == "1"


def test_end_of_an_infusion_is_not_the_end_of_the_course():
    days = ["2025-01-10", "2025-03-07", "2025-05-02"]
    items = []
    for n, d in enumerate(days):
        items += [_tx(2 * n, "4", "start", d, doc=f"N{n}", quote="Med: Ravulizumab (Ultomiris)"),
                  _tx(2 * n + 1, "4", "stop", d, doc=f"N{n}", quote="Ultomiris infusion stopped at 14:05")]
    (ep,) = reconcile_treatments(items)
    v = ep["values"]
    assert v["still_on_therapy"] == "1" and v.get("date_stopping_maint") is None
    assert v["date_starting_maint"] == days[0]
    assert any("merged into one course" in x for x in ep["info"])
    assert any("end of that day's infusion" in x for x in ep["info"])
    assert "may have begun earlier" in ep["conflict"], "an administration note is not a start: marked"


def test_a_real_stop_in_a_clinic_note_is_kept_and_a_stated_start_is_not_marked():
    items = [_tx(1, "1", "start", "2019-04-02", doc="C1", doc_date="2019-04-02", quote="Started rituximab today"),
             _tx(2, "1", "stop", "2021-06-15", doc="C2", doc_date="2021-06-15", quote="rituximab stopped today", est="1"),
             _tx(3, "1", "ongoing", doc="C2", doc_date="2021-06-15", quote="had been on rituximab since 2019")]
    (ep,) = reconcile_treatments(items)
    v = ep["values"]
    assert v["still_on_therapy"] == "0" and v["date_stopping_maint"] == "2021-06-15"
    assert "may have begun earlier" not in ep["conflict"] and not any("infusion" in x for x in ep["info"])
    assert says_first_dose("first infusion of Uplizna") and says_first_dose("loading dose") and says_first_dose("dose #1")
    assert not says_first_dose("Med: Ravulizumab (Ultomiris)") and not says_first_dose("maintenance dose #3 given")


def test_keywords_match_from_the_start_of_a_word():
    inf = keyword_rx("infection")
    assert not inf.search("reciprocal gait orthosis") and inf.search("Cipro 500 mg BID") and inf.search("ciprofloxacin")
    bg = keyword_rx("background")
    assert bg.search("ambulatory with a walker") and bg.search("Ambulation: independent")
    assert keyword_rx("diagnosis").search("diagnosis of NMOSD") and keyword_rx("diagnosis").search("the MS clinic")
    assert keyword_rx("edss_vision_brainstem").search("VA 20/40 OD"), "a keyword that starts with a symbol is unchanged"
    assert keyword_rx("comorbidity").search("history of diabetes"), "stems still match whole words"


# ---- how much of a note a group reads (18.11.26) -----------------------------------------------------------
ADMIN_NOTE = ("Infusion Center Visit\n\nPatient arrived ambulatory. Vitals stable.\n\nLab Results\n\n"
              "Administrations This Visit\n\nravulizumab-cwvz (ULTOMIRIS) 2,700 mg in sodium chloride 0.9% 54 mL IVPB\n\n"
              "Admin Date     Action     Dose       Rate       Route\n\n01/10/2025     New Bag    2,700 mg   50 mL/hr   Intravenous\n")


def _long_note() -> str:
    filler = "".join(f"Line {n}: the patient walks with a cane and reports fatigue.\n\n" for n in range(120))
    return ("HPI\n\n" + filler + "Selected relevant laboratory data\n\nComponent     Value     Date\n\n"
            "AQP4 Receptor Ab     Positive 1:100     03/02/2024\n\nSodium     140     03/02/2024\n\n" + filler
            + "Assessment\n\nStable. Continue current regimen.\n")


def test_a_short_note_is_read_whole_so_an_administration_table_is_not_cut():
    assert len(ADMIN_NOTE) < 4000
    for g in ("treatment", "treatment_detail", "relapse", "antibody"):
        assert select_view(TextView.full(ADMIN_NOTE), g).text == ADMIN_NOTE, g
    # without the rule, the table (under "Lab Results", a data section) is not among the treatment group's sections
    assert group_specs()["treatment"]["whole_note_under"] == 4000


def test_focus_reads_only_the_lines_around_a_keyword_anywhere_in_the_note():
    text = _long_note()
    assert len(text) > 12000
    ab = select_view(TextView.full(text), "antibody")
    assert "AQP4 Receptor Ab     Positive 1:100     03/02/2024" in ab.text and "Component     Value     Date" in ab.text
    assert len(ab.text) < 2500, "a few lines around the result, not the whole history"
    assert ab.text.startswith("HPI"), "the note's opening lines are kept for context"
    inf = select_view(TextView.full(text), "infection")
    assert not keyword_rx("infection").search(inf.text), "no keyword: nothing but the opening lines would be sent"
    wins = keyword_windows("a\n\nb\n\nKEY one\n\nc\n\nd\n\ne\n\nKEY two\n\nf", re.compile("KEY"), 1)
    assert len(wins) == 2 and wins[0][0] == 3, "one non-empty line either side; blank lines do not count"
    # offsets map back to the note, as quotes need
    start = ab.text.index("Positive 1:100")
    assert text[ab.to_original(start):ab.to_original(start) + 14] == "Positive 1:100"


def test_shorter_excerpts_for_relapse_and_treatment():
    from nmosd.extract.run import group_budgets

    class Client:
        num_ctx, num_predict = 10240, 3072

    from nmosd.redcap.schemas import schemas_for
    from tests.conftest import REPO

    budgets = group_budgets(Client(), schemas_for(REPO / "config/redcap/data_dictionary.csv"),
                            ["relapse", "treatment", "diagnosis"], 27000)
    assert budgets["relapse"] <= 9000 and budgets["treatment"] <= 9000 and budgets["diagnosis"] > 9000


# ---- infusion and administration records, read with text patterns (18.11.26) ----------------------------------
def test_administration_table_and_nurse_template_are_read_without_the_model():
    from nmosd.extract.admin_records import admin_rows, find_records, frequency_code

    (rec,) = find_records(ADMIN_NOTE, "2025-01-10")
    assert rec["code"] == "4" and rec["date"] == "2025-01-10" and rec["dose"] == "2,700 mg"
    (rec,) = find_records(INFUSION_NOTE, "2025-03-07")
    assert (rec["code"], rec["dose"], rec["freq"], rec["freq_other"], rec["date"]) == \
        ("4", "3,000 mg", "7", "every 8 weeks", "2025-03-07")
    no_drug = "Dx: Neuromyelitis Optica\n\nDose: 300 mg\n\nFrequency: Every 26 weeks\n\nPre-meds: PO Tylenol 650mg"
    assert find_records(no_drug, "2025-01-01") == [], "a dose with no drug named gives nothing: never guessed"
    named = no_drug + "\n\nUplizna infused per protocol. Solu-Medrol 100 mg given as pre-medication."
    (rec,) = find_records(named, "2025-01-01")
    assert rec["code"] == "5" and rec["freq"] == "6", "one maintenance drug named in the note; the steroid is a pre-med"
    botox = ADMIN_NOTE.replace("ravulizumab-cwvz (ULTOMIRIS) 2,700 mg in sodium chloride 0.9% 54 mL IVPB",
                               "botulinum toxin type A (BOTOX) injection 400 Units")
    assert find_records(botox, "2025-01-10") == [], "not a maintenance drug"
    assert frequency_code("every 2 weeks") == ("4", None) and frequency_code("Every 26 weeks.") == ("6", None)
    assert frequency_code("q6 months") == ("6", None) and frequency_code("twice daily") == ("1", None)
    assert frequency_code("every 8 weeks") == ("7", "every 8 weeks") and frequency_code("monthly") == ("5", None)
    rows = admin_rows(TextView.full(INFUSION_NOTE), run_id="r", doc_id="D1", chunk_id="c", study_id="S",
                      doc_date="2025-03-07")
    got = {r["field"]: r["value"] for r in rows}
    assert got == {"maintenance_therapy": "4", "event": "ongoing", "maintenance_tx_dosing": "3,000 mg",
                   "maintenance_tx_freq": "7", "other_frequency_detail": "every 8 weeks"}
    assert len({r["item_id"] for r in rows}) == 1 and all(r["field_group"] == "treatment_admin" for r in rows)
    r0 = rows[0]
    assert "Ravulizumab" in INFUSION_NOTE[r0["char_start"]:r0["char_end"]] and r0["event_date"] == "2025-03-07"


def _admin(n, d, dose, drug="4"):
    return Item(f"a{n}", "treatment_admin", f"A{n}", d, 0,
                {"maintenance_therapy": drug, "event": "ongoing", "maintenance_tx_dosing": dose}, d, "1", False, False,
                [f"ea{n}"], {"maintenance_tx_dosing": f"ea{n}"}, "ravulizumab-cwvz (ULTOMIRIS)")


def test_administration_records_make_one_course_and_a_loading_dose_is_its_start():
    recs = [_admin(1, "2026-01-05", "2,700 mg"), _admin(2, "2026-01-19", "3,300 mg"), _admin(3, "2026-03-16", "3,300 mg")]
    (ep,) = reconcile_treatments(recs)
    v = ep["values"]
    assert v["date_starting_maint"] == "2026-01-05" and v["still_on_therapy"] == "1"
    assert any("loading dose" in x for x in ep["info"]) and "may have begun earlier" not in ep["conflict"]
    # the same dose every time: the course is ongoing, but these notes do not show when it began
    same = [_admin(n, d, "3,000 mg") for n, d in enumerate(["2025-01-10", "2025-03-07", "2025-05-02"])]
    (ep,) = reconcile_treatments(same)
    assert ep["values"]["date_starting_maint"] is None and ep["values"]["still_on_therapy"] == "1"
    assert "no start date found" in ep["conflict"]
    # dose and frequency reach the course's REDCap fields
    from nmosd.extract.stage2_instruments import merge_treatment_details
    from nmosd.redcap.dictionary import load_dictionary
    from tests.conftest import REPO

    dic = load_dictionary(REPO / "config/redcap/data_dictionary.csv")
    for i in recs:
        i.fields |= {"maintenance_tx_freq": "7", "other_frequency_detail": "every 8 weeks"}
    (inst,) = merge_treatment_details(reconcile_treatments(recs), recs, dic, [])
    assert inst["values"]["maintenance_tx_dosing"] == "3,300 mg", "the usual dose, not the loading dose"
    assert inst["values"]["maintenance_tx_freq"] == "7" and inst["values"]["other_frequency_detail"] == "every 8 weeks"


# ---- laboratory table rows, read with text patterns (18.11.26; layouts from `nmosd lab-shapes`) ----------------
LAB_NOTE = """Lab Results

Component          Value          Date

IGG                612 (L)        03/14/2024

IGA                85             03/14/2024

IGM                22 (L)         03/14/2024

CD19 ABS           <1 (L)         03/14/2024

CD19               4              03/14/2024

Anticardiolipin IgG      <9       03/14/2024

IgG index          0.9            03/14/2024

Collected: 02/02/2024 0815

IMMUNOGLOBULIN G     540     700 - 1600     mg/dL     Final

ABSOLUTE LYMPHS      1.2     1.0 - 4.8      K/uL      Final

IgA      85 mg/dL      70 - 400 mg/dL

NMO/AQP4 FACS Titer, S      Positive      1:100

MOG FACS, S          Negative

Sodium               140     135 - 145      mmol/L    Final
"""


def test_lab_table_rows_are_read_without_the_model():
    from nmosd.extract.lab_tables import find_lab_rows, lab_table_rows

    recs = find_lab_rows(LAB_NOTE, "2024-03-20")
    labs = [(r["test"], r["value"], r["unit"], r["date"]) for r in recs if "test" in r]
    assert labs == [("igg", "612", "mg/dL", "2024-03-14"), ("iga", "85", "mg/dL", "2024-03-14"),
                    ("igm", "22", "mg/dL", "2024-03-14"), ("cd19_abs", "<1", "", "2024-03-14"),
                    ("cd19_unspecified", "4", "", "2024-03-14"),      # count or percent is not said: kept raw
                    ("igg", "540", "mg/dL", "2024-02-02"), ("alc", "1.2", "K/uL", "2024-02-02"),
                    ("iga", "85", "mg/dL", "2024-02-02")]
    # not read: another antibody's IgG, the IgG index, an unrelated test
    abs_ = [(r["antibody"], r["result"], r["titer"], r["method"], r["date"]) for r in recs if "antibody" in r]
    assert abs_ == [("aqp4", "positive", "1:100", "1", "2024-02-02"), ("mog", "negative", None, "1", "2024-02-02")]
    assert find_lab_rows("IGG      6.5      03/14/2024", "2024-03-20") == [], "no unit and not a plausible mg/dL: not guessed"
    assert find_lab_rows("She has IgG 612 and normal IgA.", "2024-03-20") == [], "a sentence is the model's job"
    rows = lab_table_rows(TextView.full(LAB_NOTE), run_id="r", doc_id="D", chunk_id="c", study_id="S", doc_date="2024-03-20")
    first = [r for r in rows if r["item_id"] == rows[0]["item_id"]]
    assert {r["field"]: r["value"] for r in first} == {"test": "igg", "value": "612", "unit": "mg/dL", "test_date": "2024-03-14"}
    assert LAB_NOTE[first[0]["char_start"]:first[0]["char_end"]].startswith("IGG") and first[0]["event_date"] == "2024-03-14"


def test_lab_table_rows_reach_the_lab_and_antibody_instruments():
    from nmosd.extract.lab_tables import lab_table_rows
    from nmosd.extract.stage2_instruments import lab_results, reconcile_labs
    from nmosd.extract.stage2_reconcile import reconcile_antibodies
    from nmosd.redcap.dictionary import load_dictionary
    from tests.conftest import REPO

    dic = load_dictionary(REPO / "config/redcap/data_dictionary.csv")
    items: dict[str, Item] = {}
    for r in lab_table_rows(TextView.full(LAB_NOTE), run_id="r", doc_id="D", chunk_id="c", study_id="S",
                            doc_date="2024-03-20"):
        it = items.setdefault(r["item_id"], Item(r["item_id"], "labs_table", "D", "2024-03-20", r["char_start"], {},
                                                 r["event_date"], r["estimate_code"], quote=r["quote"]))
        it.fields[r["field"]] = r["value"]
        it.evidence_ids.append(r["evidence_id"])
        it.field_evidence[r["field"]] = r["evidence_id"]
    its = list(items.values())
    # a sentence the model read for the same day gives another IgG value: the table row is proposed, and it is noted
    its.append(Item("m1", "labs", "D", "2024-03-20", 5, {"test": "igg", "value": "600", "unit": "mg/dL"}, "2024-03-14",
                    "1", evidence_ids=["em1"], field_evidence={"value": "em1"}, quote="IgG about 600"))
    cd, ig = reconcile_labs(lab_results(its, {}), dic)
    by_date = {i["values"]["date_igg"]: i for i in ig}
    assert by_date["2024-03-14"]["values"] | {"date_igg": ""} == {"date_igg": "", "igg": "612", "iga": "85", "igm": "22"}
    assert "igg: notes give different values" in by_date["2024-03-14"]["conflict"]
    assert by_date["2024-02-02"]["values"]["igg"] == "540"
    (c,) = cd
    assert c["values"]["cd19_abs"] == "0" and c["values"]["date_cd"] == "2024-03-14", "'<1' cells/uL is entered as 0"
    aqp4, mog = sorted(reconcile_antibodies(its, dic), key=lambda a: a["values"]["type_test"])
    assert aqp4["values"]["aqp4_assay_status"] == "1" and aqp4["values"]["aqp4_assay_date"] == "2024-02-02"
    assert aqp4["values"]["aqp4_assay_type"] == "1" and aqp4["values"]["aqp4_assay_titer"] == "4", "1:100 is code 4"
    assert mog["values"]["mog_test_positive"] == "0"


# ---- dated header and list lines (18.11.26; layouts from `nmosd shapes`) -------------------------------------
def test_problem_list_row_gives_a_fallback_diagnosis_date():
    from nmosd.extract.note_facts import dx_date_rows, find_dx_rows
    from nmosd.extract.stage2_reconcile import problem_list_fallback

    note = ("Problem List\n\nNeuromyelitis optica      G36.0      03/14/2019 -\n\nHypertension      I10      01/02/2015 -\n\n"
            "Visit Diagnoses\n\nNeuromyelitis optica      -      Primary      G36.0\n\n"
            "She was told her mother has neuromyelitis optica since 05/05/2001.\n")
    (row,) = find_dx_rows(note, "2024-06-01")
    assert row["date"] == "2019-03-14", "the NMOSD row with a date; not hypertension, not the undated row, not a sentence"
    assert find_dx_rows("Neuromyelitis optica      G36.0      03/14/2030 -", "2024-06-01") == [], "a date after the note"
    (ev,) = dx_date_rows(TextView.full(note), run_id="r", doc_id="D", chunk_id="c", study_id="S", doc_date="2024-06-01")
    assert ev["field"] == "problem_noted" and ev["event_date"] == "2019-03-14" and "G36.0" in ev["quote"]
    it = Item("p1", "dx_dates", "D", "2024-06-01", 0, {"problem_noted": "2019-03-14"}, "2019-03-14", "1",
              evidence_ids=["e1"])
    empty = {"values": {}, "evidence": [], "conflict": "", "date_evidence": []}
    got = problem_list_fallback(empty, [it], {"date": "2021-01-01", "evidence": ["x"]})
    assert got["values"] == {"date_nmo_diagnosis": "2019-03-14", "date_nmo_estimate": "4"} and got["evidence"] == ["e1"]
    assert "problem list" in got["conflict"]
    stated = {**empty, "values": {"date_nmo_diagnosis": "2018-11-02", "date_nmo_estimate": "1"}}
    assert problem_list_fallback(stated, [it], None) is stated, "a stated diagnosis date is never replaced"
    assert problem_list_fallback(empty, [it], {"date": "2018-01-01", "evidence": ["x"]}) is empty, \
        "an earlier note already states NMOSD: that fallback is kept"


def test_admit_and_discharge_header_dates_set_the_stay():
    from nmosd.extract.note_facts import admission_date_rows, find_admission_dates
    from nmosd.extract.stage2_instruments import admissions

    note = "Discharge Summary\n\nAdmit date: 01/05/2025\n\nDischarge date: 02/15/2025\n\nHospital course: ...\n"
    rec = find_admission_dates(note, "2025-02-15")
    assert (rec["admission"], rec["discharge"]) == ("2025-01-05", "2025-02-15")
    assert find_admission_dates("Admit date: pending\nPlan: discharge to rehab", "2025-02-15") is None
    assert find_admission_dates("Admit date: 03/01/2025\nDischarge date: 02/15/2025", "2025-03-02") is None
    rows = admission_date_rows(TextView.full(note), run_id="r", doc_id="D9", chunk_id="c", study_id="S", doc_date="2025-02-15")
    assert {r["field"]: r["value"] for r in rows} == {"nr_admission": "2025-01-05", "nr_discharge": "2025-02-15"}
    header = Item("h1", "admission_dates", "D9", "2025-02-15", 0, {"nr_admission": "2025-01-05", "nr_discharge": "2025-02-15"},
                  "2025-01-05", "1", evidence_ids=["eh"], field_evidence={"nr_discharge": "eh"})
    # the model read a progress note mid-stay and gave an early "discharge"
    model = Item("m1", "admission", "D3", "2025-01-14", 0, {"nr_admission": "2025-01-05", "nr_discharge": "2025-01-14",
                                                              "for_attack": "1"}, "2025-01-05", "1", evidence_ids=["em"])
    late = Item("h2", "admission_dates", "D8", "2025-02-15", 0, {"nr_discharge": "2025-02-15"}, "2025-02-15", "1",
                evidence_ids=["eh2"])
    (stay,) = admissions([model, header])
    assert stay[0] is header, "the header's dates come first"
    (stay,) = admissions([model, late])
    assert stay[0] is late and len(stay) == 2, "a discharge-only header joins the long stay it ends"
    assert admissions([late]) == [], "and never makes a stay of its own"


def test_weight_with_the_unit_in_brackets_after_the_label():
    from nmosd.extract.vitals import find_vitals

    assert [(f, v) for f, v, *_ in find_vitals("WEIGHT(lbs)      150 lbs")] == [("weight", 68.0)]
    assert [(f, v) for f, v, *_ in find_vitals("Weight      68 kg (150 lb)")] == [("weight", 68.0)]


# ---- CD19 / CD20: never assumed to be a count or a percentage (the clinician's rules, 18.11.26) ----------------
def _lab_items(note: str, doc: str = "D", doc_date: str = "2024-03-20") -> list[Item]:
    from nmosd.extract.lab_tables import lab_table_rows

    items: dict[str, Item] = {}
    for r in lab_table_rows(TextView.full(note), run_id="r", doc_id=doc, chunk_id="c", study_id="S", doc_date=doc_date):
        it = items.setdefault(r["item_id"], Item(r["item_id"], "labs_table", doc, doc_date, r["char_start"], {},
                                                 r["event_date"], r["estimate_code"], quote=r["quote"]))
        it.fields[r["field"]] = r["value"]
        it.evidence_ids.append(r["evidence_id"])
        it.field_evidence[r["field"]] = r["evidence_id"]
    return list(items.values())


def _cd(note: str) -> dict:
    from nmosd.extract.stage2_instruments import lab_results, reconcile_labs
    from nmosd.redcap.dictionary import load_dictionary
    from tests.conftest import REPO

    cd, _ig = reconcile_labs(lab_results(_lab_items(note), {}), load_dictionary(REPO / "config/redcap/data_dictionary.csv"))
    assert len(cd) == 1
    return cd[0]


def test_cd19_row_is_a_count_or_a_percentage_only_when_the_row_or_its_heading_says_so():
    from nmosd.extract.lab_tables import find_lab_rows

    kinds = lambda note: [(r["test"], r["value"]) for r in find_lab_rows(note, "2024-03-20")]  # noqa: E731
    assert kinds("CD19      12      03/14/2024") == [("cd19_unspecified", "12")]
    assert kinds("CD19      12 cells/uL      03/14/2024") == [("cd19_abs", "12")], "the unit says count"
    assert kinds("CD20      3%      03/14/2024") == [("cd20_pct", "3")], "the unit says percent"
    assert kinds("CD19      12      3%") == [("cd19_abs", "12"), ("cd19_pct", "3")], "beside a percentage, the count"
    assert kinds("Marker      Absolute count      Date\n\nCD19      12      03/14/2024") == [("cd19_abs", "12")]
    assert kinds("Marker      % of lymphocytes      Date\n\nCD20      3      03/14/2024") == [("cd20_pct", "3")]
    assert kinds("Component      Value      Date\n\nCD19      12      03/14/2024") == [("cd19_unspecified", "12")]
    assert kinds("CD19      3 - 20      03/14/2024") == [], "a reference range is not a result"
    inst = _cd("Component      Value      Date\n\nCD19      12      03/14/2024\n\nCD20      <1 (L)      03/14/2024")
    v = inst["values"]
    assert v["cd19_yn"] == "1" and v["cd20_yn"] == "1", "a result exists"
    assert not {"cd19_abs", "cd19_percent", "cd20_abs", "cd20_percent", "cd19_abs_yn"} & set(v), "but it is not entered"
    assert "CD19 = 12 on a row that does not say count or percent" in inst["conflict"]
    assert "CD20 = <1 on a row that does not say count or percent" in inst["conflict"]


def test_flow_panel_subsets_add_up_to_cd19_and_never_stand_for_total_cd20():
    panel = ("Flow cytometry      Collected: 03/14/2024 0800\n\nCD19+CD20-      4      1%\n\n"
             "CD19+CD20+      10      2%      50-400      5-20%\n")
    inst = _cd(panel)
    v = inst["values"]
    assert v["cd19_abs"] == "14" and v["cd19_abs_yn"] == "1" and v["date_cd"] == "2024-03-14"
    assert "cd19_percent" not in v, "REDCap asks for the percentage only when there is no absolute count"
    assert any("CD19 count: CD19+CD20- plus CD19+CD20+ of the same flow panel" in x for x in inst["info"])
    # a panel that reports percentages only: they are added (one panel, one denominator)
    pct_only = _cd("Collected: 03/14/2024\n\nCD19+CD20-      1%\n\nCD19+CD20+      2%\n")
    assert pct_only["values"]["cd19_percent"] == "3" and pct_only["values"]["cd19_abs_yn"] == "0"
    assert "cd20_abs" not in v and "cd20_percent" not in v and "cd20_yn" not in v
    assert "CD19+CD20+ is reported (10, 2%), but no total CD20+" in inst["conflict"]
    # an explicitly reported total wins over the sum
    inst = _cd(panel + "\nCD19 ABS      15      03/14/2024\n\nCD20 ABS      11 cells/uL      03/14/2024\n")
    assert inst["values"]["cd19_abs"] == "15" and inst["values"]["cd20_abs"] == "11"
    assert "no total CD20+" not in inst["conflict"]
    # below the reporting limit: counts are 0 by the existing rule, percentages are not added
    inst = _cd("Collected: 03/14/2024\n\nCD19+CD20-      <1      <1%\n\nCD19+CD20+      <1      <1%\n")
    assert inst["values"]["cd19_abs"] == "0" and "cd19_percent" not in inst["values"]
    below = _cd("Collected: 03/14/2024\n\nCD19+CD20-      <1%\n\nCD19+CD20+      <1%\n")
    assert "cd19_percent" not in below["values"] and "below the reporting limit" in below["conflict"]
    # one subset alone is not the total
    inst = _cd("Collected: 03/14/2024\n\nCD19+CD20+      10      2%\n")
    assert "cd19_abs" not in inst["values"] and "only one of the CD19+CD20- and CD19+CD20+ rows" in inst["conflict"]


# ---- reader against model: a disagreement is flagged, the reader's value is proposed (18.11.26) ---------------
def _dic():
    from nmosd.redcap.dictionary import load_dictionary
    from tests.conftest import REPO

    return load_dictionary(REPO / "config/redcap/data_dictionary.csv")


def test_header_dates_are_proposed_and_a_different_model_date_is_flagged():
    from datetime import date

    from nmosd.extract.stage2_instruments import new_instance, route_admissions

    header = Item("h1", "admission_dates", "D9", "2025-02-15", 0, {"nr_admission": "2025-01-05", "nr_discharge": "2025-02-15"},
                  "2025-01-05", "1", evidence_ids=["eh"], field_evidence={"nr_admission": "eh", "nr_discharge": "eh"})

    def model(n, discharge, attack):
        return Item(f"m{n}", "admission", f"D{n}", "2025-01-14", 0,
                    {"nr_admission": "2025-01-05", "nr_discharge": discharge, "for_attack": attack}, "2025-01-05", "1",
                    evidence_ids=[f"em{n}"], field_evidence={"nr_discharge": f"em{n}", "nr_admission": f"em{n}"})

    # two notes mid-stay gave an early "discharge": more mentions, but the header decides, and says so
    (nr,) = route_admissions([], [model(1, "2025-01-14", "0"), model(2, "2025-01-14", "0"), header], _dic())
    assert nr["values"]["nr_discharge"] == "2025-02-15" and nr["values"]["nr_admission"] == "2025-01-05"
    assert ("nr_discharge: the summary header says 2025-02-15, the notes read by the model say 2025-01-14: the header's "
            "date is proposed") in nr["notes"]
    assert not any(n.startswith("nr_admission:") for n in nr["notes"]), "the admission dates agree: no flag"
    assert nr["evidence"]["nr_discharge"][0] == "eh"
    # the model agrees (a day either way is not a disagreement)
    (nr,) = route_admissions([], [model(1, "2025-02-16", "0"), header], _dic())
    assert not any("summary header" in n for n in nr["notes"]) and nr["values"]["nr_discharge"] == "2025-02-15"
    # an admission for an attack: the relapse instance's dates
    rel = new_instance("2025-01-03", date(2025, 1, 3))
    rel["values"] |= {"date_onset": "2025-01-03", "date_discharge": "2025-01-14"}
    assert route_admissions([rel], [model(1, "2025-01-14", "1"), header], _dic()) == []
    assert rel["values"]["date_discharge"] == "2025-02-15" and rel["values"]["date_admission"] == "2025-01-05"
    assert any(n.startswith("date_discharge: the summary header says 2025-02-15") for n in rel["notes"])


def test_administration_records_against_the_dose_and_start_the_model_read():
    from nmosd.extract.stage2_instruments import merge_treatment_details

    recs = [_admin(1, "2025-01-10", "3,300 mg"), _admin(2, "2025-03-07", "3,300 mg")]
    for r in recs:
        r.fields |= {"maintenance_tx_freq": "7", "other_frequency_detail": "every 8 weeks"}

    def detail(dose, freq):
        return Item("d1", "treatment_detail", "C1", "2025-03-08", 0,
                    {"maintenance_therapy": "4", "maintenance_tx_dosing": dose, "maintenance_tx_freq": freq},
                    evidence_ids=["ed"], field_evidence={"maintenance_tx_dosing": "ed"}, quote="Ultomiris")

    items = [*recs, detail("3000 mg IV", "5")]
    (inst,) = merge_treatment_details(reconcile_treatments(items), items, _dic(), [])
    assert inst["values"]["maintenance_tx_dosing"] == "3,300 mg" and inst["values"]["maintenance_tx_freq"] == "7"
    assert "dose: the administration records say 3300 mg, the notes read by the model say 3000 mg" in inst["conflict"]
    assert "frequency: the administration records say 7, the notes read by the model say 5" in inst["conflict"]
    items = [*recs, detail("3300mg", "7")]
    (inst,) = merge_treatment_details(reconcile_treatments(items), items, _dic(), [])
    assert "administration records say" not in inst["conflict"], "the same dose written another way is not a disagreement"
    # the model read a start after the first recorded administration
    start = _tx(9, "4", "start", "2025-03-01", quote="Started Ultomiris")
    (ep,) = reconcile_treatments([*recs, start])
    assert "an administration of this drug is recorded on 2025-01-10, before the proposed start date" in ep["conflict"]
    (ep,) = reconcile_treatments([recs[1], _tx(9, "4", "start", "2025-03-01", quote="Started Ultomiris")])
    assert "before the proposed start date" not in ep["conflict"]


def test_lab_table_titre_against_the_titre_the_model_read():
    from nmosd.extract.stage2_reconcile import reconcile_antibodies

    def ab(n, group, titer):
        return Item(f"a{n}", group, f"D{n}", "2024-02-03", 0, {"type_test": "aqp4", "result": "positive", "titer": titer},
                    "2024-02-02", "1", evidence_ids=[f"e{n}"])

    (inst,) = reconcile_antibodies([ab(1, "antibody", "titer 1:10"), ab(2, "antibody", "1:10"), ab(3, "labs_table", "1:100")],
                                   _dic())
    assert inst["values"]["aqp4_assay_titer"] == "4", "1:100, the table's titre"
    assert "titre: the lab table says 1:100, the notes read by the model say 1:10: the table's value is proposed" in inst["conflict"]
    (inst,) = reconcile_antibodies([ab(1, "antibody", "1 : 100"), ab(3, "labs_table", "1:100")], _dic())
    assert "titre:" not in inst["conflict"]


def test_problem_list_date_well_before_the_stated_diagnosis_date_is_flagged():
    from nmosd.extract.stage2_reconcile import problem_list_check

    row = Item("p1", "dx_dates", "D", "2024-06-01", 0, {"problem_noted": "2019-03-14"}, "2019-03-14", "1", evidence_ids=["ep"])
    dx = {"values": {"date_nmo_diagnosis": "2021-06-01", "date_nmo_estimate": "1"}, "evidence": ["e1"], "conflict": ""}
    got = problem_list_check(dx, [row])
    assert got["values"] == dx["values"], "the stated date stays the proposal"
    assert "give NMOSD from 2019-03-14, earlier than the diagnosis date the notes state" in got["conflict"]
    assert got["evidence"] == ["e1", "ep"]
    near = {**dx, "values": {"date_nmo_diagnosis": "2019-04-01"}}
    assert problem_list_check(near, [row]) is near and problem_list_check({**dx, "values": {}}, [row])["conflict"] == ""
