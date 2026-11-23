"""Integration with the real transformer de-identification models (synthetic text only).

Skipped unless transformers/torch are installed and the weights are in the local Hugging Face cache
(`python scripts/download_models.py`). Runs fully offline.
"""

from __future__ import annotations

import io
import itertools
import os
import re
import sqlite3

import pytest

from nmosd.config import load_settings
from nmosd.deid.ner_models import NERUnavailable, build_ensemble
from tests.conftest import PASSPHRASE

pytestmark = pytest.mark.ner


@pytest.fixture(scope="module")
def ensemble():
    pytest.importorskip("transformers")
    pytest.importorskip("torch")
    try:
        return build_ensemble(load_settings(os.environ["NMOSD_SETTINGS"]))
    except NERUnavailable as e:
        pytest.skip(str(e))


def test_models_load_offline_and_label_unified(ensemble):
    text = ("Ms. Rosalind Quillfeather is a 34 y.o. woman seen by Dr. Theodora Blackwood at St. Brendan's Medical "
            "Center on 3/4/2019. Her husband Bartholomew drove her. Call (978) 555-0193. MRN 4417263. "
            "She has AQP4-IgG positive NMOSD treated with rituximab; Lhermitte sign present.")
    res = ensemble.predict(text)
    assert set(res.per_model) == {m.name for m in ensemble.models}
    found = {(s.label, text[s.start:s.end]) for s in res.spans}
    labels = {lab for lab, _ in found}
    assert labels <= {"PATIENT", "STAFF", "RELATIVE", "AGE", "DATE", "PHONE", "ID", "EMAIL", "URL", "LOCATION",
                      "HOSPITAL", "ORGANIZATION", "OTHER"}
    joined = " | ".join(v for _, v in found)
    for must in ["Quillfeather", "Blackwood", "555-0193", "3/4/2019"]:
        assert must in joined, (must, found)
    for s in res.spans:
        assert text[s.start:s.end] == text[s.start:s.end].strip()


def test_windows_fit_token_limit_and_cover_text(ensemble, synthetic_tables):
    texts = [r["Report_Text"] for t in ("VisitNote", "Discharge_Summaries") for r in synthetic_tables[t]]
    dense = "\r\n".join(f"{i:02d}/{i % 28 + 1:02d}/19   1{i % 9}8/7{i % 9}   AQP4 1:{i}00" for i in range(400))
    for m in ensemble.models:
        limit = m._max_tokens()
        for text in [max(texts, key=len), dense]:
            wins = m.token_windows(text)
            assert wins[0][0] == 0 and wins[-1][1] == len(text)
            assert all(b[0] < a[1] for a, b in itertools.pairwise(wins)), "consecutive windows overlap"
            for s, e in wins:
                n = len(m._pipe.tokenizer(text[s:e])["input_ids"])
                assert n <= limit, (m.name, n, limit)


def test_pipeline_with_real_ner_on_synthetic(ensemble, synthetic_dir, tmp_path, seeded_phi):
    from nmosd.deid.leak_test import run_leak_test
    from nmosd.deid.linkage import load_linkage
    from nmosd.deid.pipeline import run_deid
    from nmosd.ingest.load_staging import stage

    settings = load_settings(os.environ["NMOSD_SETTINGS"])
    one = tmp_path / "one.txt"
    one.write_text("100004417\n100009912\n")
    staging = tmp_path / "work" / "staging.sqlite"
    stage(synthetic_dir / "synthetic_rpdr.sqlite", one, staging, settings, stdout=io.StringIO())
    out, link = tmp_path / "deid" / "deid.sqlite", tmp_path / "work" / "l" / "link.enc"
    buf = io.StringIO()
    assert run_deid(staging, out, link, synthetic_dir / "identifiers.yaml", settings, use_ner=True, ensemble=ensemble,
                    passphrase=PASSPHRASE, stdout=buf) == 0
    con = sqlite3.connect(out)
    blob = "\n".join(r[0] for r in con.execute("SELECT text FROM documents"))
    con.close()
    for empi in ("100004417", "100009912"):
        for it in seeded_phi[empi]:
            v = it["value"]
            flags = 0 if v.lower() in {"hope"} else re.I
            assert not re.search(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])", blob, flags), it
    for term in ["rituximab", "optic neuritis", "AQP4-IgG", "Uhthoff", "Mayo Clinic", "PLEX", "EDSS", "hepatitis B"]:
        assert term in blob, term
    assert run_leak_test(out, link, settings, use_ner=False, passphrase=PASSPHRASE, stdout=io.StringIO()) == 0
    assert load_linkage(link, PASSPHRASE)["patients"]
    assert "NER spans per model" in buf.getvalue()
