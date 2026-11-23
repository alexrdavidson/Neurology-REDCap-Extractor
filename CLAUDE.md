# NMOSD Extractor — rules for Claude Code
- This repo processes PHI. NEVER read, print or query real patient data or any file under ../data/,
  data/, outputs/, or any path from config/settings.yaml. Use tests/fixtures/synthetic/ only.
- Scripts run on real data must output metadata/aggregates only. No values, text, IDs or dates.
- Runtime network access: only Ollama at 127.0.0.1. No cloud models (reject model names containing "cloud").
- Target runtime is Windows 11 (Python 3.11+); development may be on macOS. Use pathlib; no
  shell-specific code; no hard-coded paths.
- Map REDCap choices by CODE, never by label or list position.
- Never let the LLM decide which patient a document belongs to; that's fixed upstream.
- Follow BUILD_PLAN.md phase by phase. Run `pytest` before declaring a phase done.

## Project-specific notes
- Real data for this project lives under `~/Documents/Private Documents/nmosd_extractor/` (outside the
  repo). Never list, open, export or query anything there. The user runs the commands on it.
- ONE exception (the user approved it on 16.11.26, on condition that it is HIPAA compliant; BUILD_PLAN §0 rule 8):
  when the user asks for a quality assessment, Claude may read the files of a quality packet written by
  `nmosd quality-packet` (`Patient_*.md` and `README.md` in `~/Documents/NMOSD quality review/packet_*/`, or the
  folder the user names). The packet is built to HIPAA Safe Harbor (no date element but the year, no IDs, leak-checked).
  On 18.11.26 the user also approved `--whole-notes` packets, so Claude can work out what every REDCap field should
  have been: `Patient_*_notes_*.md` hold every note in full, scrubbed and checked the same way, and may be read too.
  Only those files: never the packet key, the databases, or anything under `~/Documents/Private Documents/`.
  Quote packet text, and give the per-field answers worked out from it, only in replies to the user or in files in
  that packet folder (or the session scratchpad), never in the repo, tests, logs or memory.
- Synthetic data: `python scripts/make_synthetic.py --out <dir>` (deterministic from `--seed`).
- Tests: `.venv/bin/pytest`; lint: `.venv/bin/ruff check .`
- Tests must never load `config/settings.yaml`; pass explicit paths or use `tests/conftest.py` fixtures.
