# Neurology REDCap Extractor

Local-LLM extraction of REDCap values for neurological conditions from RPDR clinical notes. A clinician reviews
every proposed value; approved values are exported as a REDCap import file. The app opens on a choice of condition
(`config/conditions.yaml`). **NMOSD** is fully built, for the MGB *NMOSD database* REDCap project (PID 47765: all
303 codebook fields). MOGAD, MS, myasthenia gravis, autoimmune encephalitis, epilepsy, Parkinson's disease, ALS/MND,
stroke, dementia and migraine are listed as *not set up yet*. Built phase by phase from [BUILD_PLAN.md](BUILD_PLAN.md).
The Python package and command are still called `nmosd`.

**This project processes PHI.** Read [CLAUDE.md](CLAUDE.md) and the data-safety rules in BUILD_PLAN §0. Real data
lives outside the repo; every command that writes PHI refuses paths inside it; scripts run on real data print
counts only; the only runtime network access is Ollama on 127.0.0.1.

## For users: the guided app

Clinicians use the **guided app**, in its own window called *Neurology REDCap Extractor* (served on 127.0.0.1 only):

- **Windows**: run `Install Neurology REDCap Extractor.bat` once; it makes the desktop icon.
- **Mac**: `.venv/bin/pip install -e ".[ner,app,window]"`, then `.venv/bin/python scripts/make_mac_app.py` once; it
  makes `Neurology REDCap Extractor.app` (no Terminal window). Or double-click `Start Neurology REDCap Extractor.command`.
- **Any terminal**: `python -m nmosd app --window` (without `--window`: in the browser).

It opens on **"Which condition are you working on?"**. A condition that is set up opens the guide, which walks
through every step below and runs the long ones in the background; the others explain what is needed to set them up.
Each condition keeps its own settings file in `config/` (git-ignored; NMOSD's is `config/settings.yaml`), so each
has its own private data folder. The window is pywebview (`[window]` extra); without it the app opens in an
Edge/Chrome app window, or else the browser. Closing the window closes the app; background jobs carry on.
User guide: [docs/USER_GUIDE.md](docs/USER_GUIDE.md). To make the zip for another computer:
`python scripts/make_release.py` (no settings, no data).

### Adding a condition

1. List it in `config/conditions.yaml` (`status: planned` shows it as not set up yet).
2. Its REDCap data dictionary, field groups (which fields come from the notes and which from structured data), the
   diagnosis check, and the reconcile/export rules. Today these are NMOSD's (`config/fields/`, `config/eligibility.yaml`,
   `config/redcap/`, `src/nmosd/redcap/sources.py`, `src/nmosd/extract/stage2_*.py`): making them per condition is
   the next step of this work.
3. Pilot it on a few patients against a clinician's reference, then set `status: ready`.

## Pipeline

```
RPDR .accdb ──inspect──► metadata only
     │
   stage ──► staging.sqlite (identifiable; one documents table, duplicates collapsed, P/F policy, relevance class)
     │
   deid ──► deid.sqlite (study IDs, shifted dates, surrogate tokens) + linkage.sqlite.enc (encrypted, separate folder)
     │      header layer → known identifiers → patterns → NER ensemble (OBI + Stanford, union) → date shifter
     │      leak-test (must be 0) · deid-report (100% human review) · deid-signoff
     │
   extract ──► evidence (stage 1: boilerplate + copy-forward removed, prefilter, section chunks, Ollama, quote check)
     │
   reconcile ──► proposals (diagnosis check, relapse/treatment episodes, antibody tests, single-value fields)
     │
   review (Streamlit, 127.0.0.1) ──► step 1 diagnosis check (NMOSD + ≥12 months follow-up), then append-only reviews
     │
   export ──► REDCap CSV in the project's own export layout (study-ID space; included patients only)
     │        ──reidentify──► final import file (approved environment)
     │
   evaluate ──► aggregate metrics against existing REDCap abstraction
```

## Commands

`python -m nmosd <command>`: `inspect`, `benchmark`, `stage`, `select-pilot`, `suggest-identifiers`, `deid`,
`leak-test`, `deid-report`, `deid-signoff`, `schemas`, `extract`, `reconcile`, `review`, `export`, `reidentify`,
`evaluate`, `profile-redcap-csv` (metadata-only layout of a REDCap export), `app` (the guided app; `--window` for its own window), `download-models` and `pull-model` (one-time setup downloads). Paths default to `config/settings.yaml` (git-ignored; template in `config/settings.example.yaml`).
Step-by-step instructions for real data: [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Development

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # synthetic data only, generated into a temp folder
.venv/bin/ruff check .
python scripts/make_synthetic.py --out tests/fixtures/synthetic --csv    # inspect the synthetic dataset
python scripts/demo_synthetic.py                                        # full synthetic run + review app
```

Optional extras: `[windows]` (pyodbc), `[ner]` (transformers + torch), `[review]` (streamlit), `[app]` (streamlit, psutil, truststore: the guided app), `[window]` (pywebview: the app's own window), `[bench]` (psutil).

## Layout

| Path | What |
|---|---|
| `config/` | the conditions list, settings template, doc types, boilerplate, sections, de-id patterns/allow-list/label maps, field specs, REDCap maps |
| `src/nmosd/ingest/` | Access reader (pyodbc / mdbtools / sqlite), inspector, column map, staging, pilot selection |
| `src/nmosd/deid/` | study IDs, linkage key, date shift, headers, known identifiers, patterns, NER ensemble, surrogates, pipeline, leak test, report |
| `src/nmosd/redcap/` | dictionary parser, schema generation, export, re-identification |
| `src/nmosd/extract/` | Ollama client, benchmark, dedupe, prefilter, chunking, prompts, stage 1, quote check, stage 2, runner |
| `src/nmosd/review/` | review store (append-only) and Streamlit app |
| `src/nmosd/eval/` | gold comparison and report |
| `src/nmosd/synth/` | synthetic RPDR-like generator (5 fictional patients + 1 control) |
| `docs/` | runbook, date-shifting rules |
| `assets/icon/` | app icon (`scripts/make_icon.py` rebuilds it; `scripts/make_desktop_shortcut.py` makes the Windows shortcut, `scripts/make_mac_app.py` the Mac app) |

## Status

See the last section of the build summary in the Claude Code session, or the phase tests:
`tests/test_phase0_scaffold.py` … `tests/test_phase7_9_review_export_eval.py`.
