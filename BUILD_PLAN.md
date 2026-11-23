
This file is the build brief for Claude Code. Work through the phases in order. Each phase ends with acceptance criteria: don't start the next phase until they pass. Commit at the end of each phase.

---

## 0. Read this first: data-safety rules (non-negotiable)

This project processes **protected health information (PHI)**. Anything Claude Code reads (files, command output) is sent to Anthropic's API, so:

1. **Never read, open, print, `cat`, `head`, grep, or query any real patient data**, identifiable or de-identified. That covers the `.accdb` file, any CSV/SQLite/Parquet derived from it, de-identification outputs, extraction outputs, and review databases.
2. Real data lives **outside the repository**, e.g. `../data/` (sibling of the repo) or a path set in `config/settings.yaml`. Never copy real data into the repo.
3. Develop and test **only against synthetic data** that you generate (`tests/fixtures/synthetic/`).
4. Scripts that the humans run on real data must print **only metadata and aggregates**: counts, column names, types, lengths, timings, accuracy metrics. Never print values, note text, names, IDs or dates. Add a `--i-understand-this-prints-phi` flag (default off) to any debug option that would print content.
5. If a task seems to need a look at real data, **stop and ask the user** to run a script and paste back a metadata-only summary.
6. Logs must not contain note text or identifiers. Log document IDs (study-generated, not source IDs) and counts only.
7. Everything must work **offline** after setup. The only network calls allowed at runtime go to Ollama on `http://127.0.0.1:11434`.
8. **One exception to rule 1 (16.11.26).** The user approved Claude assessing the quality of the proposals, on condition that it is HIPAA compliant. Claude may read a **quality packet** that `nmosd quality-packet` wrote, and nothing else. The packet:
    - is built to the HIPAA Safe Harbor standard on top of the signed-off de-identification;
    - holds evidence passages only, never whole notes;
    - keeps no date element but the year (dates become tokens such as `[[2019 D05]]`);
    - shows ages over 89 as 90+, uses no study or document IDs (Patient A, Note 1), and scrubs residual patterns;
    - is searched for every patient's known identifiers, and nothing is written if any check fails.

   Whether sharing it is permitted is decided by MGB (privacy office / IRB), not by the tool. The key back to study IDs stays in the PHI location. Claude reads only `Patient_*.md` and `README.md` in the packet folder (default `~/Documents/NMOSD quality review/`), never the key or anything else. It quotes packet text only in replies to the user, never into the repo, tests, logs or memory.

   **Whole notes (18.11.26).** The user asked Claude to work out what every REDCap field should have been for the 5 pilot patients, which needs whole notes rather than passages, and approved `nmosd quality-packet --whole-notes`. It adds `Patient_A_notes_01.md`, ... with the whole text of every note in date order, through the same scrubbing (`--ner` recommended) and the same final checks; the same date tokens and note numbers as `Patient_A.md`. The key now also maps each date token back to its date (PHI location only), so answers given as tokens can be mapped back. Claude may read those files as well; its per-field answers go only in replies to the user or in files in the packet folder (or the session scratchpad), never in the repo, tests, logs or memory. MGB's view on sharing whole notes still applies.

### Create these guardrail files in Phase 0

**`CLAUDE.md`** (repo root):

```markdown
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
```

**`.claude/settings.json`**: defence in depth only. Deny rules have had enforcement bugs, so the real protection is keeping data outside the repo (rule 2).

```json
{
  "permissions": {
    "deny": [
      "Read(../data/**)",
      "Read(./data/**)",
      "Read(./outputs/**)",
      "Read(**/*.accdb)",
      "Read(**/*.mdb)",
      "Read(**/*.sqlite)",
      "Read(**/*.db)",
      "Read(./config/settings.yaml)",
      "Bash(mdb-export:*)",
      "Bash(sqlite3:*)"
    ]
  }
}
```

**`.gitignore`**: `data/`, `outputs/`, `*.accdb`, `*.mdb`, `*.sqlite`, `*.db`, `config/settings.yaml`, `*.key`, `linkage*`, `.venv/`.

---

## 1. Context

**Goal.** Extract values for the MGB _NMOSD database_ REDCap project (PID 47765) from de-identified clinical notes using a local LLM. A clinician reviews every proposed value, and approved values are exported as a REDCap import file. This pilot covers 5 patients and ~20 fields. A later version will move to Snowflake Cortex + Streamlit in Snowflake, so keep the LLM client and storage behind small interfaces.

**Source data: confirmed schema (from the metadata-only inspector, 09.11.26).** A Microsoft Access database (`.accdb`, ~48 MB) from MGB **RPDR**, covering **32 patients**. Read it with `mdb-export` (macOS) or pyodbc (Windows). Tables:

|Table|Rows|Patients|Columns|Notes|
|---|---|---|---|---|
|`Demographics`|32|32|`EMPI, Gender_Legal_Sex, Date_of_Birth, Age, Sex_At_Birth, Gender_Identity, Language, Language_group, Race1, Race2, Race_Group, Ethnic_Group, Marital_status, Religion, Is_a_veteran, Zip_code, Country, Vital_status, Date_Of_Death`|No name, no MRN. `Race2` and `Date_Of_Death` are empty|
|`VisitNote`|432|32|`EMPI, Report_Number, Report_Date_Time, Report_Description, Report_Status, Report_Type, Report_Text` (same 7 for all note tables)|Text median ~27k chars, max ~101k. Types: Office Visit, Telemedicine, ED Provider Note, Patient Email, MyChart Renewal, Documentation, Outreach, Procedure, Nurse Triage|
|`ProgressNote`|870|32|″|Median ~4.5k chars, max ~47k. Includes Nursing, Lactation, Periop Nursing, Event|
|`Radiology`|164|23|″|Only **116 distinct texts**, so the same report sits under several report numbers. ~70 relevant (MRI brain / C / T / L / total spine / face(orbits) / MRA)|
|`Pathology`|37|18|″|Mostly irrelevant. **Flow cytometry (7)** may hold CD19/CD20|
|`Discharge_Summaries`|12|9|″|Median ~34k chars, max ~51k|
|`HistoryAndPhysical`|11|8|″||
|`<-- Click on "Forms"…`|0|–|`Field1`|Access placeholder; skip it|

Facts the code must handle:

- Every `Report_Text` ends with `[report_end]`; strip it.
- `Report_Date_Time` and `Date_of_Birth` are **text in more than one format** (lengths 17 vs 19–22 across tables), so write a tolerant parser with tests for each format.
- `Report_Status`: F (final), P (preliminary), R (probably revised). Keep F and R, and drop P when a final version exists; make this configurable.
- The first letter of `Report_Type`/`Report_Description` encodes the hospital (B, M, F, W, N, S, V). Keep it as a `site` column.
- **Not in this export:** `Med`, `Lab`, `Lno`, `Mrn`, `Con`, `Enc`. So medications, antibody results and B-cell counts come from note text unless the user obtains those tables later. Design the ingest to pick them up automatically if they appear.

### 1b. Inside the note text (from the metadata-only profile, 09.11.26)

All findings below come from masked shapes and counts; Claude has seen no real text. Build the synthetic data to mimic them.

**Header blocks (most notes in every table except Radiology/Pathology).** CRLF line endings throughout; many lines are fixed-width, space-padded columns.

- **Discharge / H&P / Progress notes:**
    - Line 1: a confidentiality banner ending in a quoted file name, like `***This report contains … , 'NNNNNN.txt'.`
    - Line 2: `<SITE> <Clinic/Unit> <Lastname>, <Firstname> [<Initial>]`
    - Line 3: `<NN> <Years> <Sex?> MRN: <digits>, DOB: <m/dd/yyyy>, <…>`
    - Line 4: `<Town> MA <ZIP>-<ZIP4> Account #: <digits>`
    - The labels are inferred from shapes. The user will confirm them; make the header parser pattern-driven and configurable.
- **VisitNote:**
    - Line 1: banner.
    - Line 2: `<Lastname>, <Firstname> [“<Preferred name>”] (MR # <digits>) DOB: <mm/dd/yyyy>`; some variants use `(MRN <digits>)` or omit the DOB.
    - Line 3: provider name.
    - Line 4: `<m/dd/yyyy h:mm AM> <Department/Provider>`
- **Pathology:**
    - `Accession Number: <S19-12345 style> Report Status: …`
    - `Type: …`
    - Lab address and phone number lines.
- **Radiology:**
    - Exam title line(s).
    - A reason or history line.
    - Header fields with dates in a month-abbreviation form.
- **Use for de-identification:**
    1. Remove the header block by rule.
    2. **Harvest the patient's names (including quoted preferred names), MRNs, DOB, town and ZIP from the header into `identifiers.yaml`.** This replaces the missing `Con`/`Mrn` tables. The user confirms the harvested list, and it must never be printed.

**Date formats.**

- `Report_Date_Time` is `mm/dd/yy hh:mm:ss` (two-digit year) in Visit, Progress and H&P, and `m/d/yyyy h:mm:ss AM` in Discharge, Pathology and Radiology.
- `Date_of_Birth` is `mm/dd/yy hh:mm:ss` (two-digit year): **resolve the century using the `Age` column**.
- In-text dates are mostly `m/d/yyyy` and `mm/dd/yyyy`. Short forms (`mm/dd/yy`, `m/dd`, `mm/yy`, `m/d`) and `Month yyyy` are common.

**Things that look like dates but aren't. The date shifter must not touch these, and must have tests for each:**

- visual acuity `20/20`, `20/400`, `CF`, `HM`
- strength `5/5`, `4+/5`
- BP `120/80`, also in tables headed `BP Readings from Last N Encounters`
- reflexes `2+`
- doses `1/2 tab`
- ratios and titres `1:100`
- spinal levels `C2-C5`, `T4-T9`
- ranges `3-4`
- EDSS-style scores

Ambiguous short forms like `3/19` (March 19th or March 2019) are shifted by a rule that works for both readings, or flagged for review. Document the chosen rule.

**Section headings that occur across many patients (use them for chunking and section filtering):**

- **Radiology:** `FINDINGS` and `IMPRESSION` (in ~99% of reports); level headings such as `CERVICAL SPINE`, `THORACIC SPINE`, `BRAIN`, `Findings by level`; `TECHNIQUE`.
- **Neurology progress/visit notes:**
    - `HPI` / `History of present illness`, `INTERVAL EVENTS`, `Subjective`, `Past Medical History`, `Past History`
    - `Medications` / `Current medication regimen`
    - `PHYSICAL EXAM` / `Neurologic examination` / `Exam` (with `Sensation`, `Reflexes`, `Coordination`, `Gait`, `Motor`, `CNP`)
    - `DATA / RESULTS`, `Selected relevant imaging`, `Selected relevant laboratory data`, `Imaging`, `Labs`
    - `ASSESSMENT & PLAN` / `Assessment` / `Plan` / `Impression` / `Recommendations`
    - **`Dx:` and `Tx:`**, which look like a neuroimmunology summary of diagnosis and treatment history and are high-value for extraction
    - `TB T spot` and `Hep B virus serologies` (pre-B-cell-therapy screening)
- **H&P:** `NEUROLOGY ADMISSION NOTE`, `History of Presenting Illness`, `Relevant Prior to Admission Medications`, the exam subsections, `DATA & IMAGING`, `ASSESSMENT & PLAN`, `Active Neurologic Issue(s) & Plan`.
- **Discharge:** `Principal Problem`, `Brief Summary of Hospitalization`, `Hospital Course by Problem`, `INITIAL HPI`, `Selected Relevant Labs/Imaging`, `ITEMS FOR FOLLOW UP`.
- **Pathology:** `CLINICAL HISTORY`, `INTERPRETATION`, `DIAGNOSIS`, `FINAL PATHOLOGIC DIAGNOSIS`, `RESULT`. **Flow cytometry** holds CD19/CD20.

**Boilerplate to strip before the LLM sees anything** (VisitNote especially):

- `Encounter-Level Documents`, `Order-Level Documents`, `Patient-Level Documents`, `Versions`
- billing lines (`ESTABLISHED HIGH/MOD MDM NN MIN`)
- vitals tables (`BP/Temp/Pulse/Wt Readings from Last N Encounters`, `Vitals`)
- immunisation lists (`TRIVALENT PF`, `PCV..`)
- `Contact Options`, `Education Provided`, `Patient Stated Goal`, PT/OT goal blocks
- Keep the boilerplate list in `config/boilerplate.yaml`.

**Copy-forward.** Share of a note's ≥80-character paragraphs already in an earlier note for the same patient: VisitNote median 33% (IQR 0–62%), ProgressNote median 7% (IQR 0–43%), others ~0%.

- Before extraction, **remove repeated paragraphs within each patient**: send each normalised paragraph to the LLM once, at its first occurrence.
- Keep a map from every later occurrence back to the first, so evidence can still cite every note.

**Volume.**

- About 4.6M tokens across all 32 patients (VisitNote 2.85M, ProgressNote 1.53M).
- Per patient: median ~107k tokens, min ~10k, max ~577k.
- Target **≤ ~150k tokens per pilot patient after filtering**, and log the reduction from each step.

**Keyword prevalence (patients out of 32).**

|Term|Patients|
|---|---|
|NMO / NMOSD terms|32|
|AQP4|30|
|MOG|16|
|inebilizumab|25|
|rituximab|19|
|eculizumab / ravulizumab|18|
|satralizumab|10|
|mycophenolate|10|
|azathioprine|5|
|tocilizumab|2|
|CD19 / CD20|14|
|IgG|24|
|pregnancy|~13|
|EDSS / mRS (rarely documented)|9|

### 1c. Updates from the clinical team (10.11.26)

**1. The first review step for every patient: does the patient actually have NMOSD?** The first real patient
reviewed had MOG antibody disease, not NMOSD. So each patient's review now starts by confirming the diagnosis:

- The patient needs a **documented, confirmed NMOSD diagnosis** with **at least 12 months of follow-up**.
- Put the most weight on **recent clinic letters and hospital notes**.
- Rule out the main alternatives: **MOGAD and MS**. Less common ones are **autoimmune encephalitis** and
  **spinal cord infarct**.
- A **positive AQP4 antibody** is usually diagnostic. About 85% of NMOSD patients are AQP4-IgG positive.

Implementation (built 10.11.26, see Phases 4, 6, 7 and 8):

- The `diagnosis` field group extracts one extra item per note: `dx_category`, the diagnosis the note gives
  now. Its codes are nmosd, mogad, ms, ae, sc_infarct and other. Possible or rule-out diagnoses are marked
  hypothetical. It is not a REDCap field.
- `reconcile` proposes the diagnosis-check answers. The proposal is the diagnosis stated by the most recent
  clinic letter or hospital note. A copied-forward paragraph counts at the date of the latest note that
  repeats it, and newly written text beats copied text from the same date.
- It flags other diagnoses stated within the last 24 months, and disagreements with the antibody results.
  Examples: AQP4-IgG positive but not proposed as NMOSD; MOG-IgG positive with no positive AQP4-IgG.
- It computes follow-up from the NMOSD diagnosis date to the last clinical note.
- In the review app, "1 · Diagnosis check" comes first. The REDCap instruments unlock only after the reviewer
  confirms NMOSD and at least 12 months of follow-up. Excluded patients need no further review and are
  never exported.
- The rules (criteria text, categories, follow-up threshold and definition, recency window) are a draft in
  `config/eligibility.yaml`, pending clinician sign-off.

Decisions by the user (10.11.26):

- **Follow-up** may be counted from the diagnosis or from the first MGB visit; either is fine. From the
  diagnosis is used.
- **Follow-up contacts:** clinical notes, including infusion procedure notes, and **patient messages**. Messages
  to the patient usually give the date of an infusion or a follow-up appointment, which is part of the
  patient's treatment.
  - Messages count at their own date. Outreach notes, which are "low value" by class, are recognised by their
    description.
  - The infusion and appointment dates the messages give also count. A new `followup` field group extracts
    them from messages and outreach notes.
  - A message date after the last note is flagged when it alone decides the 12 months. Cancelled or tentative
    dates are ignored, as are dates more than 18 months after the message.
- **Excluded patients are not recorded in REDCap at all.** That is what the export does.
- **Flags for other recent diagnoses** are fine. Patients often have several diagnoses at once, so only the
  diagnoses that replace NMOSD compete with it: MOGAD, MS, autoimmune encephalitis and spinal cord infarct.
  - A co-existing "other" diagnosis (e.g. Sjögren's, lupus) never becomes the proposal and is only listed.
    It is flagged only when it is the newest thing the notes say.
  - The model reports one item per diagnosis. "Not NMOSD" statements are kept as negated items, and a
    recent one is flagged.
- **"Not enough information"**, when accepted, excludes the patient.
- **Seronegative NMOSD** is flagged for a closer look, not excluded.
- **Borderline cases are seen by the reviewer first.** Nothing is ever excluded without a reviewer's
  decision. A case is borderline when any diagnosis-check flag is raised, or follow-up is within 3 months
  of the 12-month threshold either way.
  - The app marks borderline cases and lists the reasons.
  - It lets the reviewer look at the other instruments read-only before deciding.
  - It refuses to exclude a borderline case without a written reason.

**2. Output format: the project's own REDCap export.** The user saved a REDCap data export as
`~/Documents/Private Documents/nmosd_extractor/data/NMOSDDatabase_DATA_2026-11-10_1547.csv`. It shows the
format our outputs must be saved in. It holds PHI, so **Claude must never open it**.

- `nmosd profile-redcap-csv` prints its layout, metadata only. The user runs it and pastes the output back.
  That output confirms the structural columns, longitudinal events, instrument names, date formats and the
  file style.
- `export` takes the file as a template (`paths.redcap_template`) and reads its header line only. It writes
  exactly those columns, in that order, with the template's BOM, line endings and quoting. `reidentify`
  keeps the same layout.
- Without a template, the export uses REDCap's standard order: record ID, the repeat columns, then each form's
  fields followed by its `<form>_complete` column.

What the metadata-only profile of that export showed (10.11.26):

- UTF-8 without a BOM, CRLF line endings, quotes only where needed. This was already our default.
- 677 columns, in REDCap's standard export order. The file holds one sample record (28 rows).
- Not longitudinal: no `redcap_event_name`, and no data access group column. The repeating instruments are
  relapse, treatment, antibody_lab_data, edss, cd1920_counts, immunoglobulin_levels,
  infections_requiring_antibiotics, ivig_for_hypogam, last_follow_up and non_relapse_hospitalization.
- **Dates are written with slashes** (month first, as the `date_mdy` validation suggests), not YYYY-MM-DD.
  - `export.date_format` sets how our files write dates; they always read YYYY-MM-DD too.
  - The export, `reidentify` (DOB included) and `evaluate` use it.
  - The profile now reports the exact style (zero-padding, order) and suggests the setting.
- **Every checkbox has `___unk` and `___na` columns.** These come from the project's Missing Data Codes. The
  export now writes them, with 1 when the reviewed value is UNK or NA.
- UNK/NA also appear as text in coded and date fields. Evaluation and re-identification now leave them alone
  instead of failing.
- All 44 pilot dictionary fields are present. Every column our export writes exists in the template.
- `record_id` values are text up to 32 characters. See §6.

**Demographics from the RPDR Demographics table (16.11.26).** The user confirmed that the demographics come from
a standardized database: the `Demographics` table of the RPDR export (the same table as the `Demographics.csv` that
`inspect_accdb.py --export-csv` writes). `sex`, `race`, `ethnicity` and `living_deceased` are therefore taken from
that table only (`note_check: false` in `config/fields/pilot_fields.yaml`), and the `demographics` note check
(10.11.26) no longer runs by default. `Gender_Legal_Sex` is used when `Sex_At_Birth` is empty or has no REDCap
code, and `Race_Group` likewise for `Race1`; the review shows which column a value came from, or why there is none.

**Runtime machine.** HP Spectre x360 16, Windows 11 Home, i7-1260P, 32 GB RAM (often only ~8 GB free), Intel Iris Xe iGPU and no discrete GPU. So inference runs on CPU and is slow. Windows App Control for Business is enforced. Prefer pure-Python dependencies with wheels for Windows.

**REDCap.** The codebook PDF is in the user's files. Ask the user for the **data dictionary CSV** export, which is what the code parses. Key facts:

- Repeating instruments: `relapse`, `treatment`, `antibody_lab_data` (and others not in the pilot). Demographics is non-repeating.
- Paired date-estimate fields: 1 = exact (within several days), 2 = day estimated, 3 = day and month estimated, 4 = day, month and year estimated.
- Missing-data codes `UNK` and `NA`.
- Checkbox fields export as `field___code` columns. Calculated fields must never be populated.
- Known quirks: `rx_before_nmo` has duplicate Methotrexate (5 and 11); `adverse_events` order is 6 before 5.
- Identifier fields `mrn` and `dob` are only populated at the re-identification step, never by the LLM.

---

## 2. Tech stack

- Python 3.11+, `uv` or `venv` + `pip`; `pyproject.toml`
    
- Storage: **SQLite** (stdlib `sqlite3`), one file per stage
    
- Access reading: `pyodbc` + Microsoft Access Database Engine driver (Windows) **or** `mdbtools` CLI (macOS/Linux), behind one `AccessReader` interface with both backends
    
- De-identification: own header, known-identifier and regex layers, plus an **ensemble of two clinical de-identification transformer models** run locally via Hugging Face `transformers` + `torch` (CPU is fine):
    
    - **OBI Robust DeID** (`obi/deberta_deid_i2b2_mgb`, trained on i2b2 + Mass General Brigham notes; fall back to `obi/deid_roberta_i2b2` if unavailable)
    - **Stanford AIMI de-identifier** (`StanfordAIMI/stanford-deidentifier-base`, MIT licence; check for a newer `-v2`)
    
    Download the weights once during setup (no PHI involved), then run fully offline (`HF_HUB_OFFLINE=1`). Pin model revisions (commit hashes) in config for reproducibility.
    
- LLM: `ollama` Python client, structured outputs via `format=<JSON schema>` from **Pydantic v2** models, `temperature=0`, explicit `num_ctx`, `think=False` for reasoning models
    
- UI: **Streamlit** (localhost only)
    
- Tests: `pytest`; lint: `ruff`
    
- Config: `config/settings.yaml` (git-ignored, real paths) + committed `config/settings.example.yaml`
    

---

## 3. Repository layout

```
nmosd-extractor/
  CLAUDE.md
  BUILD_PLAN.md
  .claude/settings.json
  pyproject.toml
  config/
    settings.example.yaml        # data paths (outside repo), model, num_ctx, date-shift range
    redcap/data_dictionary.csv   # REDCap metadata (no PHI), provided by user
    fields/pilot_fields.yaml     # clinical extraction specs, clinician-written
    deid/allowlist.yaml          # terms never to scrub (drug names, eponyms, US states...)
  src/nmosd/
    config.py
    ingest/      access_reader.py, inspect_source.py, load_staging.py, column_map.py
    deid/        study_ids.py, linkage.py, date_shift.py, known_identifiers.py,
                 patterns.py, header.py, ner_models.py, surrogates.py, pipeline.py,
                 leak_test.py, review_report.py
    store/       schema.sql, db.py
    redcap/      dictionary.py, schemas.py, export.py, reidentify.py
    extract/     ollama_client.py, prefilter.py, chunking.py, prompts/,
                 stage1_evidence.py, quote_verify.py, stage2_reconcile.py, run.py
    review/      app.py
    eval/        compare_gold.py, report.py
  assets/icon/   nmosd_extractor.ico (desktop icon) and nmosd_extractor.png
  scripts/
    make_synthetic.py
    benchmark_ollama.py
    make_icon.py, make_desktop_shortcut.py
  tests/
    fixtures/synthetic/
    test_*.py
```

CLI entry points (`python -m nmosd <command>`): `inspect`, `stage`, `deid`, `leak-test`, `deid-report`, `extract`, `review`, `export`, `reidentify`, `evaluate`, `benchmark`.

---

## 4. Phases

### Phase 0: Scaffold, guardrails, synthetic data

- Create the layout, guardrail files, `pyproject.toml`, `settings.example.yaml`, ruff and pytest config.
- `scripts/make_synthetic.py`: build a **synthetic RPDR-like Access-equivalent dataset** as SQLite (and optionally CSV), with tables `Dem`, `Mrn`, `Con`, `Med`, `Lab`, and note tables `Prg`, `Vis`, `Rad`, `Pat`. Include 5 fake NMOSD patients with realistic clinical narratives: multiple relapses described repeatedly across clinic letters, treatment switches (e.g. azathioprine → rituximab → inebilizumab), "plan to start rituximab" vs "received rituximab", vague onset dates ("summer 2016"), AQP4 lab results, and MRI reports.
- Make the synthetic notes match §1b:
    - CRLF line endings, the header-block layouts per table, and fixed-width columns
    - the section headings listed, plus `Dx:`/`Tx:` lines
    - boilerplate blocks
    - copied-forward paragraphs (30–60% in visit notes)
    - visual acuities, strength grades, BP tables and spinal levels that look like dates
    - two-digit-year `Report_Date_Time` and DOB
- Seed the synthetic notes with **fake PHI in nasty formats**: names in headers and sign-offs, MRNs with and without dashes, DOBs in 5 formats, phone numbers, addresses, relatives' names, clinician names, outside hospitals, ages over 89, dates like "3/19", "March '19", "2019-03-04".
- Write a **synthetic gold-standard** REDCap-style answer file for the 5 fake patients.
- ✅ _Accept:_ `pytest` passes; synthetic DB builds deterministically from a seed; guardrail files exist.

### Phase 1: Source inspector + Ollama benchmark (the user runs these on real hardware)

- `nmosd inspect --source <path>` prints **metadata only**: table names, column names and types, row counts, distinct-count per column, and min/median/max/p95 length for text columns. For note tables, it also prints counts by `Report_Description` category only if `--show-categories` is passed, with a warning (note titles are usually safe, but the user decides). No values otherwise.
- Must work via pyodbc (Windows) and mdbtools (macOS). Detect available backends and give a clear install message if neither is present.
- Truncation check: report max text length per memo column so the user can compare with Access.
- `nmosd benchmark --models qwen3:8b,gemma3:12b --num-ctx 8192`: uses **synthetic** notes of 500 / 2,000 / 6,000 tokens and reports prompt-eval tokens/s, generation tokens/s, wall time, peak RAM, and whether the structured output validated. Verify Ollama is on 127.0.0.1, and refuse any model name containing `cloud`.
- ✅ _Accept:_ both run on synthetic data. Then **hand over to the user**: they run `inspect` on the real `.accdb` and `benchmark` on Shamik's laptop and paste back the output. Update `column_map.py` defaults to match the real schema.

### Phase 2: Staging

- `nmosd stage --source <accdb> --empi-list <file> --out <staging.sqlite>`: copies only the selected patients' rows (5 EMPIs, supplied by the user in a file outside the repo) from all relevant tables into a staging SQLite, using the column map. This is still identifiable, so it goes in the PHI location.
    
- Normalise: one `documents` table across all note tables (`source_table`, `source_report_number`, `empi`, `report_datetime`, `report_description`, `report_type`, `report_status`, `site`, `text`). Keep the original text byte-for-byte apart from stripping `[report_end]`. Handle multi-line text.
    
- **Remove duplicates**: group exact-duplicate `Report_Text` (after whitespace normalisation) within a patient into one document. Keep all source report numbers in a `document_sources` table. Report how many were collapsed per table (counts only).
    
- Tag each document with a `relevance_class` from `Report_Description`/`Report_Type` using a committed, editable mapping in `config/doc_types.yaml`:
    
    - `neuro_mri`: MRI brain / spine / face / MRA
    - `clinical_note`: office, telemedicine, ED, H&P, discharge, progress
    - `patient_message`: emails, MyChart renewals (useful for medications)
    - `flow_cytometry`
    - `pregnancy_related`: lactation, perinatal
    - `low_value`: XR, mammogram, routine pathology, nursing
- Parse both `Report_Date_Time` formats (two-digit and four-digit years) and `Date_of_Birth` (two-digit year, century from `Age`).
    
- **`nmosd select-pilot`**: writes a local table (PHI location only, never printed) of every patient with:
    
    - estimated tokens, before and after filtering
    - note counts by type
    - number of neuro MRIs
    - keyword flags (AQP4, MOG, each drug, pregnancy, CD19)
    
    This helps the user pick 5 varied patients of moderate volume. Print only aggregate distributions to stdout.
    
- ✅ _Accept:_ round-trip tests on synthetic data that mimic the real schema above; row counts reported per table and per patient (counts only).
    

### Phase 3: De-identification

Implement `nmosd deid --staging <in> --out <deid.sqlite> --linkage <linkage.sqlite.enc>`.

0. **Header blocks first:**
    - Parse each note's header block (§1b) with configurable patterns.
    - Harvest identifiers into the per-patient identifier set.
    - Replace the whole block with a neutral synthetic header: `[NOTE_TYPE] [SITE] [SHIFTED_DATE]`.
    - Remove the confidentiality-banner line.
    - Report per table the % of notes whose header matched a known pattern (counts only). Unmatched headers go to manual review.
1. **Study IDs:** `secrets`-based random IDs (`NMO-` + 6 chars of an unambiguous alphabet). New random document IDs. Drop all source IDs.
2. **Linkage key** (separate file; encrypt with a passphrase using `cryptography`'s Fernet with a scrypt-derived key): study ID ↔ EMPI/PMRN/MRNs, date offset, surrogate maps. Never written next to the de-identified output (enforce different directories).
3. **Date shift:**
    - One offset per patient, drawn uniformly from ±[30, 365] days.
    - Apply it to all structured dates.
    - Detect dates in text (many formats, including partial and two-digit years) and shift them, preserving the original format.
    - **Never shift the look-alike values listed in §1b** (VA, strength, BP, ratios, spinal levels, doses), using context rules with unit tests for each case.
    - Flag ambiguous short forms and unparseable or relative-but-anchored dates for review.
    - Ages ≥ 90 become "90+".
4. **Known-identifier layer:** this export has **no name or MRN table**, so read identifiers from a user-supplied, git-ignored `identifiers.yaml` in the PHI location (per EMPI: names, nicknames, MRNs, relatives, address) plus `Demographics` (EMPI, DOB, ZIP). If `Con`/`Mrn` tables appear later, read them too. Also add a helper that _suggests_ candidate names from common RPDR note header patterns into that file, for the user to confirm; it must never print them to stdout. Build per-patient identifier variants (name orderings, initials-plus-surname, possessives, MRN with and without separators, DOB in all formats, phone formats, address pieces, ZIP, email, relatives). Replace them with case- and whitespace-insensitive matching.
5. **Pattern layer:** phone, email, URL, SSN, MRN-like digit runs (configurable lengths), accession numbers, ZIP, ages ≥ 90.
6. **NER layer: model ensemble.**
    - Run both transformer models over every note (sliding windows with overlap; track character offsets).
    - Take the **union** of their PHI spans, using **recall-biased thresholds**: a span counts as PHI if _either_ model flags it above a low threshold, configurable.
    - Map each model's labels to one scheme: `PATIENT, STAFF, RELATIVE, AGE, DATE, PHONE, ID, EMAIL, URL, LOCATION, HOSPITAL, ORGANIZATION`.
    - **DATE spans go to the date shifter**, not to redaction.
    - Apply an allow-list so drug names, eponyms (e.g. Devic, Babinski, Lhermitte, Uhthoff, Romberg), assay names, NMOSD terms and US states are never scrubbed. Log allow-list overrides (counts only).
    - Record per-model and union span counts in the manifest, so the reviewer can see where the models disagree.
    - Keep a pluggable `NERModel` interface, so **AnonCAT** (if CogStack grants access to its model pack) or an MGB-approved service can be added later without changing the pipeline.
7. **Surrogates:** consistent tokens per patient (`[PATIENT]`, `[CLINICIAN_01]`, `[HOSPITAL_01]`, `[TOWN_01]`, `[RELATIVE_01]`, `[ID]`, `[PHONE]`, …). Mappings go only into the linkage key.
8. **Structured tables:** drop identifier columns, shift dates, and run free-text columns through the same pipeline.
9. **`nmosd leak-test`:** re-scan the output for every known-identifier variant (must be 0) plus the pattern and NER layers. Print **counts by category only**, and exit non-zero if known-identifier hits are greater than 0.
10. **`nmosd deid-report`:** writes a local HTML report (to the PHI location, not the repo) for the human reviewer. It shows original and de-identified text side by side with every replacement highlighted, a checkbox per document for "reviewed — no residual PHI", and a free-text field for misses. Claude must not open it.
11. **Manifest table:** tool versions, rule-set hash, run timestamp, counts per identifier category, reviewer sign-off fields.

- ✅ _Accept:_ on synthetic data, all seeded fake PHI is removed (test asserts 100% of seeded identifiers replaced), clinical terms on the allow-list survive, intervals between shifted dates equal the originals, and the leak test passes.

### Phase 4: REDCap dictionary → schemas

- `redcap/dictionary.py`: parse the data dictionary CSV (field name, form, type, choices, validation, branching logic, annotations). Detect calc, descriptive and `@HIDDEN` fields and choices keyed by code.
- `config/fields/pilot_fields.yaml`: one entry per pilot field with `definition`, `acceptable_evidence`, `exclusions` (e.g. "a plan or prescription is not a start date"), `time_window`, `ambiguity_rule`, `estimate_field`, `instrument`, `repeating: bool`, `prefilter_keywords`, and `preferred_source` (`structured` | `notes`). Draft it from the codebook and **mark every spec `status: draft — needs clinician sign-off`**.
- Direct structured mappings from `Demographics` (no LLM): `Sex_At_Birth` → `sex`, `Race1` → `race`, `Ethnic_Group` → `ethnicity` (one value across all 32, so flag for checking), `Vital_status` + `Date_Of_Death` → `living_deceased`. Keep the value maps in config, and send unmapped values to review.
- Pilot fields:
    - Demographics: `sex` (structured), `date_nmo_diagnosis` + `date_nmo_estimate`, `otherdx`, `dx_other`, `living_deceased` (structured)
    - Relapse: `date_onset` + `date_attack_estimate`, `relapse_phenotype`, `admission`, `date_admission`, `date_discharge`, `acute_rx`, `mri_relapse`
    - Treatment: `maintenance_therapy`, `date_starting_maint` + `date_maintenance_estimate`, `still_on_therapy`, `date_stopping_maint` + `date_stopping_estimate`, `reason_for_stopping_tx`
    - Antibody Lab Data: `type_test`, `aqp4_assay_date`, `aqp4_assay_status`, `mog_assay_date`, `mog_test_positive`. Extract from note text for now (no Lab table in this export); switch to structured-first if a Lab table is supplied.
    - Diagnosis check (10.11.26, §1c; not a REDCap field): `dx_category` in the `diagnosis` group, `redcap: false`; rules in `config/eligibility.yaml` (draft, needs clinician sign-off).
- `redcap/schemas.py`: generate Pydantic models per field group. Coded fields use `Literal[...]` of the valid codes plus `"UNK"` and `"not_established"`. Dates use `YYYY-MM-DD` with a separate estimate code.
- ✅ _Accept:_ schema generation is tested against the real dictionary CSV (it holds no PHI and is safe to read) and the generated JSON schemas are accepted by Ollama's `format`.

### Phase 5: Extraction stage 1 (evidence per note)

- **Volume is the main constraint.** For 5 patients, expect roughly 0.5–1M tokens if every note were sent (visit notes have a median of ~27k chars and reach ~101k), which is many hours on CPU. Prefiltering and sectioning are required.
- `dedupe.py`: per patient, in date order, strip boilerplate blocks (`config/boilerplate.yaml`), then drop paragraphs already seen in an earlier note (normalised hash). Keep the first-occurrence map. Report the token reduction.
- `prefilter.py`: select candidate documents per field group using `relevance_class`, then keyword hits from the field specs. Skip `low_value` by default. Report selection counts and estimated tokens before running.
- `chunking.py`: split long notes on section headings (HPI, Interval History, Exam, Imaging, Labs, Assessment/Plan, Medications, Impression) to fit `num_ctx` with overlap. Send only the sections relevant to the field group (e.g. MRI _Impression_ + _Findings_ for imaging fields; Assessment/Plan + Medications for treatment). Track character offsets back to the original text. Use the real section headings in §1b, and keep the heading→section-type map in `config/sections.yaml`. Always include `Dx:`/`Tx:` lines for the diagnosis and treatment field groups, and radiology `IMPRESSION` + `FINDINGS` for imaging and relapse-MRI fields.
- Combined field-group prompts are allowed when they fit, to cut repeated prompt processing of the same chunk.
- `ollama_client.py`: a thin wrapper with retries and timeouts. It sets `num_ctx` explicitly and **checks `prompt_eval_count` against the expected token count to detect silent truncation** (Ollama truncates without erroring). It records model name, digest, options and duration, and enforces the 127.0.0.1 host and the model allow-list. Put it behind an `LLMClient` interface (a Cortex implementation comes later).
- Prompt per field group: the field specs (definitions, exclusions), the schema described in words, and instructions to return `not_established` unless the text supports the exact meaning, plus verbatim quotes.
- Output rows in `evidence`: `run_id, doc_id, chunk_id, field, value, event_date, estimate_code, quote, char_start, char_end, certainty, negated, hypothetical/planned flag`.
- `quote_verify.py`: whitespace- and case-normalised exact match, then a tolerant fuzzy match with a threshold. Record `verified` along with offsets. Unverified evidence is kept but flagged.
- Resumable batch: skip done (doc, field group, run config) combos. Show progress and ETA, and save after every document so overnight runs survive interruption.
- ✅ _Accept:_ on synthetic data, at least 90% of evidence quotes verify, and "plan to start rituximab" is not emitted as a start date.

### Phase 6: Extraction stage 2 (reconciliation)

- **Relapses:** cluster evidence by onset date (configurable window, default ±30 days, wider when the estimate code is ≥ 3) plus phenotype. Merge the repeated mentions across letters into one episode, keeping all supporting evidence. Flag conflicts (different phenotypes or dates outside the window) rather than resolving them silently.
- **Treatments:** build episodes per drug from start/stop evidence and structured meds. Prefer structured administration records for dates when they exist. Detect switches and overlaps.
- **Single-value fields:** majority/latest-wins rules per field spec, with conflict flags.
- **Diagnosis check (10.11.26, §1c):** propose `dx_category` (latest clinic letter or hospital note wins) and `followup_12m` (≥ 12 months from diagnosis to the last clinical note) with evidence newest first, antibody cross-checks and a summary for the review screen.
- Use the LLM only for genuinely ambiguous merges, with its own logged prompt and output.
- Write `proposals`: `patient, instrument, instance_key (stable hash of episode anchor), field, value, estimate_code, evidence_ids, conflict_flag, run_id`. Stable `instance_key` ensures re-runs don't duplicate episodes.
- ✅ _Accept:_ on synthetic data, relapse count matches the gold standard for all 5 fake patients, and treatment episodes match.

### Phase 7: Review app (Streamlit, localhost)

- Three columns: **Left** has the patient list, review progress, instruments and outstanding conflicts. **Centre** has the proposed values (editable, with the REDCap choice labels shown and codes stored), Accept / Correct / Unresolved, and a comment field. **Right** shows the source evidence (full note with the quote highlighted, navigable to other evidence) plus keyword search across all of the patient's notes, with date and type filters and chronological browsing.
- Reviewers can add or delete repeating instances (e.g. a missed relapse) and attach a different evidence passage when correcting.
- Every action is written immediately to an append-only `reviews` table (reviewer name from a login prompt, timestamp, old value, new value, reason). Original proposals are never overwritten. Pages load from the DB and never re-run the model.
- Bind to `127.0.0.1`, set `server.headless = true`, and disable usage stats (`browser.gatherUsageStats = false`).
- **Step 1 per patient (10.11.26, §1c):** the diagnosis check comes first. The instruments stay locked until NMOSD with ≥ 12 months of follow-up is confirmed. An excluded patient needs nothing else. For a borderline case the reviewer can look at the instruments read-only, and excluding it needs a written reason.
- ✅ _Accept:_ full review of one synthetic patient end to end, and state persists across restarts. The synthetic MOGAD patient (P3) is proposed as MOGAD and excluded at the check.

### Phase 8: REDCap export + re-identification

- `nmosd export`: from accepted or corrected values only, build a REDCap **flat import CSV**: `record_id` (= study ID), `redcap_repeat_instrument`, `redcap_repeat_instance` (stable mapping from `instance_key`), fields, and checkbox `___code` columns. Dates are `YYYY-MM-DD`. Never set `*_complete` to Complete. Skip calc and descriptive fields, and validate against the dictionary (codes, ranges, branching logic warnings).
- Export only patients whose diagnosis check is included (10.11.26, §1c). Match the project's REDCap export layout via `paths.redcap_template` (header line only), and confirm the details from the user's `profile-redcap-csv` output.
- `nmosd reidentify` (**the user runs this in the approved environment**): decrypt the linkage key, map study ID → REDCap `record_id`, add `mrn`/`dob` if required, un-shift all dates, and write the final import file. Print counts only.
- Import into the **test** REDCap project via the Data Import Tool (the API comes later).
- ✅ _Accept:_ on synthetic data, the export passes validation, un-shifting restores the original dates exactly, and re-running export produces identical instance numbers.

### Phase 9: Evaluation

- `nmosd evaluate --gold <redcap_export.csv>`: compare against the existing REDCap entries for the same 5 patients (the user supplies a REDCap export, de-identified with the same linkage key). The comparison runs in the PHI location, and only **aggregate metrics** are printed:
    - per-field accuracy / precision / recall
    - episode-level matching for relapses and treatments (count error, matched pairs)
    - date error in days, split by estimate code
    - share of `not_established` and conflict flags
    - review time per patient (from the review log)
    - for comparison across models: run on each benchmark model
- ✅ _Accept:_ report generated on synthetic data; aggregate-only output confirmed by test.

### Phase 10: Desktop shortcut (Windows, 11.11.26)

- **Icon** `assets/icon/nmosd_extractor.ico`: a white outline brain on a teal (#009CA6) tile; at 16–24 px the brain has fewer folds and a thicker line. The brain is drawn in code, so there is no third-party artwork or logo. `scripts/make_icon.py` rebuilds the icon. (23.11.26: the Brigham mark was taken out of the icon and the repo before the repo was made public.)
- **`nmosd review --desktop`**: starts the app exactly as before (127.0.0.1, headless, no usage stats). It opens the browser once the app answers, and keeps the console window open if the app fails, so the message can be read. A second launch still refuses while the port is taken.
- **`scripts/make_desktop_shortcut.py`**: run once on the Windows laptop with the project's venv. It creates `NMOSD Extractor.lnk` on the desktop (including a desktop that OneDrive has moved). The shortcut runs `python -m nmosd review --desktop` in the repo folder and uses the icon.
    - Values reach PowerShell as environment variables, never pasted into its source.
    - If PowerShell is restricted (e.g. Constrained Language Mode under App Control), the script prints the steps to make the shortcut by hand.
    - `--dry-run` previews the shortcut on any OS.
- The shortcut holds no data paths: `config/settings.yaml` is read at launch, as for every command.
- ✅ _Accept:_ tests cover the icon sizes and its reproducible build, the shortcut fields, the dry run, the manual fallback, and the desktop launch (browser opened only once the port answers; window held open on failure). The user then creates the shortcut on the Windows laptop and confirms that it opens the review app.


### Phase 11: Every field of the codebook, 1-303 (16.11.26)

The user's review of the pilot: many codebook fields were not extracted. The codebook PDF
(`docs/NMOSD database _ REDCap_Codebook.pdf`) lists the fields to screen for, numbered 1-303 in 12 instruments.

- **Dictionary from the codebook.** `scripts/codebook_to_dictionary.py` reads the PDF's words and positions
  (`pdftotext -bbox`) and writes `config/redcap/data_dictionary.csv` in REDCap's own layout: 303 fields, choices by
  code, branching logic, field notes, annotations. It stops unless the numbers run 1..N, and it matches the
  hand-copied pilot fixture on every overlapping field. A field's number is its row (`Field.number`). The codebook
  also lists each `<form>_complete` status (`Field.is_form_status`), which the export writes once per form.
- **Where each field comes from** (`src/nmosd/redcap/sources.py`): 179 from the notes, 52 derived, 4 from the
  Demographics table, 59 filled by REDCap (calculated, descriptive, form status), 3 identifiers, and 6 the reviewer
  enters (initials, reviewer, hospital names, which de-identification removes, and `changes_fu`). Drug codes differ
  between fields, so code-to-code maps translate them; tests check the maps against the labels.
- **The codebook in the prompts.** The user asked that each field's label and note guide the model. Every item
  description now carries the codebook number, label, field note, branching logic ("REDCap asks this only if ...")
  and codes. New field groups list REDCap fields by name (`redcap_fields:` in `config/fields/pilot_fields.yaml`),
  and their schema is built from the dictionary. `guidance:` adds what the codebook does not say. `extra_items:`
  adds keys that are not REDCap fields (e.g. `for_attack`).
- **New field groups:** `admission` and `admission_course` (an admission for an attack goes into the relapse
  instance, any other becomes a non-relapse hospitalization); `imaging`, `imaging_brain`, `imaging_spine` (MRIs
  within 14 days are one instance, dated by the earliest); `treatment_detail` (dose, frequency, why started,
  toxicities); `labs` (CD19/CD20, IgG/IgM/IgA, lymphocytes); `ivig` (replacement only, not acute IVIG);
  `infection`; `background` (baseline mobility, paraneoplastic, insurance denials); `death`; four EDSS groups;
  `vitals` (weight and height by text pattern, no model). The relapse group also answers the attack details
  (clinical_confirm, trigger, gad_relapse, let, noncore_phenotype, firstrelapsestate).
- **EDSS** reads one examination note per maintenance treatment start and stop and the last follow-up
  (`timing`, #191) (`src/nmosd/extract/targets.py`). Those dates come from a reconcile run, so step 7 is now
  Extract → Combine → Disability (`extract --groups edss_...`) → Combine again.
- **Derived fields:** counts of attacks and admissions, phenotypes at diagnosis and overall, treatments before
  diagnosis, the DMT at each attack or admission and those started after it, B-cell counts before an attack, AQP4
  and MOG status, the DMT and mRS at last follow-up, `reason_maintenance` from the order of the courses (the
  codebook's 30-day rule) and its sub-reasons, `mri_relapse_number`, the last follow-up date, whether the patient
  is still followed at MGB, and long-term side effects from the conditions list. Each instance is cleaned with
  the branching logic, using the record's other values, so REDCap's hidden questions are not proposed. How a
  value was worked out is shown as a note (ℹ️), separate from conflicts (⚠️).
- **Fixes from the pilot review:**
  - A cut-off answer (it ran into `num_predict`, as long problem lists did) is asked again on the two halves of
    the excerpt, up to 4 levels, instead of losing the whole note. Complete items are salvaged from what is left.
  - The estimate code follows how the quote writes the date: "since 2019" is code 3 even if the model wrote
    2019-01-01 with code 1.
  - Repeat doses are one course: same-drug courses merge unless a stop comes between them or the drug goes
    unmentioned for over a year. One patient had 12 ravulizumab "courses".
  - Still on therapy follows the most recent notes that mention the drug. A drug not mentioned for over a year
    while other treatment continues is proposed as stopped (flagged).
  - An undated AQP4/MOG result is kept: it joins a dated test with the same result, or becomes an undated
    instance. Assay method, laboratory, titer and specimen are read too.
  - `otherdx` is the diagnosis given or considered at the initial presentation (the user's definition). It
    counts only from a note written before the diagnosis date or one saying "initially/previously/differential".
  - Comorbidities: every line of a problem list is reported. NMOSD itself and its attacks (e.g. transverse
    myelitis) are listed as "part of NMOSD", never ticked.
  - The notes search matches words across spaces, line breaks and hyphens. When no note has the words together,
    it returns the notes containing all of them.
  - "Diagnosis check" has no number. Instruments show their codebook range ("Relapse (#45–86)"). Fields show
    "45. Attack Date (date of symptom onset)", the variable name, whether REDCap hides the field, and how a
    derived value was found. Questions the branching logic skips are hidden.
- **Cost:** a group's keywords must now appear in the sections it reads, and the labs filter ignores "AQP4-IgG".
  Even so, the new groups add roughly two thirds more model calls. Step 7's preview shows the counts.
- **`nmosd coverage`** prints, for every field 1-303, its source and how many patients have a value. For a field
  read from the notes, it also counts the patients without one whose notes the field's group did read. Counts
  only, safe to paste. It answers "which sections could have been filled and aren't" without Claude reading notes.
- **`nmosd quality-packet`** (`src/nmosd/eval/quality_packet.py`, §0 rule 8): a HIPAA Safe Harbor-style packet of
  the proposals and their evidence, which Claude may read to assess quality (the user approved this on condition it
  is HIPAA compliant). Tests: `tests/test_quality_packet.py`. 18.11.26: `--whole-notes` adds every note in full
  (§0 rule 8), and the key maps date tokens back to dates.
- **Code checks (18.11.26, `src/nmosd/extract/checks.py`, tests `tests/test_checks_1811.py`).** From the pilot's
  reference review (whole notes read by Claude; results outside the repo). Applied in Combine, so no model re-run:
  (1) a treatment mention must name its drug in the quote or within 250 characters of it in the note; unnamed
  mentions are left out when others name the drug, and a drug no passage names is still proposed but flagged;
  (2) a "stop" on the day of a note that also starts the drug that day is the end of that infusion, not of the
  course; (3) a start date taken from an administration note that does not speak of starting is flagged ("the
  course may have begun earlier"); (4) prefilter keywords match from the start of a word ("cipro" not in
  "reciprocal"). Still to build, after the pilot is combined again and re-scored: a history pass over the
  copied-forward overview, rule readers for the regular structured blocks (lab tables first), routing by note
  content, and a verify call for treatments/relapses/dates.
- **Scoring and the fast test (18.11.26, `src/nmosd/eval/score_reference.py`, tests `tests/test_score_reference.py`).**
  `nmosd score` compares a Combine run with reference answers (`Patient_X.json`, outside the repo: codes, date
  tokens with their precision, text; the packet key maps patients and tokens back). Instances are paired by date and
  by drug or antibody; fields are correct, wrong, missed, unsupported or not comparable; counts and field names
  only. `nmosd fast-test` runs extract → combine → disability → combine → score on each reference file's fast set
  (`extract`'s new `only_docs`; copied-forward text is judged within the set) into `<extract>_fast`, leaving the
  full results alone. `--back 1` scores the run before the latest.
- **What each group reads, and infusion records without the model (18.11.26).** A check on the pilot's notes (the
  section and keyword rules run over the packet's text) showed why answers were missed: the infusion nurse's notes
  (800-2,600 characters, no headings the rules know) were read at 11-13% by the treatment groups, so the
  administration table was never seen; and the attack history was read, but inside excerpts of about 30,000
  characters. New per-group options in `config/fields/pilot_fields.yaml` (`chunking.select_view`,
  `run.group_budgets`): `whole_note_under: 4000` (a short note is read whole), `focus: N` (only N lines either side
  of a keyword, anywhere in the note: antibody, labs, infection, ivig, background, death, treatment_detail,
  diagnosis, admission, admission_course), `max_chunk_chars: 9000` (relapse, treatment). New guidance: relapse
  (one item per past attack in a history summary), treatment (an administration record is one dose, never guess
  a drug from a dose), antibody (negative results, titre, assay, draw date), imaging (a normal MRI is a study).
  New no-model group `treatment_admin` (`src/nmosd/extract/admin_records.py`): "Administrations This Visit"
  tables and "Med: / Frequency: / Dose:" templates become dated mentions with dose and frequency; Combine merges
  them into the course and takes a first dose that differs from the later ones as the loading dose (the start).
  On the fast set this sends roughly a third less text to the model. Every group whose spec changed runs again.
  Not built: a lab-table reader (the pilot has almost no CD19 or immunoglobulin table rows; the few antibody rows
  now reach the model through `focus`) and the verify call (it adds a model call per mention; wait for the fast
  test to show whether unsupported values remain after the code checks). Tests: `tests/test_checks_1811.py`.
- **Lab tables after all (18.11.26, later).** `nmosd lab-shapes` (`src/nmosd/eval/lab_shapes.py`; masked line shapes
  and counts only, from the export or a staged database; also lists the test names of a structured Lab table, of
  which this export has none) over all 32 patients' 1,526 notes showed repeated table layouts for immunoglobulins,
  lymphocyte and CD19/CD20 counts and the AQP4/MOG results. New no-model group `labs_table`
  (`src/nmosd/extract/lab_tables.py`): a row is read when its first cell is exactly a known test name and its value
  parses; the date is the row's date cell, else a "Collected:" line above, else the note's. Its rows feed the lab
  instruments (a table row is proposed before a sentence the model read) and the antibody instances. Not read, and
  open for the clinician: a row named only `CD19`/`CD20` (count or percent?) and flow-panel rows (`CD19+CD20-`).
- **`nmosd shapes` and three more no-model readers (18.11.26, later).** `shapes` is `lab-shapes` for all the
  structured data (immunotherapy rows, weight and height, diagnosis rows, disability scores, MRI lines, attack
  lists, admission dates) plus the export's structured tables (this export: Demographics only; no Med or Lab
  table). From its output over 32 patients: `dx_dates` (the problem-list / visit-diagnosis row naming NMOSD with
  its date, in 249 notes of 28 patients: a fallback for `date_nmo_diagnosis`, estimate code 4, used only when no
  note states a date and it is earlier than the first note stating NMOSD) and `admission_dates` ("Admit date:" /
  "Discharge date:" header lines: they join the stay they belong to and come first; a discharge-only header joins
  the latest stay begun up to 120 days before and never makes a stay) in `src/nmosd/extract/note_facts.py`; the
  vitals reader also reads "WEIGHT(lbs)  150 lbs" (`reader_version: 2`). Candidates not built: medication-list
  rows with start and end dates (an order date is not a first dose), dated MRI result headers
  ("MRI Brain [n] date (Final)") as imaging studies, the "Date | EDSS" table, the vitals header-row table.
- **CD19 / CD20: the clinician's rules (18.11.26).** Laboratories report both an absolute count and a percentage
  for each marker, so a marker name and a number are not enough. (1) A row named only `CD19`/`CD20` is a count or
  a percentage only by its unit, by giving both (the number beside a percentage is the count) or by its column
  heading; otherwise the raw result is kept as `cd19_unspecified`/`cd20_unspecified`, flagged, and not entered
  (the `labs` prompt has the same rule and options). (2) Flow-panel rows are subsets: total CD19+ = CD19+CD20- plus
  CD19+CD20+ only when both come from the same note and date and no total is reported (an explicit total wins;
  percentages below the reporting limit are not added); CD19+CD20+ is never entered as total CD20+, since it may
  omit CD19-CD20+ cells: it is shown to the reviewer. REDCap asks for the percentage only when there is no
  absolute count, so a proposal follows that. `lab_tables._cd_records`, `stage2_instruments._subset_total`.
- **Reader against model (18.11.26).** Where a no-model reader and the model both answer, Combine proposes the
  reader's value and flags a disagreement for the reviewer (no extra model call): lab values (same test and day),
  the AQP4/MOG titre, the dose and frequency of a treatment course (administration records against
  `treatment_detail`; "3,000 mg" and "3000mg" are the same dose), admission and discharge dates (the summary
  header against the admission groups and the attack's own mention; more than a day apart), a start date later than
  the first recorded administration, and a problem-list NMOSD date more than a month before the stated diagnosis
  date (the stated date stays). `stage2_instruments.header_dates`, `merge_treatment_details`,
  `stage2_reconcile.problem_list_check`, `reconcile_antibodies`, `reconcile_treatments`.
- **Speed (17.11.26).** The pilot's step 7 took about 14 h (1,762 pairs, ~28 s each); the 220-patient cohort would
  take about 25 days. (1) A cut-off answer is split at once, not asked twice more unchanged. (2) `llm.num_predict`
  3072 with `llm.num_ctx` 10240, so excerpts keep their size; `num_predict` joins the run hash unless it is 1024.
  (3) `llm.parallel` (default 4): worker threads with a client each, answers written in the planned order so the
  database matches a one-at-a-time run; Ollama needs `OLLAMA_NUM_PARALLEL`. (4) `skip_repeated_lines` on
  `comorbidity` and `background`: list lines already read for the patient are left out, notes with nothing new are
  not read, prose is never cut. `nmosd timing [--cohort N]` reports where the time went (counts and times only).
  Tests: `tests/test_speed_1711.py`, `tests/test_timing.py`. The cohort run will use the original notes (user,
  18.11.26), so no de-identification time.
- **Hospital names from the notes (18.11.26).** The user asked that, without de-identification, the fields left out
  because the names were removed be read from the notes: `hx_opcare` (#39), `nr_hosp_2` (#61), `nr_hosp` (#94). A new
  group `hospital` (`needs_hospital_names: true`) reads them only when the notes still name hospitals: the manifest
  says `deidentified: false`, or `tokenise_hospitals: false` (`sources.hospital_names_kept`). Otherwise `extract`
  skips it, reconcile ignores any old mentions, and the fields stay with the reviewer as before. A separate group,
  so the admission groups' finished pilot work stays done (their run hashes are unchanged). A hospital mention
  never makes an admission and never moves its dates: it names the hospital of a stay the admission groups found
  (`stay_hospitals`), and the attack's relapse instance gets it as `nr_hosp_2`. `hx_opcare` is the hospital named
  most often for outpatient care. The review app, export, coverage report and quality packet pick the field list
  from each project's notes (`ReviewStore.instrument_fields`). Also fixed: two admission mentions without an
  admission date, far from any dated stay, made reconcile fail. Tests: `tests/test_hospital_names.py`.
- ✅ _Accept:_ 381 tests pass, including `tests/test_codebook_coverage_1611.py` (dictionary rebuilt from the PDF, the
  numbering, every field's source, the codebook text in the schemas, each fix above, the new instruments and
  derived fields, numbered fields in the review app, export of the new instruments, the coverage report). Still to
  do on real data: re-run step 7, review, and get the clinician's sign-off of every new group (TODO.md).

### Phase 12: Accuracy tables for a paper (18.11.26)

The user asked for a table of how accurate the extraction was (whether an answer stayed the same or was
incorrect), saved, to report in a paper.

- **Nothing new to record during review:** `reviews` already keeps every decision, append-only.
- **`nmosd accuracy`** (`src/nmosd/eval/accuracy.py`). Reference standard: the
  reviewer's final value. Field level, for every field the review shows in the instances the extractor proposed
  and the reviewer kept (included patients only): correct, correctly empty, wrong value, filled when it should be
  empty, missed; unresolved and not reviewed are counted but left out of the rates. UNK/NA count as no value; text
  ignores case and spacing; dates compare as dates; checkboxes as sets. Accuracy with a 95% Wilson interval,
  precision, recall and F1, overall, by source (model, derived, Demographics table), by instrument and by field. Also:
  the diagnosis check per patient, episodes (confirmed, deleted = wrongly proposed, added = missed; precision and
  recall), date errors in days, and checkbox codes.
- **Saved as a snapshot:** each run writes a new dated folder: the report and CSV tables (counts only, safe to
  share), and `decisions_with_study_ids.csv` (one row per decision, no values or dates; PHI location) for a
  patient-level analysis. The report says that the reviewer saw the proposals (possible anchoring), and that
  `evaluate` compares with REDCap entries made without the tool.
- **Guided app step 10, "Export tool accuracy"** (the user's request): a name for the export (default: the model),
  **Export the accuracy table**, the report on screen, and **Download the accuracy tables (.zip)** with the
  counts-only files only (`accuracy.SHAREABLE`; never the file with study IDs), plus **Open the folder**. Ticked when
  the latest export is newer than the review database, ⚠️ when decisions were made after it.
- ✅ _Accept:_ `tests/test_accuracy.py` (each kind of decision counted, the rates, episodes, dates, codes, counts-only
  output, a new folder each run, the command line) and the guided-app walk-through in `tests/test_app.py` (step 10
  asks for a review first, then exports, downloads only the shareable files, and is ticked).

### Phase 13: Neurology REDCap Extractor: choice of condition, own window (19.11.26)

The user asked for the tool to be usable for other neurological conditions, to open on a choice of condition, to
run in its own window called "Neurology REDCap Extractor" instead of Safari, and to go into a private GitHub repo of
that name.

- **Conditions list** `config/conditions.yaml` (`src/nmosd/conditions.py`): id, name, full name, group, `status`
  (`ready` / `planned`) and, for ready ones, a settings file in config/. NMOSD is ready and keeps
  `config/settings.yaml`; the others (the user's choice: MOGAD, MS, myasthenia gravis, autoimmune encephalitis,
  epilepsy, Parkinson's, ALS/MND, stroke, dementia, migraine) are planned. Settings names are limited to
  `settings[.<id>].yaml` (all git-ignored), so each condition has its own private data folder. The list is not an
  extraction rule: `rules_hash()` leaves it and every settings file out.
- **Start screen** (guide.py): "Which condition are you working on?", cards by group, ✅ Ready / 🕓 Not set up yet.
  A ready condition opens the 10 steps and the review page, with **Change condition** in the sidebar (clears the
  window's session: no paths, patient lists, identifiers or passphrases carry over). A planned one explains what
  setting it up needs (data dictionary, notes vs structured fields signed off, diagnosis check, pilot patients). No
  condition's settings file is read before it is opened. The choice is per window: a new window starts at the list.
- **Own window** `nmosd app --window` (`src/nmosd/app/window.py`): pywebview (`[window]` extra; WebKit / Edge
  WebView2, private mode, downloads allowed, Mac menu bar named after the app) → Edge/Chrome `--app` window with a
  throwaway profile → the browser. Closing the window stops the Streamlit server; background jobs carry on. With no
  terminal (Mac app, pythonw), prints and the server's output go to a log outside the app folder
  (`~/Library/Logs/Neurology REDCap Extractor`, `%LOCALAPPDATA%\Neurology REDCap Extractor\logs`), failures show a
  dialog, and the browser fallback shows a dialog whose OK closes the app.
- **Launchers renamed**: `Start/Install Neurology REDCap Extractor.bat`, `Start Neurology REDCap Extractor.command`;
  the Windows shortcut runs `pythonw -m nmosd app --window` and removes the old "NMOSD Extractor" shortcut; the
  installer adds the `[window]` extra separately (a failure only warns: Edge app mode works on every Windows 11).
  `scripts/make_mac_app.py` makes `Neurology REDCap Extractor.app` (git-ignored; absolute path to this folder).
- **Release/git**: the release zip is `Neurology_REDCap_Extractor_<date>.zip` and leaves out per-condition settings,
  the Mac app and quality-review packets (`Patient_*` files refused anywhere). `.gitignore` adds the same.
- **Not done (next step for a second condition):** the extraction engine is still NMOSD's (field groups, eligibility,
  REDCap sources, reconcile and instrument rules, prompts). Making a condition ready needs those per condition.
  The package and command stay `nmosd`.
- ✅ _Accept:_ `tests/test_conditions_window.py` (the list and its validation, git-ignored settings, the start screen
  and the planned page via AppTest, Change condition, the window fallbacks, the windowed launcher with dialogs, the
  CLI, the Mac app bundle); release, shortcut and app walk-through tests updated. The user checks the window on the
  Mac and on Windows.

---

## 5. Definition of done (pilot)

One complete path works on the real 5 patients: de-identified with zero leak-test hits and human sign-off, extracted overnight on Shamik's laptop, reviewed in the app, exported, re-identified in the approved environment, imported into the test REDCap project, and evaluated against the existing abstraction.

## 6. Things to ask the user rather than guess

- ~~Real `.accdb` schema~~ (received 09.11.26, see §1)
- Whether RPDR can supply `Med`, `Lab`, `Lno`, `Mrn`, `Con` tables for the same cohort
- The 5 pilot EMPIs and their `identifiers.yaml`
- ~~Section headings, date formats~~ (received 09.11.26, see §1b)
- Confirmation of the header-line labels (e.g. "line 3 = age, sex, MRN, DOB"); the user describes labels only, never values
- ~~(10.11.26) The `profile-redcap-csv` output for the REDCap export~~ (received 10.11.26, see §1c: not longitudinal). Still open: the exact date style, which the re-run profile prints as a suggested `export.date_format`.
- ~~(10.11.26) Is follow-up counted from the NMOSD diagnosis or from the first MGB visit?~~ (either is fine; diagnosis is used, see §1c)
- ~~(10.11.26) Which encounters count as follow-up?~~ (clinical notes incl. infusion notes, and patient messages with the infusion/appointment dates they give, see §1c)
- ~~(10.11.26) Does seronegative NMOSD need extra evidence before inclusion?~~ (flagged for a closer look, not excluded, see §1c)
- ~~(10.11.26) Should excluded patients (e.g. MOGAD) be recorded anywhere in REDCap?~~ (no)
- (10.11.26) Are the `record_id` values in the project export the real REDCap record IDs? They are text up to 32 characters, and REDCap's "hash the record ID" export option produces 32-character IDs. `record_map.csv` must hold the real ones.

---
- 19.11.26, `nmosd quality-packet --fast [--reference <folder>]`: a packet of the fast test's results (its own
  review file, so no review decision is laid over the proposals), with the dates numbered as in the packet the
  reference answers were written from (`tokens_from`; a date that packet did not have gets a letter, `D05a`
  = after D05 and before D06). For reading why a fast-test value was wrong or missed. First fast test on the
  rebuilt tool: recall 23%, precision 30% over the 5 pilot patients (counts in the packet folder).
  Open: `score` still counts the hospital-name fields (not read from de-identified notes) as missed.

## 7. Order of work and first Claude Code session

**Track A (start now):** Phase 0 → Phase 1 (inspector already exists as `scripts/inspect_accdb.py`; add the benchmark) → Phase 2 → **Phase 3 (de-identification)**. The user then runs it on the 5 pilot patients and completes 100% human review.

**Track B (in parallel, synthetic data only):** Phase 4 (once the REDCap dictionary CSV is in `config/redcap/`) → Phases 5–9.

Paste into Claude Code to begin:

> Read CLAUDE.md and BUILD_PLAN.md in full. Do Phase 0 only. Then stop and summarise what you built, what tests pass, and any questions. Remember: never read anything outside this repository or any real patient data. Use the synthetic fixtures you generate, modelled on §1 and §1b.

- REDCap data dictionary CSV; whether the project has longitudinal events
- Which `@HIDDEN` fields are still in use
- Clinician sign-off on `pilot_fields.yaml`
- Whether hospital names should be tokenised or kept (affects `nr_hosp_2`, `hx_opcare`). 18.11.26: with the
  original notes (the cohort) they are kept and the `hospital` group reads them; still open for de-identified notes
- Benchmark results → which model is the default