"""Phase 3: de-identification on synthetic data.

Acceptance: all seeded fake PHI is replaced (100%), clinical terms on the allow-list survive, intervals
between shifted dates equal the originals, and the leak test passes.
"""

from __future__ import annotations

import io
import itertools
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import date
from types import SimpleNamespace

import pytest
import yaml

from nmosd.config import load_settings
from nmosd.deid.date_shift import Offsets, find_dates, lookalike_reason, shift_text, unshift_date
from nmosd.deid.header import parse_header
from nmosd.deid.known_identifiers import IdentifierSet, KnownMatcher, from_yaml_entry, suggest_identifiers
from nmosd.deid.leak_test import run_leak_test
from nmosd.deid.linkage import LinkageError, load_linkage, save_linkage
from nmosd.deid.ner_models import DictionaryNER, Ensemble, HFTokenClassifier, map_label, merge_spans, windows
from nmosd.deid.patterns import PatternLayer
from nmosd.deid.pipeline import PatientCtx, deidentify_text, is_allowlisted, run_deid
from nmosd.deid.review_report import import_signoff, write_report
from nmosd.deid.study_ids import ALPHABET, draw_offset_days, new_doc_id, new_study_id
from nmosd.deid.surrogates import SurrogateMap
from nmosd.ingest.load_staging import stage
from nmosd.safety import SafetyError
from nmosd.synth.annotations import quote_pattern
from tests.conftest import NOTE_TABLES, REPO

SETTINGS = load_settings(os.environ["NMOSD_SETTINGS"])
PASS = "correct horse battery staple"
COMMON = {"hope"}


# =============================================================================================
# Date shifting
# =============================================================================================
OFF = Offsets(200)  # months +7, years +1


@pytest.mark.parametrize("text", [
    "VA 20/20 OD, 20/400 OS", "CF at 3 ft, HM", "Strength 5/5 throughout", "4+/5 hip flexion", "4-/5 grip",
    "BP 120/80", "BP Readings from Last 3 Encounters: 118/76", "Reflexes 2+ symmetric", "biceps 2/4 bilaterally",
    "Take 1/2 tab daily", "titer 1:100", "LETM C2-C5", "T4-T9 lesion", "C5/6 disc", "lasted 3-4 days", "EDSS 3.5",
    "pain 7/10", "grade 2/6 systolic murmur", "VA 6/12 OD", "1 1/2 tablets", "2/3 of the cord",
])
def test_lookalikes_are_never_shifted(text):
    assert shift_text(text, OFF, 2019).text == text


@pytest.mark.parametrize("src,expected", [
    ("3/4/2019", "9/20/2019"), ("03/04/19", "09/20/19"), ("2019-03-04", "2019-09-20"), ("March 4, 2019", "September 20, 2019"),
    ("Mar. 4, 2019", "Sep. 20, 2019"), ("4 March 2019", "20 September 2019"), ("04-MAR-2019", "20-SEP-2019"),
    ("Monday, March 4, 2019", "Friday, September 20, 2019"), ("March 2019", "October 2019"), ("March '19", "October '19"),
    ("9/2016", "4/2017"), ("3/98", "10/98"), ("summer 2016", "winter 2017"), ("Summer of 2016", "Winter of 2017"), ("in 2016", "in 2017"),
    ("2016-2018", "2017-2019"), ("(2011)", "(2012)"), ("March 4", "September 20"), ("3/19", "10/19"),
])
def test_formats_preserved(src, expected):
    assert shift_text(src, OFF, 2019).text == expected


def test_ambiguous_short_form_rule_and_wrap_flag():
    r = shift_text("follow-up 11/20", OFF, 2020)
    assert r.text == "follow-up 06/21", "month wraps into the next year: second number moves with the carry"
    assert r.counts()["flag_ambiguous_short_date_year_wrap"] == 1
    r2 = shift_text("relapse 3/19", OFF, 2019)
    assert r2.text == "relapse 10/19" and "flag_ambiguous_short_date_year_wrap" not in r2.counts()


def test_holidays_and_anchors_flagged():
    r = shift_text("Over Christmas 2018 and on her birthday", OFF, 2019)
    assert "[HOLIDAY]" in r.text and r.counts()["flag_holiday"] == 1 and r.counts()["flag_relative_anchor"] == 1


def test_lowercase_may_is_not_a_date():
    assert shift_text("She may 2 times a day", OFF, 2019).text == "She may 2 times a day"


def test_strong_date_cue_overrides_strength_rule():
    assert lookalike_reason("Started on 5/5", 11, 14, 5, 5) is None
    assert lookalike_reason("Strength 5/5", 9, 12, 5, 5) == "strength"


def test_intervals_preserved_for_full_dates():
    text = "MRI 3/4/2019; relapse 4/30/2019; admitted 2019-05-01, discharged May 5, 2019."
    out, _ = find_dates(shift_text(text, OFF, 2019).text, 2019)
    orig, _ = find_dates(text, 2019)
    assert [(o.value - orig[0].value) for o in orig] == [(o.value - out[0].value) for o in out]


@pytest.mark.parametrize("days", [30, -30, 45, -200, 365, -365, 183, -183])
def test_unshift_inverts_each_precision(days):
    off = Offsets(days)
    d = date(2019, 3, 4)
    assert unshift_date(d + (date(2019, 1, 2) - date(2019, 1, 1)) * days, off, "day") == d
    sm = shift_text("March 2019", off, 2019).text
    (m,), _ = find_dates(sm, 2019)
    assert unshift_date(m.value, off, "month").replace(day=1) == date(2019, 3, 1)
    sy = shift_text("in 2016", off, 2019).text
    assert unshift_date(date(int(sy[-4:]), 7, 1), off, "year").year == 2016


def test_offsets_drawn_in_range():
    for _ in range(200):
        d = draw_offset_days(30, 365)
        assert 30 <= abs(d) <= 365
        assert Offsets(d).months != 0


# =============================================================================================
# IDs, linkage, safety
# =============================================================================================
def test_study_and_doc_ids():
    ids = {new_study_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(re.fullmatch(rf"NMO-[{ALPHABET}]{{6}}", i) for i in ids)
    assert re.fullmatch(rf"DOC-[{ALPHABET}]{{10}}", new_doc_id())


def test_linkage_roundtrip_and_wrong_passphrase(tmp_path):
    p = tmp_path / "link" / "k.enc"
    save_linkage(p, {"patients": {"NMO-AAAAAA": {"empi": "123"}}}, PASS)
    assert b"123" not in p.read_bytes()
    assert load_linkage(p, PASS)["patients"]["NMO-AAAAAA"]["empi"] == "123"
    with pytest.raises(LinkageError):
        load_linkage(p, "wrong passphrase!!")
    with pytest.raises(LinkageError):
        save_linkage(p, {}, "short")


def test_linkage_must_not_sit_next_to_output(staged_for_deid, tmp_path, synthetic_dir):
    with pytest.raises(SafetyError):
        run_deid(staged_for_deid, tmp_path / "out" / "deid.sqlite", tmp_path / "out" / "link.enc",
                 synthetic_dir / "identifiers.yaml", SETTINGS, use_ner=False, passphrase=PASS, stdout=io.StringIO())
    with pytest.raises(SafetyError):
        run_deid(staged_for_deid, tmp_path / "out" / "deid.sqlite", tmp_path / "out" / "sub" / "link.enc",
                 synthetic_dir / "identifiers.yaml", SETTINGS, use_ner=False, passphrase=PASS, stdout=io.StringIO())
    with pytest.raises(SafetyError):
        run_deid(staged_for_deid, REPO / "outputs" / "deid.sqlite", tmp_path / "l" / "link.enc",
                 synthetic_dir / "identifiers.yaml", SETTINGS, use_ner=False, passphrase=PASS, stdout=io.StringIO())


# =============================================================================================
# Header, known identifiers, patterns, NER, surrogates
# =============================================================================================
def test_headers_parse_and_harvest(synthetic_tables):
    for t in NOTE_TABLES:
        for r in synthetic_tables[t]:
            h = parse_header(r["Report_Text"], t)
            assert h.matched, (t, r["Report_Text"][:200])
            if t in ("VisitNote", "ProgressNote", "Discharge_Summaries", "HistoryAndPhysical"):
                assert h.replace and h.banner and h.harvested.get("mrn")
    vis = next(r for r in synthetic_tables["VisitNote"] if "“" in r["Report_Text"].split("\r\n")[1])
    h = parse_header(vis["Report_Text"], "VisitNote")
    assert h.harvested["preferred"] and h.harvested["provider"]


def _ids():
    return from_yaml_entry({
        "names": {"first": ["Rosalind"], "last": ["Quillfeather-Ashby"], "preferred": ["Roz"]},
        "mrns": ["0441726"], "dob": "1988-03-04",
        "relatives": [{"relation": "sister", "name": "Hope Ashby"}],
        "address": {"street": ["14 Juniper Hollow Road"], "town": ["Wexley Falls"], "zip": ["01987"]},
        "phones": ["(978) 555-0193"], "emails": ["roz@example.com"],
    })


@pytest.mark.parametrize("text,cat", [
    ("Rosalind Quillfeather-Ashby", "PATIENT"), ("QUILLFEATHER-ASHBY, ROSALIND", "PATIENT"), ("R. Quillfeather", "PATIENT"),
    ("Quillfeather's husband", "PATIENT"), ("Ashby, Rosalind M", "PATIENT"), ("Roz", "PATIENT"),
    ("MRN 441-726", "ID"), ("MRN 0441726", "ID"), ("MRN 441 726", "ID"),
    ("DOB 3/4/1988", "DOB"), ("born March 4, 1988", "DOB"), ("1988-03-04", "DOB"), ("dob 03/04/88", "DOB"),
    ("DOB: 04-MAR-1988", "DOB"), ("(978) 555-0193", "PHONE"), ("978.555.0193", "PHONE"), ("9785550193", "PHONE"),
    ("14 Juniper Hollow Rd.", "ADDRESS"), ("Juniper Hollow Road", "ADDRESS"), ("Wexley Falls", "TOWN"),
    ("01987-1234", "ZIP"), ("Hope Ashby", "RELATIVE"), ("her sister Hope", "RELATIVE"), ("ROZ@EXAMPLE.COM", "EMAIL"),
])
def test_known_identifier_variants(text, cat):
    hits = KnownMatcher(_ids()).find(text)
    assert any(h.category == cat for h in hits), (text, [(h.category, text[h.start:h.end]) for h in hits])


def test_common_word_names_are_case_sensitive():
    m = KnownMatcher(_ids())
    assert not m.find("we hope she improves")
    assert m.find("Hope visited")


def test_staff_variants_do_not_overmatch():
    m = KnownMatcher(IdentifierSet(), staff=["Theodora Blackwood, MD", "Octavia F", "Dr. B"])
    text = "enhancement of both nerves; 2000 units PO daily; CF at 3 ft; hepatitis B; Temp 98.2 F; D.O.B."
    assert not m.find(text)
    assert m.find("seen by Dr. Blackwood") and m.find("T. Blackwood")


def test_pattern_layer():
    t = ("Her mother of three and husband Bartholomew. Seen at St. Brendan's Medical Center, referred by Dr. "
         "Pennywhistle. Electronically signed by Theodora Blackwood, MD. 91-year-old, aged 93. 555-0193 call. "
         "x@y.org, www.example.org, SSN 123-45-6789, MRN: 1234567, S19-12345.\r\nNeurology Progress Note\r\nHospital day 2")
    cats = {(h.category, t[h.start:h.end]) for h in PatternLayer().find(t)}
    for c, v in [("RELATIVE", "Bartholomew"), ("HOSPITAL", "St. Brendan's Medical Center"), ("STAFF", "Pennywhistle"),
                 ("STAFF", "Theodora Blackwood"), ("AGE", "91"), ("AGE", "93"), ("EMAIL", "x@y.org"),
                 ("URL", "www.example.org"), ("ID", "123-45-6789"), ("ID", "S19-12345")]:
        assert (c, v) in cats, (c, v)
    assert not any(v == "three" for _, v in cats)
    assert not any("Neurology" in v for c, v in cats if c == "HOSPITAL")


def test_allowlist():
    for term in ["Mayo Clinic", "rituximab", "Devic's", "Uhthoff", "AQP4", "Massachusetts", "Babinski"]:
        assert is_allowlisted(term)
    assert not is_allowlisted("Quillfeather")


def test_ner_ensemble_union_labels_and_windows():
    a = DictionaryNER("a", {"Blackwood": "STAFF", "Mayo Clinic": "ORGANIZATION"})
    b = DictionaryNER("b", {"Theodora Blackwood": "STAFF", "Wexley": "LOCATION"})
    res = Ensemble([a, b]).predict("Theodora Blackwood at Mayo Clinic in Wexley")
    assert res.per_model == {"a": 2, "b": 2} and res.union == 3 and res.agreement == 1
    assert map_label("B-HCW", {"HCW": "STAFF"}) == "STAFF" and map_label("O", {}) == ""
    text = "word " * 1000
    ws = windows(text, 400, 100)
    assert ws[0][0] == 0 and ws[-1][1] == len(text) and all(b2[0] < a2[1] for a2, b2 in itertools.pairwise(ws))
    from nmosd.deid.ner_models import NERSpan
    assert len(merge_spans([NERSpan(0, 5, "A", 0.5), NERSpan(3, 9, "B", 0.9)])) == 1


def test_ner_spans_are_routed_and_allowlisted():
    ids = IdentifierSet()
    ctx = PatientCtx("1", "NMO-AAAAAA", Offsets(200), ids, KnownMatcher(ids), SurrogateMap())
    ner = Ensemble([DictionaryNER("m", {"Mayo Clinic": "ORGANIZATION", "Zebulon": "PATIENT", "3/4/2019": "DATE",
                                        "92": "AGE", "45": "AGE", "cycle 2 day 3": "DATE"})])
    r = deidentify_text("Zebulon, 92 and 45 years, tested at Mayo Clinic on 3/4/2019; cycle 2 day 3.", ctx,
                        PatternLayer(), ner)
    assert r.text.startswith("[PATIENT], 90+ and 45 years, tested at Mayo Clinic on 9/20/2019")
    assert r.counts["allowlist_override"] >= 1 and r.flags["ner_date_unparsed"] == 1


def test_surrogates_consistent_per_patient():
    s = SurrogateMap()
    assert s.token("STAFF", "Dr. Blackwood") == s.token("STAFF", "Theodora Blackwood, MD") == "[CLINICIAN_01]"
    assert s.token("STAFF", "Pennywhistle") == "[CLINICIAN_02]"
    assert s.token("RELATIVE", "Hope Ashby", "hope ashby") == s.token("RELATIVE", "Hope") == "[RELATIVE_01]"
    assert s.token("PHONE", "x") == "[PHONE]" and s.token("PATIENT", "Roz") == "[PATIENT]"


# =============================================================================================
# Full pipeline on the synthetic dataset
# =============================================================================================
@pytest.fixture(scope="module")
def staged_for_deid(synthetic_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("phi_work") / "staging.sqlite"
    stage(synthetic_dir / "synthetic_rpdr.sqlite", synthetic_dir / "pilot_empis.txt", out, SETTINGS, stdout=io.StringIO())
    return out


@pytest.fixture(scope="module")
def deid_run(staged_for_deid, synthetic_dir, tmp_path_factory):
    root = tmp_path_factory.mktemp("phi")
    out, link = root / "deid" / "deid.sqlite", root / "work" / "linkage" / "link.enc"
    buf = io.StringIO()
    rc = run_deid(staged_for_deid, out, link, synthetic_dir / "identifiers.yaml", SETTINGS, use_ner=False,
                  passphrase=PASS, stdout=buf)
    assert rc == 0
    con = sqlite3.connect(out)
    con.row_factory = sqlite3.Row
    yield {"out": out, "link_path": link, "link": load_linkage(link, PASS), "con": con, "stdout": buf.getvalue(),
           "staging": staged_for_deid, "root": root}
    con.close()


def _docs_by_empi(run):
    sid = {v["empi"]: k for k, v in run["link"]["patients"].items()}
    out = defaultdict(list)
    for r in run["con"].execute("SELECT study_id, text, report_description FROM documents"):
        out[r["study_id"]].append(r["text"] + "\n" + (r["report_description"] or ""))
    return {e: "\n".join(out[s]) for e, s in sid.items()}


def test_all_seeded_phi_removed(deid_run, seeded_phi, pilot_empis):
    docs = _docs_by_empi(deid_run)
    total, leaked = 0, []
    for empi in pilot_empis:
        for item in seeded_phi[empi]:
            v = item["value"]
            flags = 0 if v.lower() in COMMON else re.I
            total += 1
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])", docs[empi], flags):
                leaked.append(item)
    assert total > 200
    assert leaked == [], leaked


def test_no_source_identifiers_in_output(deid_run, synthetic_tables, pilot_empis):
    con = deid_run["con"]
    blob = "\n".join(str(v) for t in ("patients", "documents", "structured_rows", "span_counts", "review_queue")
                     for r in con.execute(f"SELECT * FROM {t}") for v in tuple(r))
    for e in pilot_empis:
        assert e not in blob
    for t in NOTE_TABLES:
        for r in synthetic_tables[t]:
            if r["EMPI"] in pilot_empis:
                assert r["Report_Number"] not in blob
    sids = [r[0] for r in con.execute("SELECT study_id FROM patients")]
    assert len(sids) == 5 and all(s.startswith("NMO-") for s in sids)
    texts = [r[0] for r in con.execute("SELECT text FROM documents")]
    assert not any("report contains confidential" in t for t in texts), "banner removed"
    assert sum(t.startswith("[") for t in texts) > 100, "neutral headers"


def test_clinical_terms_survive(deid_run):
    blob = "\n".join(r[0] for r in deid_run["con"].execute("SELECT text FROM documents"))
    for term in ["optic neuritis", "rituximab", "inebilizumab", "Uhthoff", "Mayo Clinic", "AQP4-IgG", "PLEX",
                 "IV methylprednisolone", "units PO daily", "hepatitis B", "Temp 98.2 F", "VA 20/20 OD", "20/400",
                 "Strength 5/5", "4+/5", "1/2 tab", "titer 1:100", "C2-C5", "T4-T9", "EDSS", "D.O.B.",
                 "enhancement of both optic nerves", "multiple sclerosis", "Dx:", "Tx:", "FINDINGS", "IMPRESSION"]:
        assert term in blob, term


def _annotation_hits(run, synthetic_dir):
    """Yield (empi, slot precision, original slot text, deid slot text) for annotated date slots."""
    ann = json.loads((synthetic_dir / "annotations.json").read_text(encoding="utf-8"))
    st = sqlite3.connect(run["staging"])
    by_src = {(r[0], r[1], r[2]): r[3] for r in st.execute(
        "SELECT source_table, report_number, report_status, doc_pk FROM document_sources")}
    st.close()
    doc_of_pk = {v["doc_pk"]: k for k, v in run["link"]["documents"].items()}
    texts = {r["doc_id"]: r["text"] for r in run["con"].execute("SELECT doc_id, text FROM documents")}
    found = missing = 0
    out = []
    for a in ann:
        pk = by_src.get((a["table"], a["report_number"], a["status"]))
        if pk is None:
            continue
        text = texts[doc_of_pk[pk]]
        for an in a["annotations"]:
            if not an["slots"]:
                continue
            m = quote_pattern(an["segments"]).search(text)
            if not m:
                missing += 1
                continue
            found += 1
            for name, slot in an["slots"].items():
                out.append((a["empi"], slot["precision"], slot["text"], m.group(name)))
    return out, found, missing


def test_every_annotated_date_was_shifted_and_intervals_preserved(deid_run, synthetic_dir):
    hits, found, missing = _annotation_hits(deid_run, synthetic_dir)
    assert found > 300 and missing / (found + missing) < 0.02
    offsets = {v["empi"]: Offsets(v["offset_days"]) for v in deid_run["link"]["patients"].values()}
    deltas = defaultdict(set)
    for empi, prec, orig, new in hits:
        off = offsets[empi]
        assert new == shift_text(orig, off, 2026).text, (prec, orig, new)
        if prec in ("day", "month"):
            assert orig != new, (prec, orig, new)
        if prec == "day":
            (o,), _ = find_dates(orig, 2026)
            (n,), _ = find_dates(new, 2026)
            if o.kind == "full" and n.kind == "full":
                deltas[empi].add((n.value - o.value).days)
    assert deltas and all(len(v) == 1 and next(iter(v)) == offsets[e].days for e, v in deltas.items())


def test_ages_over_89_capped(deid_run):
    con = deid_run["con"]
    assert "90+" in [r[0] for r in con.execute("SELECT age_at_export FROM patients")]
    blob = "\n".join(r[0] for r in con.execute("SELECT text FROM documents"))
    assert "90+" in blob and not re.search(r"\b9[0-9]-year-old", blob)


def test_manifest_and_stdout(deid_run, seeded_phi):
    m = {r[0]: r[1] for r in deid_run["con"].execute("SELECT key, value FROM manifest")}
    for k in ["tool_version", "rules_hash", "run_at", "counts_by_category", "header_match", "signoff_reviewer",
              "signoff_complete", "ner_span_counts_per_model", "date_shift_range_days"]:
        assert k in m, k
    out = deid_run["stdout"]
    assert "Header blocks matched" in out and "VisitNote: 84/84 (100%)" in out
    for v in {x["value"] for vs in seeded_phi.values() for x in vs if len(x["value"]) >= 4}:
        assert v not in out


def test_leak_test_passes_and_detects_injection(deid_run, tmp_path):
    buf = io.StringIO()
    assert run_leak_test(deid_run["out"], deid_run["link_path"], SETTINGS, use_ner=False, passphrase=PASS, stdout=buf) == 0
    assert "Known-identifier hits: 0 (PASS)" in buf.getvalue()
    bad = tmp_path / "deid" / "bad.sqlite"
    bad.parent.mkdir()
    bad.write_bytes(deid_run["out"].read_bytes())
    c = sqlite3.connect(bad)
    c.execute("UPDATE documents SET text = text || ' seen with Rosalind Quillfeather' WHERE rowid = 1")
    c.commit()
    c.close()
    buf = io.StringIO()
    assert run_leak_test(bad, deid_run["link_path"], SETTINGS, use_ner=False, passphrase=PASS, stdout=buf) == 1
    assert "Quillfeather" not in buf.getvalue() and "PATIENT" in buf.getvalue()


def test_leak_test_saves_where_the_spots_are_and_the_report_marks_them(deid_run, tmp_path):
    bad = tmp_path / "deid" / "spots.sqlite"
    bad.parent.mkdir()
    bad.write_bytes(deid_run["out"].read_bytes())
    c = sqlite3.connect(bad)
    c.execute("UPDATE documents SET text = text || ' Seen with Zebedee Fakename, aged 95 years; his son is 42 years.' "
              "WHERE rowid = 1")
    c.commit()
    doc_id, text = c.execute("SELECT doc_id, text FROM documents WHERE rowid = 1").fetchone()
    c.close()
    ens = Ensemble([DictionaryNER("fake", {"Zebedee Fakename": "PATIENT", "95 years": "AGE", "42 years": "AGE"})])
    cand = tmp_path / "reports" / "leak_candidates.json"
    buf = io.StringIO()
    rc = run_leak_test(bad, deid_run["link_path"], SETTINGS, use_ner=True, ensemble=ens, passphrase=PASS, stdout=buf,
                       candidates_out=cand)
    out = buf.getvalue()
    assert rc == 0 and "PATIENT: 1" in out and "  AGE: 1" in out        # the age under 90 is kept on purpose
    assert "Zebedee" not in out
    saved = json.loads(cand.read_text(encoding="utf-8"))
    assert "Zebedee" not in cand.read_text(encoding="utf-8")
    spots = saved["documents"][doc_id]["spots"]
    assert sorted((text[s:e], c) for f, s, e, c, _ in spots if f == "text") == [
        ("95", "AGE"), ("95 years", "AGE"), ("Zebedee Fakename", "PATIENT"), ("Zebedee Fakename", "STAFF")]
    report = tmp_path / "reports" / "deid_report.html"
    buf = io.StringIO()
    write_report(deid_run["staging"], bad, deid_run["link_path"], report, SETTINGS, passphrase=PASS, stdout=buf)
    page = report.read_text(encoding="utf-8")
    # overlapping spots from the two checks are shown as one
    assert "Spots to check: 2</strong> in 1 notes" in page and page.count("data-chk=1") == 1
    assert "PATIENT / STAFF (1)" in page and "AGE (1)" in page
    assert 'id="chk-1"' in page and 'id="chk-2"' in page and "Show only notes with spots to check" in page
    assert "Spots to check marked: 2 in 1 documents" in buf.getvalue()
    # a later change to that note (a new de-identification run) makes its saved positions stale: not marked
    c = sqlite3.connect(bad)
    c.execute("UPDATE documents SET text = 'changed ' || text WHERE rowid = 1")
    c.commit()
    c.close()
    write_report(deid_run["staging"], bad, deid_run["link_path"], report, SETTINGS, passphrase=PASS,
                 stdout=io.StringIO())
    page = report.read_text(encoding="utf-8")
    assert "Spots to check: 0" in page and "1 notes changed since the leak test" in page


def test_report_without_leak_test_positions_says_how_to_get_them(deid_run, tmp_path):
    report = tmp_path / "r" / "deid_report.html"
    write_report(deid_run["staging"], deid_run["out"], deid_run["link_path"], report, SETTINGS, passphrase=PASS,
                 stdout=io.StringIO())
    assert "Spots to check:</strong> not available" in report.read_text(encoding="utf-8")


def test_rerun_reuses_ids_offsets_and_doc_ids(deid_run, synthetic_dir):
    out2 = deid_run["root"] / "deid2" / "deid.sqlite"
    run_deid(deid_run["staging"], out2, deid_run["link_path"], synthetic_dir / "identifiers.yaml", SETTINGS,
             use_ner=False, passphrase=PASS, stdout=io.StringIO())
    link2 = load_linkage(deid_run["link_path"], PASS)
    assert {k: v["offset_days"] for k, v in link2["patients"].items()} == \
           {k: v["offset_days"] for k, v in deid_run["link"]["patients"].items()}
    c2 = sqlite3.connect(out2)
    assert sorted(r[0] for r in c2.execute("SELECT doc_id FROM documents")) == \
           sorted(r[0] for r in deid_run["con"].execute("SELECT doc_id FROM documents"))
    c2.close()


def test_with_ner_ensemble_records_counts(staged_for_deid, synthetic_dir, tmp_path):
    ens = Ensemble([DictionaryNER("obi_fake", {"Uhthoff": "PATIENT", "Theodora": "STAFF"}),
                    DictionaryNER("stanford_fake", {"Blackwood": "STAFF", "hot showers": "LOCATION"})])
    buf = io.StringIO()
    run_deid(staged_for_deid, tmp_path / "d" / "deid.sqlite", tmp_path / "l" / "link.enc", synthetic_dir / "identifiers.yaml",
             SETTINGS, use_ner=True, ensemble=ens, passphrase=PASS, stdout=buf)
    con = sqlite3.connect(tmp_path / "d" / "deid.sqlite")
    m = json.loads(con.execute("SELECT value FROM manifest WHERE key='ner_span_counts_per_model'").fetchone()[0])
    assert set(m) == {"obi_fake", "stanford_fake"} and all(v > 0 for v in m.values())
    blob = "\n".join(r[0] for r in con.execute("SELECT text FROM documents"))
    assert "Uhthoff" in blob, "allow-listed eponym survives even when a model flags it"
    assert "hot showers" not in blob
    con.close()


def test_fail_closed_without_identifiers_or_ner(staged_for_deid, tmp_path):
    buf = io.StringIO()
    rc = run_deid(staged_for_deid, tmp_path / "d" / "x.sqlite", tmp_path / "l" / "x.enc", None, SETTINGS,
                  use_ner=False, passphrase=PASS, stdout=buf)
    assert rc == 2 and "suggest-identifiers" in buf.getvalue()
    settings = load_settings(os.environ["NMOSD_SETTINGS"])
    settings.raw["deid"]["ner"]["models"] = [{"name": "x", "hf_id": "nonexistent/model-xyz", "label_map": "obi"}]
    rc = run_deid(staged_for_deid, tmp_path / "d" / "y.sqlite", tmp_path / "l" / "y.enc", None, settings,
                  use_ner=True, passphrase=PASS, allow_unconfirmed_identifiers=True, stdout=buf)
    assert rc == 2 and "Refusing to continue" in buf.getvalue()


def test_report_and_signoff(deid_run, tmp_path):
    out = tmp_path / "reports" / "deid_report.html"
    write_report(deid_run["staging"], deid_run["out"], deid_run["link_path"], out, SETTINGS, passphrase=PASS,
                 stdout=io.StringIO())
    html_text = out.read_text(encoding="utf-8")
    assert html_text.count("class='doc'") == 142 and "reviewed — no residual PHI" in html_text and "<mark" in html_text
    docs = [r[0] for r in deid_run["con"].execute("SELECT doc_id FROM documents")]
    so = tmp_path / "signoff.json"
    so.write_text(json.dumps({"reviewer": "Test Reviewer", "documents": {d: {"reviewed": True, "misses": ""} for d in docs}}))
    copy = tmp_path / "deid" / "deid.sqlite"
    copy.parent.mkdir()
    copy.write_bytes(deid_run["out"].read_bytes())
    m = import_signoff(copy, so, stdout=io.StringIO())
    assert m["signoff_complete"] is True and m["signoff_reviewed_docs"] == 142
    with pytest.raises(SafetyError):
        write_report(deid_run["staging"], deid_run["out"], deid_run["link_path"], REPO / "outputs" / "r.html", SETTINGS,
                     passphrase=PASS, stdout=io.StringIO())


def test_suggest_identifiers(staged_for_deid, tmp_path, seeded_phi):
    buf = io.StringIO()
    res = suggest_identifiers(staged_for_deid, tmp_path / "phi" / "suggested.yaml", stdout=buf)
    assert res["patients"] == 5 and res["staff"] >= 5
    data = yaml.safe_load((tmp_path / "phi" / "suggested.yaml").read_text())
    p1 = data["patients"]["100004417"]
    assert "Quillfeather" in p1["names"]["last"] and "Roz" in p1["names"]["preferred"] and "4417263" in p1["mrns"]
    assert p1["dob"] == "1988-03-04" and "Wexley Falls" in p1["address"]["town"]
    for v in {x["value"] for vs in seeded_phi.values() for x in vs if len(x["value"]) >= 4}:
        assert v not in buf.getvalue()


class _WhitespaceTokenizer:
    """Like byte-level BPE tokenizers with trimmed offsets: every whitespace token has a zero-width span."""
    model_max_length = 64

    def __call__(self, text, add_special_tokens=True, return_offsets_mapping=False):
        offs = [(m.start(), m.start() if m.group().isspace() else m.end()) for m in re.finditer(r"\S+|\s", text)]
        return {"offset_mapping": offs, "input_ids": list(range(len(offs) + (2 if add_special_tokens else 0)))}


def test_token_windows_count_zero_width_whitespace_tokens():
    m = HFTokenClassifier("fake", "fake/model", {})
    m._pipe = SimpleNamespace(tokenizer=_WhitespaceTokenizer(),
                              model=SimpleNamespace(config=SimpleNamespace(max_position_embeddings=512)))
    text = "\r\n".join(f"BP{i:03d}" + " " * 12 for i in range(300))
    wins = m.token_windows(text)
    assert wins[0][0] == 0 and wins[-1][1] == len(text)
    assert all(b[0] < a[1] for a, b in itertools.pairwise(wins))
    for s, e in wins:
        assert len(m._pipe.tokenizer(text[s:e])["input_ids"]) <= 64, (s, e)
