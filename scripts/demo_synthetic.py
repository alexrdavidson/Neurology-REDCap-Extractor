#!/usr/bin/env python3
"""End-to-end run on SYNTHETIC data only (no PHI), in a folder outside the repository.

Quick demo (a deterministic stand-in replaces the LLM; about a minute):

    python scripts/demo_synthetic.py [--out DIR]

Full rehearsal with the local LLM (Ollama), using the ordinary `python -m nmosd` commands:

    python scripts/demo_synthetic.py --out DIR --prepare-only [--model gemma3:12b]

This builds the synthetic data and DIR/settings.yaml, which points every path into DIR, then prints the
commands to run next: stage, deid, leak-test, extract (Ollama), reconcile, review, export, reidentify and
evaluate. Every command carries `--settings DIR/settings.yaml`, so none of them touches config/settings.yaml
or real data. The linkage passphrase comes from NMOSD_SYNTHETIC_PASSPHRASE, never from the real
NMOSD_LINKAGE_PASSPHRASE.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nmosd.config import load_settings
from nmosd.deid.leak_test import run_leak_test
from nmosd.deid.pipeline import run_deid
from nmosd.extract.run import run_extraction
from nmosd.extract.stage2_reconcile import run_reconcile
from nmosd.ingest.load_staging import stage
from nmosd.synth.build import build

PASS = "synthetic demo passphrase"
PASS_ENV = "NMOSD_SYNTHETIC_PASSPHRASE"   # deliberately not the real NMOSD_LINKAGE_PASSPHRASE


def demo_settings(root: Path, syn: Path, export_date: str) -> dict:
    """Settings for the synthetic run: every path inside `root`; merged over settings.example.yaml."""
    work, deid = root / "work", root / "deid"
    paths = {"source": syn / "synthetic_rpdr.sqlite", "phi_dir": work, "empi_list": syn / "pilot_empis.txt",
             "identifiers": syn / "identifiers.yaml", "staging_db": work / "staging" / "staging.sqlite",
             "pilot_selection_db": work / "staging" / "pilot_selection.sqlite", "deid_db": deid / "deid.sqlite",
             "linkage": work / "linkage" / "linkage.sqlite.enc",
             "deid_report": work / "reports" / "deid_report.html", "extract_db": deid / "extract.sqlite",
             "review_db": deid / "review.sqlite", "export_dir": deid / "export",
             "quality_packet_dir": root / "quality_review",
             "reidentified_dir": work / "reidentified"}
    return {"paths": {k: str(v) for k, v in paths.items()} | {"redcap_template": None},
            "source": {"export_date": export_date},
            "deid": {"passphrase_env": PASS_ENV},
            # Same date style as the project's REDCap export (profile of 11.11.26).
            "export": {"date_format": "M/D/YYYY"}}


def write_settings(root: Path, syn: Path, export_date: str) -> Path:
    path = root / "settings.yaml"
    path.write_text("# SYNTHETIC data only (scripts/demo_synthetic.py). Every path points inside this folder.\n"
                    + yaml.safe_dump(demo_settings(root, syn, export_date), sort_keys=False), encoding="utf-8")
    settings = load_settings(path)
    for key in settings.get("paths") or {}:          # a new key in settings.example.yaml must not leak out
        p = settings.path(key)
        if p is not None and not p.resolve().is_relative_to(root.resolve()):
            raise SystemExit(f"demo settings: paths.{key} points outside {root}; add it to demo_settings()")
    return path


def _q(p: Path | str) -> str:
    return f'"{p}"' if os.name == "nt" else shlex.quote(str(p))


def next_steps(root: Path, settings_path: Path, model: str | None) -> str:
    """The commands for a full run, from inside `root` (relative paths keep them short; run from anywhere
    else, `--settings settings.yaml` is not found and the command stops instead of using real settings)."""
    s = load_settings(settings_path)
    model_name = model or s.get("llm.model")
    nm = f"python -m nmosd --settings {settings_path.name}"
    m = f" --model {model_name}" if model else ""
    sep = "\\" if os.name == "nt" else "/"
    export_csv = sep.join(("deid", "export", "redcap_import_deidentified.csv"))
    rmap, gold = sep.join(("synthetic", "record_map.csv")), sep.join(("synthetic", "gold_redcap.csv"))
    env = (f'$env:{PASS_ENV} = "{PASS}"' if os.name == "nt" else f"export {PASS_ENV}={shlex.quote(PASS)}")
    return "\n".join([
        f"Synthetic data and settings are ready in {root}",
        "",
        "Before you start: open the Ollama app (or run `ollama serve` in a terminal of its own and leave it",
        f"running; it never finishes) and `ollama pull {model_name}`.",
        "Then run these in order, in another terminal with the project's virtual environment active.",
        "The command lines carry no comments (zsh does not treat a pasted # as one), so they copy safely:",
        "",
        f"  cd {_q(root)}",
        f"  {env}",
        f"  {nm} stage",
        f"  {nm} deid",
        f"  {nm} leak-test",
        f"  {nm} extract --dry-run{m}",
        f"  {nm} extract --limit-docs 10{m}",
        f"  {nm} extract{m}",
        f"  {nm} reconcile",
        f"  {nm} review",
        f"  {nm} export",
        f"  {nm} reidentify --export {export_csv} --record-map {rmap} --add-identifiers",
        f"  {nm} evaluate --gold {gold} --predicted {export_csv} --linkage {s.path('linkage').relative_to(root)} "
        f"--record-map {rmap} --extract {s.path('extract_db').relative_to(root)} --label {model_name} "
        f"--report eval_{model_name.replace(':', '_')}.md",
        "",
        "What to expect:",
        "  - deid runs the NER models and takes a few minutes.",
        '  - leak-test must print "Known-identifier hits: 0 (PASS)".',
        "  - extract --dry-run only counts the LLM calls.",
        '  - extract --limit-docs 10 is a quick check of 10 calls: read its "Done:" line before going on.',
        "  - extract then does the rest and keeps those 10. It prints an ETA and is safe to stop and re-run.",
        "  - review serves the app at http://127.0.0.1:8501. Press Ctrl+C in its terminal when you have finished.",
        "",
        "In the review app, start every patient with the diagnosis check. The synthetic answers: P3 has MOGAD and",
        "should be excluded; the other four have NMOSD with more than 12 months of follow-up.",
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "nmosd_synthetic_demo")
    ap.add_argument("--prepare-only", action="store_true",
                    help="build the synthetic data and settings.yaml, then print the commands for a full run "
                         "with the local LLM")
    ap.add_argument("--model", help="Ollama model for the printed extract commands (default: llm.model)")
    a = ap.parse_args()
    root = a.out.expanduser().resolve()
    syn = root / "synthetic"
    if a.prepare_only and any((root / d).exists() for d in ("deid", "work")):
        print(f"{root} already holds a run (its review decisions would be mixed with a new one). "
              "Choose a new --out folder, or delete this one first.", file=sys.stderr)
        return 1
    manifest = build(syn, with_csv=False)
    settings_path = write_settings(root, syn, manifest["export_date"])
    if a.prepare_only:
        text = next_steps(root, settings_path, a.model)
        (root / "next_steps.txt").write_text(text + "\n", encoding="utf-8")
        print(text)
        print(f"\n(These steps are also saved in {root / 'next_steps.txt'})")
        return 0

    settings = load_settings(settings_path)
    for key in ("deid_db", "linkage"):
        if settings.path(key).exists():
            settings.path(key).unlink()
    stage(settings.path("source"), settings.path("empi_list"), settings.path("staging_db"), settings)
    run_deid(settings.path("staging_db"), settings.path("deid_db"), settings.path("linkage"),
             settings.path("identifiers"), settings, use_ner=False, passphrase=PASS)
    run_leak_test(settings.path("deid_db"), settings.path("linkage"), settings, use_ner=False, passphrase=PASS)
    run_extraction(settings.path("deid_db"), settings.path("extract_db"), settings, llm="synthetic-oracle",
                   annotations=syn / "annotations.json")
    run_reconcile(settings.path("extract_db"), settings.path("deid_db"), settings)
    print("\nOpen the review app on the synthetic result with:\n"
          f"  python -m nmosd --settings {_q(settings_path)} review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
