"""Shared fixtures. Tests only ever touch synthetic data generated into a temp directory.

Tests must never load config/settings.yaml: NMOSD_SETTINGS is pointed at the committed example.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
os.environ["NMOSD_SETTINGS"] = str(REPO / "config" / "settings.example.yaml")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
csv.field_size_limit(sys.maxsize)

from nmosd.synth.build import build  # noqa: E402

SEED = 20261109


@pytest.fixture(scope="session")
def synthetic_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("synthetic")
    build(out, seed=SEED, with_csv=True)
    return out


@pytest.fixture(scope="session")
def synthetic_db(synthetic_dir) -> Path:
    return synthetic_dir / "synthetic_rpdr.sqlite"


@pytest.fixture(scope="session")
def synthetic_tables(synthetic_db) -> dict[str, list[dict]]:
    con = sqlite3.connect(synthetic_db)
    con.row_factory = sqlite3.Row
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    out = {}
    for n in names:
        q = '"' + n.replace('"', '""') + '"'
        out[n] = [dict(r) for r in con.execute(f"SELECT * FROM {q}")]
    con.close()
    return out


@pytest.fixture(scope="session")
def seeded_phi(synthetic_dir) -> dict:
    return json.loads((synthetic_dir / "seeded_phi.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def annotations(synthetic_dir) -> list[dict]:
    return json.loads((synthetic_dir / "annotations.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def pilot_empis(synthetic_dir) -> list[str]:
    return (synthetic_dir / "pilot_empis.txt").read_text(encoding="utf-8").split()


NOTE_TABLES = ["VisitNote", "ProgressNote", "Radiology", "Pathology", "Discharge_Summaries", "HistoryAndPhysical"]


PASSPHRASE = "correct horse battery staple"


@pytest.fixture(scope="session")
def pipeline(synthetic_dir, tmp_path_factory):
    """Full synthetic chain: stage → deid (rule layers) → extract (synthetic oracle) → reconcile."""
    import io

    from nmosd.config import load_settings
    from nmosd.deid.linkage import load_linkage
    from nmosd.deid.pipeline import run_deid
    from nmosd.extract.run import run_extraction
    from nmosd.extract.stage2_reconcile import run_reconcile
    from nmosd.ingest.load_staging import stage

    settings = load_settings(os.environ["NMOSD_SETTINGS"])
    root = tmp_path_factory.mktemp("pipeline")
    p = {"root": root, "settings": settings, "staging": root / "work" / "staging.sqlite",
         "deid": root / "deid" / "deid.sqlite", "linkage": root / "work" / "linkage" / "link.enc",
         "extract": root / "deid" / "extract.sqlite", "review": root / "deid" / "review.sqlite",
         "synthetic": synthetic_dir}
    stage(synthetic_dir / "synthetic_rpdr.sqlite", synthetic_dir / "pilot_empis.txt", p["staging"], settings,
          stdout=io.StringIO())
    run_deid(p["staging"], p["deid"], p["linkage"], synthetic_dir / "identifiers.yaml", settings, use_ner=False,
             passphrase=PASSPHRASE, stdout=io.StringIO())
    buf = io.StringIO()
    run_extraction(p["deid"], p["extract"], settings, llm="synthetic-oracle",
                   annotations=synthetic_dir / "annotations.json", stdout=buf)
    p["extract_stdout"] = buf.getvalue()
    run_reconcile(p["extract"], p["deid"], settings, stdout=io.StringIO())
    p["link"] = load_linkage(p["linkage"], PASSPHRASE)
    return p
