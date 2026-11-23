"""`nmosd quality-packet` (16.11.26): a HIPAA Safe Harbor-style packet of the proposals and their evidence for an outside
quality check. Synthetic data only.

The packet holds no date element but the year, no study or document IDs, no known identifier of any patient, no
ages over 89; it is refused for notes that were not de-identified or not signed off, and refused (nothing written)
if a check fails.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import sqlite3
from datetime import date

import pytest

from nmosd.deid.date_shift import find_dates
from nmosd.eval.quality_packet import DateTokens, PacketRefused, Scrubber, build_packet, run_quality_packet
from tests.conftest import PASSPHRASE, REPO

DICT = REPO / "config/redcap/data_dictionary.csv"


def _copy(pipeline, tmp_path, signoff=True, deidentified=True):
    """The pipeline's databases in a fresh folder, with the de-identification signed off (or not)."""
    d = tmp_path / "deid"
    d.mkdir(parents=True)
    for k in ("deid", "extract"):
        shutil.copy(pipeline[k], d / pipeline[k].name)
    con = sqlite3.connect(d / pipeline["deid"].name)
    con.execute("INSERT OR REPLACE INTO manifest(key, value) VALUES ('signoff_complete', ?)", ("true" if signoff else "false",))
    con.execute("INSERT OR REPLACE INTO manifest(key, value) VALUES ('deidentified', ?)", ("true" if deidentified else "false",))
    con.commit()
    con.close()
    return d / pipeline["deid"].name, d / pipeline["extract"].name, d / "review.sqlite"


def _years_only(text: str) -> bool:
    bare = re.sub(r"\[\[[^\]]*\]\]", " ", text)
    return all(m.kind in ("year", "year_range") for m in find_dates(bare, date.today().year)[0])


def test_scrubber_keeps_only_years():
    from nmosd.config import load_settings

    dt = DateTokens(["2019-03-14"])
    s = Scrubber(load_settings(REPO / "config/settings.example.yaml"))
    out = s.scrub("Seen 03/14/2019, again in 3/2019 and since 2019. A 92-year-old; call 617-555-1234. Age: 95", dt, 2023)
    assert "[[2019 D01]]" in out and "[[2019 D02 month-year]]" in out and "since 2019" in out
    assert "92" not in out and "95" not in out and "Age: 90+" in out and "555" not in out
    assert _years_only(out)
    assert dt.token("2019-03-14") == "[[2019 D01]]", "the same date is always the same token"


def test_date_tokens_numbered_as_in_an_earlier_packet():
    first = DateTokens(["2019-03-14", "2019-06-02", "2020-01-10"])
    seed = {iso: tok.lstrip("D") for tok, iso in first.key().items()}
    assert first.key() == {"D01": "2019-03-14", "D02": "2019-06-02", "D03": "2020-01-10"}
    dt = DateTokens(["2019-06-02", "2019-04-01", "2019-05-01", "2018-12-31", "2021-02-02"], seed)
    assert dt.token("2019-06-02") == "[[2019 D02]]" and dt.token("2020-01-10") == "[[2020 D03]]", "old numbers kept"
    assert dt.token("2019-04-01") == "[[2019 D01a]]" and dt.token("2019-05-01") == "[[2019 D01b]]", "in date order"
    assert dt.token("2018-12-31") == "[[2018 D00a]]" and dt.token("2021-02-02") == "[[2021 D03a]]"
    assert dt.token("2019-03-20") == "[[2019 D01c]]", "a date met later takes the next free letter"
    assert dt.key()["D01a"] == "2019-04-01" and dt.key()["D02"] == "2019-06-02" and len(dt.key()) == 8


def test_packet_with_the_tokens_of_an_earlier_packet(pipeline, seeded_phi, tmp_path):
    deid, extract, review = _copy(pipeline, tmp_path)
    kw = dict(passphrase=PASSPHRASE, dictionary=DICT, stdout=io.StringIO())
    one = build_packet(extract, deid, review, pipeline["linkage"], tmp_path / "packet_one", pipeline["settings"], **kw)
    two = build_packet(extract, deid, review, pipeline["linkage"], tmp_path / "packet_two", pipeline["settings"],
                       tokens_from=one["key"], **kw)
    k1, k2 = (json.loads(r["key"].read_text(encoding="utf-8"))["patients"] for r in (one, two))
    assert k1 == k2, "the same results and notes: the same tokens for every date"
    for f in sorted((tmp_path / "packet_one").glob("Patient_*.md")):
        assert f.read_text(encoding="utf-8") == (tmp_path / "packet_two" / f.name).read_text(encoding="utf-8")
    assert "as in an earlier packet" in (tmp_path / "packet_two" / "README.md").read_text(encoding="utf-8")
    assert "as in an earlier packet" not in (tmp_path / "packet_one" / "README.md").read_text(encoding="utf-8")
    # a key that lacks some dates: those get a letter, the rest keep their numbers
    cut = json.loads(one["key"].read_text(encoding="utf-8"))
    for e in cut["patients"].values():
        e["dates"] = dict(list(e["dates"].items())[::2])
    part = tmp_path / "deid" / "cut_key.json"
    part.write_text(json.dumps(cut), encoding="utf-8")
    three = build_packet(extract, deid, review, pipeline["linkage"], tmp_path / "packet_three", pipeline["settings"],
                         tokens_from=part, **kw)
    k3 = json.loads(three["key"].read_text(encoding="utf-8"))["patients"]
    for label, e in cut["patients"].items():
        assert e["dates"].items() <= k3[label]["dates"].items()
        assert sorted(k3[label]["dates"].values()) == sorted(k1[label]["dates"].values())
        if len(k1[label]["dates"]) > 1:
            assert any(re.fullmatch(r"D\d+[a-z]+", t) for t in k3[label]["dates"])
    blob = "\n".join(f.read_text(encoding="utf-8") for f in (tmp_path / "packet_three").glob("Patient_*.md"))
    assert _years_only(blob)
    with pytest.raises(PacketRefused):
        build_packet(extract, deid, review, pipeline["linkage"], tmp_path / "packet_four", pipeline["settings"],
                     tokens_from=tmp_path / "no_such_key.json", **kw)


def test_packet_is_safe_harbor_shaped(pipeline, seeded_phi, tmp_path):
    deid, extract, review = _copy(pipeline, tmp_path)
    out = tmp_path / "packet"
    buf = io.StringIO()
    res = build_packet(extract, deid, review, pipeline["linkage"], out, pipeline["settings"], passphrase=PASSPHRASE,
                       dictionary=DICT, stdout=buf)
    files = sorted(out.glob("Patient_*.md"))
    assert res["files"] == len(files) == 5 and (out / "README.md").exists()
    blob = "\n".join(f.read_text(encoding="utf-8") for f in files)
    assert "## Relapse (#45–86)" in blob and "**#45 date_onset**" in blob and "[[" in blob and "«" in blob
    assert _years_only(blob), "no date element but the year"
    for f in files:   # D numbers follow date order within each patient
        toks = [(int(y), int(n)) for y, n in re.findall(r"\[\[(\d{4}) D(\d+)", f.read_text(encoding="utf-8"))]
        by_n = dict((n, y) for y, n in toks)
        years = [by_n[n] for n in sorted(by_n)]
        assert years == sorted(years), f.name
    assert not re.search(r"NMO-[A-Z0-9]{4,}|DOC-[A-Z0-9]{6,}", blob), "no study or document IDs"
    for v in {x["value"] for vs in seeded_phi.values() for x in vs if len(x["value"]) >= 4}:
        assert not re.search(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])", blob, re.I), v
    key = res["key"]
    assert key.parent == deid.parent and "NMO-" in key.read_text(encoding="utf-8"), "the key stays in the PHI folder"
    printed = buf.getvalue()
    assert "Wrote 5 patient files" in printed and "NMO-" not in printed and not re.search(r"\d{4}-\d{2}-\d{2}", printed)


def test_whole_notes_packet(pipeline, seeded_phi, tmp_path, monkeypatch):
    """--whole-notes (18.11.26): every note in full, same scrubbing, same tokens and note numbers, same checks."""
    import json

    import nmosd.eval.quality_packet as qp

    monkeypatch.setattr(qp, "NOTES_PART_CHARS", 4000)     # small parts, so the split is exercised
    deid, extract, review = _copy(pipeline, tmp_path)
    out = tmp_path / "packet"
    buf = io.StringIO()
    res = build_packet(extract, deid, review, pipeline["linkage"], out, pipeline["settings"], passphrase=PASSPHRASE,
                       dictionary=DICT, whole_notes=True, stdout=buf)
    con = sqlite3.connect(deid)
    per_patient = [n for (n,) in con.execute("SELECT COUNT(*) FROM documents GROUP BY study_id ORDER BY study_id")]
    con.close()
    assert res["files"] == 5 and len(sorted(out.glob("Patient_?.md"))) == 5
    key = json.loads(res["key"].read_text(encoding="utf-8"))
    assert sorted(len(p["notes"]) for p in key["patients"].values()) == sorted(per_patient)
    total_notes = 0
    for label, entry in key["patients"].items():
        stem = label.replace(" ", "_")
        parts = sorted(out.glob(f"{stem}_notes_*.md"))
        assert len(parts) > 1, "split into parts at note boundaries"
        notes = "".join(p.read_text(encoding="utf-8") for p in parts)
        heads = re.findall(r"^## Note (\d+) \(", notes, re.M)
        assert [int(h) for h in heads] == list(range(1, len(entry["notes"]) + 1)), "every note, in order, once"
        assert notes.count("\n~~~~\n") == 2 * len(heads)
        total_notes += len(heads)
        main = (out / f"{stem}.md").read_text(encoding="utf-8")
        assert "## Notes (whole text)" in main and main.count(f"characters, in {stem}_notes_") == len(heads)
        # the same date tokens in both files, and every token in the key
        tokens = set(re.findall(r"\[\[\d{4} (D\d+)", notes)) | set(re.findall(r"\[\[\d{4} (D\d+)", main))
        assert tokens and tokens <= set(entry["dates"]), "date tokens map back through the key"
        for d, iso in entry["dates"].items():
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso)
            for y in re.findall(rf"\[\[(\d{{4}}) {d}\b", notes + main):
                assert y == iso[:4]
    assert total_notes == sum(per_patient) == res["totals"]["whole notes"]
    blob = "\n".join(f.read_text(encoding="utf-8") for f in out.glob("*.md"))
    assert _years_only(blob), "no date element but the year, whole notes included"
    assert not re.search(r"NMO-[A-Z0-9]{4,}|DOC-[A-Z0-9]{6,}", blob)
    for v in {x["value"] for vs in seeded_phi.values() for x in vs if len(x["value"]) >= 4}:
        assert not re.search(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])", blob, re.I), v
    assert "## Whole notes" in (out / "README.md").read_text(encoding="utf-8")
    printed = buf.getvalue()
    assert "Wrote 5 patient files and" in printed and "NMO-" not in printed
    assert not re.search(r"\d{4}-\d{2}-\d{2}", printed)


def test_whole_notes_refused_when_an_identifier_would_leak(pipeline, seeded_phi, tmp_path, monkeypatch):
    import nmosd.eval.quality_packet as qp
    from nmosd.deid.known_identifiers import IdentifierSet, KnownMatcher

    deid, extract, review = _copy(pipeline, tmp_path)
    link = pipeline["link"]
    matchers = [KnownMatcher(IdentifierSet.from_json(p.get("identifiers", {})), link.get("staff", []),
                             link.get("organizations", [])) for p in link["patients"].values()]
    name = next(x["value"] for vs in seeded_phi.values() for x in vs
                if len(x["value"]) >= 5 and any(m.find(f" {x['value']} ") for m in matchers))
    real = qp._tidy
    monkeypatch.setattr(qp, "_tidy", lambda text: real(text) + f" {name}")   # only the whole notes leak
    with pytest.raises(PacketRefused, match=r"known identifiers \(whole notes\)"):
        build_packet(extract, deid, review, pipeline["linkage"], tmp_path / "packet", pipeline["settings"],
                     passphrase=PASSPHRASE, dictionary=DICT, whole_notes=True, stdout=io.StringIO())
    assert not (tmp_path / "packet").exists(), "nothing written"


def test_id_check_ignores_clinical_words_but_catches_ids(pipeline, tmp_path, monkeypatch):
    """18.11.26: "NMO-SPECTRUM DISORDER" in an all-caps note blocked the real packet. Only real IDs, or text shaped
    exactly like one (NMO- + 6, DOC- + 10 from the ID alphabet), block it; the message shows the shape only."""
    import nmosd.eval.quality_packet as qp

    real = qp._tidy
    words = " PROBLEM LIST: NMO-SPECTRUM DISORDER, NMO-ANTIBODY POSITIVE, NMO-IGG, NMO-RELATED, DOC-HYPERTENSION."
    monkeypatch.setattr(qp, "_tidy", lambda text: real(text) + words)
    deid, extract, review = _copy(pipeline, tmp_path / "ok")
    res = build_packet(extract, deid, review, pipeline["linkage"], tmp_path / "ok" / "packet", pipeline["settings"],
                       passphrase=PASSPHRASE, dictionary=DICT, whole_notes=True, stdout=io.StringIO())
    assert res["files"] == 5 and "NMO-SPECTRUM" in (tmp_path / "ok/packet/Patient_A_notes_01.md").read_text("utf-8")

    con = sqlite3.connect(pipeline["deid"])
    sid, did = con.execute("SELECT study_id, doc_id FROM documents LIMIT 1").fetchone()
    con.close()
    monkeypatch.setenv("NMOSD_LINKAGE_PASSPHRASE", PASSPHRASE)
    for i, leak in enumerate([sid, did, "NMO-ACDEFG", "see DOC-ACDEFGHJKM."]):
        monkeypatch.setattr(qp, "_tidy", lambda text, leak=leak: real(text) + f" {leak}")
        deid, extract, review = _copy(pipeline, tmp_path / f"leak{i}")
        buf = io.StringIO()
        assert run_quality_packet(extract, deid, review, pipeline["linkage"], tmp_path / f"leak{i}" / "packet",
                                  pipeline["settings"], dictionary=DICT, whole_notes=True, stdout=buf) == 1, leak
        msg = buf.getvalue()
        assert "study or document IDs (whole notes)" in msg and "shapes: " in msg, msg
        assert re.search(r"NMO-A{6}|DOC-A{10}|NMO-[A9]{6}|DOC-[A9]{10}", msg), msg
        assert sid not in msg and did not in msg and "ACDEFG" not in msg, "the shape only, never the ID"
        assert not (tmp_path / f"leak{i}" / "packet").exists()


def test_packet_refused_without_signoff_or_for_original_notes(pipeline, tmp_path):
    deid, extract, review = _copy(pipeline, tmp_path / "a", signoff=False)
    with pytest.raises(PacketRefused, match="signed off"):
        build_packet(extract, deid, review, pipeline["linkage"], tmp_path / "p1", pipeline["settings"],
                     passphrase=PASSPHRASE, dictionary=DICT, stdout=io.StringIO())
    deid, extract, review = _copy(pipeline, tmp_path / "b", deidentified=False)
    buf = io.StringIO()
    assert run_quality_packet(extract, deid, review, pipeline["linkage"], tmp_path / "p2", pipeline["settings"],
                              dictionary=DICT, stdout=buf) == 1
    assert "not de-identified" in buf.getvalue() and not (tmp_path / "p2").exists()


def test_packet_refused_when_an_identifier_would_leak(pipeline, seeded_phi, tmp_path, monkeypatch):
    deid, extract, review = _copy(pipeline, tmp_path)
    from nmosd.deid.known_identifiers import IdentifierSet, KnownMatcher

    link = pipeline["link"]
    matchers = [KnownMatcher(IdentifierSet.from_json(p.get("identifiers", {})), link.get("staff", []),
                             link.get("organizations", [])) for p in link["patients"].values()]
    name = next((x["value"] for vs in seeded_phi.values() for x in vs
                 if len(x["value"]) >= 5 and any(m.find(f" {x['value']} ") for m in matchers)), None)
    assert name, "a seeded identifier the leak check knows"
    real = Scrubber.scrub
    monkeypatch.setattr(Scrubber, "scrub", lambda self, text, dt, pivot: real(self, text, dt, pivot) + f" {name}")
    with pytest.raises(PacketRefused, match="known identifiers") as refused:
        build_packet(extract, deid, review, pipeline["linkage"], tmp_path / "packet", pipeline["settings"],
                     passphrase=PASSPHRASE, dictionary=DICT, stdout=io.StringIO())
    assert not (tmp_path / "packet").exists(), "nothing written"
    said = str(refused.value)
    assert "Where the known identifiers are (kind and line only):" in said and " in an evidence passage" in said
    assert name not in said, "the refusal names the kind of identifier and of line, never the text"


def test_an_answer_whose_label_is_a_known_organisation_is_withheld(pipeline, tmp_path, monkeypatch):
    """A codebook choice can name a laboratory or hospital that is on the known-identifier list (19.11.26: it blocked
    a packet). The label is study wording, not patient text, but the answer is withheld, code and label."""
    import nmosd.eval.quality_packet as qp

    deid, extract, review = _copy(pipeline, tmp_path)
    kw = dict(passphrase=PASSPHRASE, dictionary=DICT, stdout=io.StringIO())
    build_packet(extract, deid, review, pipeline["linkage"], tmp_path / "packet_one", pipeline["settings"], **kw)
    blob = "\n".join(f.read_text(encoding="utf-8") for f in (tmp_path / "packet_one").glob("Patient_*.md"))
    code, label = next((c, lab.strip()) for c, lab in re.findall(r"\*\*: proposed (\w+) = ([^;(\n,]+)", blob)
                       if len(lab.strip().split()) >= 2 and "[" not in lab)
    assert f"{code} = {label}" in blob and qp.WITHHELD not in blob
    real = qp.load_linkage

    def with_organisation(path, passphrase):
        link = real(path, passphrase)
        return {**link, "organizations": [*link.get("organizations", []), label]}

    monkeypatch.setattr(qp, "load_linkage", with_organisation)
    buf = io.StringIO()
    res = build_packet(extract, deid, review, pipeline["linkage"], tmp_path / "packet_two", pipeline["settings"],
                       **{**kw, "stdout": buf})
    blob2 = "\n".join(f.read_text(encoding="utf-8") for f in (tmp_path / "packet_two").glob("Patient_*.md"))
    assert qp.WITHHELD in blob2 and label not in blob2
    assert res["totals"]["answers withheld (the label is on the known-identifier list)"] >= 1
    assert "answers withheld" in buf.getvalue()


def test_fast_packet_command_uses_the_fast_results_and_the_reference_tokens(pipeline, tmp_path, monkeypatch):
    """`nmosd quality-packet --fast --reference ...`: the fast test's results file, a review file of its own, and the
    date tokens of the packet the reference answers were written from."""
    import argparse

    from nmosd.cli import cmd_quality_packet
    from nmosd.eval.score_reference import fast_db

    monkeypatch.setenv("NMOSD_LINKAGE_PASSPHRASE", PASSPHRASE)
    deid, extract, review = _copy(pipeline, tmp_path)
    shutil.copy(extract, fast_db(extract))
    quality = tmp_path / "quality"
    first = build_packet(extract, deid, review, pipeline["linkage"], quality / "packet_first", pipeline["settings"],
                         passphrase=PASSPHRASE, dictionary=DICT, stdout=io.StringIO())
    review.unlink()
    reference = quality / "packet_first" / "Claude_reference"
    reference.mkdir()
    (reference / "Patient_A.json").write_text("{}", encoding="utf-8")
    args = argparse.Namespace(extract=str(extract), deid=str(deid), review_db=str(review), dictionary=str(DICT),
                              linkage=str(pipeline["linkage"]), out=str(quality), context=250, passages=2, ner=False,
                              whole_notes=False, fast=True, reference=str(reference))
    assert cmd_quality_packet(args, pipeline["settings"]) == 0
    new = [p for p in quality.glob("packet_*") if p.name != "packet_first"]
    assert len(new) == 1 and "as in an earlier packet" in (new[0] / "README.md").read_text(encoding="utf-8")
    assert fast_db(review).exists() and not review.exists(), "the full review file is not opened"
    k1 = json.loads(first["key"].read_text(encoding="utf-8"))["patients"]
    k2 = json.loads((deid.parent / f"quality_packet_key_{new[0].name}.json").read_text(encoding="utf-8"))["patients"]
    assert k1 == k2
    for f in sorted((quality / "packet_first").glob("Patient_*.md")):
        assert f.read_text(encoding="utf-8") == (new[0] / f.name).read_text(encoding="utf-8")


def test_passages_are_cut_from_the_scrubbed_note_and_never_through_a_word_or_token():
    """19.11.26: a packet was refused for a staff name in a passage although the whole notes had passed. A passage
    was cut out of the note first and scrubbed afterwards: a cut or a «» mark could leave half a word, and a name
    whose "Dr." was cut off was not redacted. Passages are now windows of the scrubbed note."""
    from nmosd.config import load_settings
    from nmosd.eval.quality_packet import _passage

    s = Scrubber(load_settings(REPO / "config/settings.example.yaml"))
    dt = DateTokens(["2019-03-14"])
    text = ("The patient was seen on 03/14/2019 for a relapsing course with longstanding numbness. "
            "She was admitted and later discharged home; phone 617-555-1234 was called twice. ") * 3
    out, applied = s.scrub_mapped(text, dt, 2023)
    assert out == s.scrub(text, dt, 2023) and "[[2019 D01]]" in out and "555" not in out
    starts = [t[0] for t in applied]
    words = set(re.findall(r"[A-Za-z]+", out))
    for m in re.finditer(r"relaps|admit|numb|03/14/2019|617-555", text):
        for context in (3, 7, 12, 20, 33):
            got = _passage(out, applied, starts, m.start(), m.end(), context)
            assert got.count("«") == 1 and got.count("»") == 1
            bare = re.sub(r"\[\[[^\]]*\]\]|\[[^\[\]]*\]", " ", got.replace("«", "").replace("»", ""))
            assert set(re.findall(r"[A-Za-z]+", bare)) <= words, "no half words at the cuts or the marks"
            assert got.count("[") == got.count("]"), "no token or redaction cut through"
            assert _years_only(got) and "555" not in got
    quote = text.index("03/14/2019")
    assert "«[[2019 D01]]»" in _passage(out, applied, starts, quote + 3, quote + 5, 10), "a quote inside a date"
    assert _passage(out, applied, starts, None, None, 10) == ""
