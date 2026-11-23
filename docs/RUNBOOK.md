# Runbook: running the pipeline on the real pilot data

Everything below runs on your machine (or Shamik's laptop). Claude Code never sees the data. Only paste back
output marked **safe to paste**, which is always counts, column names and timings. Never paste note text,
identifiers, the identifiers file, the HTML report or any database.

PHI folder used below: `~/Documents/Private Documents/nmosd_extractor/` (`$PHI`). On Windows use the equivalent
private folder; every path is set in `config/settings.yaml`.

---

## 0. One-time setup

### macOS (development)

```bash
cd "<repo>"
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev,ner,app,bench]"
brew install mdbtools                     # Access reader
cp config/settings.example.yaml config/settings.yaml   # then edit paths (already prepared: see below)
```

A ready-made `settings.yaml` for this Mac is in Claude's scratchpad; copy it with:

```bash
cp "/private/tmp/claude-501/-Users-alex-Documents-Medicine-Year-14---PhD-Year-3-Harvard-placement-NMOSD-Extractor/860fd43c-1911-4d2c-a39f-02db9278e311/scratchpad/settings.yaml" config/settings.yaml
```

### Windows 11 (pilot laptop): the guided app

Easiest: unzip the release (`python scripts/make_release.py` on the Mac makes it), double-click
**Install Neurology REDCap Extractor**, then the **Neurology REDCap Extractor** desktop icon, and choose **NMOSD** on
its start screen. The app walks through every step of this runbook and shows the same metadata-only outputs. Instructions for users: [USER_GUIDE.md](USER_GUIDE.md). The
manual steps below do the same by hand.

### Windows 11 (pilot laptop): by hand

1. Install Python 3.11 (64-bit) from python.org; tick "Add to PATH".
2. Install the **Microsoft Access Database Engine 2016 Redistributable (64-bit)**. App Control for Business may
   need IT approval.
3. Install **Ollama** for Windows; then `ollama pull qwen3:8b` and `ollama pull gemma3:12b`.
4. In PowerShell in the repo folder:
   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\pip install -e ".[windows,ner,app,bench]"
   copy config\settings.example.yaml config\settings.yaml   # edit the paths
   ```
5. If App Control blocks `torch` DLLs, the NER layer cannot run; tell Claude and do not use `--no-ner` on real data.
6. Desktop shortcut for the review app (after `config\settings.yaml` has its paths):
   ```powershell
   .venv\Scripts\python scripts\make_desktop_shortcut.py
   ```
   Double-click **NMOSD Extractor** on the desktop: the review app starts and opens in the browser. Keep the
   black window open while you review; closing it stops the app. If PowerShell is restricted on the laptop,
   the script prints the steps to make the shortcut by hand.

### NER model weights (one time, needs internet, no PHI involved)

```bash
python scripts/download_models.py
```
Paste the printed `hf_id @ revision` lines into `deid.ner.models` in `config/settings.yaml`. Everything after this
runs offline (`HF_HUB_OFFLINE=1` is set at runtime).

### Linkage passphrase

Choose a passphrase (12+ characters) and keep it in your password manager. Either type it when prompted or set
`NMOSD_LINKAGE_PASSPHRASE` for the session. Losing it means the de-identified data can no longer be re-identified.

---

## 1. Phase 1 handover

```bash
python -m nmosd inspect                          # source path from settings.yaml
python -m nmosd inspect --show-categories        # optional: note-title counts (read before sharing)
python -m nmosd benchmark --models qwen3:8b,gemma3:12b --num-ctx 8192     # on the pilot laptop
```
**Safe to paste:** the `inspect` output (without `--show-categories` unless you have read it) and the benchmark table.

## 2. Choose the 5 pilot patients

```bash
python -m nmosd stage --all-patients --out "$PHI/work/staging/all_patients.sqlite"
python -m nmosd select-pilot --staging "$PHI/work/staging/all_patients.sqlite"
```
**Safe to paste:** the `select-pilot` summary (distributions only). Open the per-patient table
(`pilot_selection.sqlite`, PHI location) yourself, e.g. in DB Browser for SQLite, pick 5 varied patients of
moderate volume, and write their EMPIs, one per line, to `$PHI/work/pilot_empis.txt`.

## 3. Stage the pilot

```bash
python -m nmosd stage
```
**Safe to paste:** the counts table.

## 4. Confirm identifiers

```bash
python -m nmosd suggest-identifiers --out "$PHI/work/identifiers.suggested.yaml"
```
If the "Header pattern matches" counts are well below 100% for a table, run `python -m nmosd header-shapes`.
For the notes whose header is not recognised it prints the most common masked line shapes (digits 9, letters
A/a, label words such as `MRN:` kept) and counts, never a value. **Safe to paste.** The header patterns in
`config/deid/patterns.yaml` are then fitted to those shapes.

Open that file yourself, check every entry, add nicknames, relatives, phone numbers, emails and street addresses,
and save it as `$PHI/work/identifiers.yaml`. Also confirm the header-line labels (e.g. "line 3 = age, sex, MRN,
DOB"). Tell Claude the labels only, never values.

## 5. De-identify

```bash
python -m nmosd deid
python -m nmosd leak-test          # must print "Known-identifier hits: 0 (PASS)"
python -m nmosd deid-report        # writes $PHI/work/reports/deid_report.html
```
Open the report in your browser, review **every** document, tick "reviewed — no residual PHI", note misses
(describe the location, not the value), click **Download sign-off JSON**, then:

```bash
python -m nmosd deid-signoff --file ~/Downloads/deid_signoff.json
```
**Safe to paste:** the `deid` summary (counts by category, header-match percentages, review-flag counts) and the
leak-test counts. If headers do not match at ~100% for a table, tell Claude the table and the masked shape only.

### 5b. Without de-identification (optional, 13.11.26)

De-identification is optional. To keep the notes as they are, set `deid.enabled: false` in the settings (the guided
app asks in steps 4 and 5), skip step 4, and run instead of the commands above:

```bash
python -m nmosd stage --all-patients       # or with the patient list, as before
python -m nmosd deid --keep-identifiers    # copies the notes as they are, under study IDs; a few minutes
```
The original text and real dates go into the same working database, so `extract`, `reconcile`, `review`,
`export` and `reidentify` run unchanged; `leak-test`, `deid-report` and `deid-signoff` say there is nothing to do.
The notes, the extraction and the review then hold real names and dates: they stay in the private folder, and
the review screen says so. Extraction never reuses work done on de-identified notes (or the reverse).

A patient prepared one way is never switched silently: `deid` stops and asks for `--switch-mode`, because that
patient's earlier extraction, review and REDCap files carry the other dates. Make the final file from that work
first (section 8) if you need it; after the switch the patient's extraction runs again and their dates need
reviewing again.

## 6. Schemas and field specs (Track B)

1. Export the REDCap **data dictionary CSV** (Project Setup → Data Dictionary → Download) and save it as
   `config/redcap/data_dictionary.csv`. It contains no PHI, so Claude can read it.
2. `python -m nmosd schemas --dictionary config/redcap/data_dictionary.csv --out "$PHI/deid/schemas"`
3. A clinician reviews `config/fields/pilot_fields.yaml` and changes each `status` to `signed off <name> <date>`.
4. A clinician also reviews `config/eligibility.yaml`, the diagnosis-check rules (see §7), and signs it off the
   same way.

### 6b. The REDCap export format (the layout our files must match)

The project's own REDCap data export (`NMOSDDatabase_DATA_<date>_<time>.csv`, raw codes) holds PHI. Keep it in
the PHI folder and set `paths.redcap_template` to it in `config/settings.yaml`. Then describe it for Claude:

```bash
python -m nmosd profile-redcap-csv          # uses paths.redcap_template; or --file <csv>
```
**Safe to paste:** the whole output. It lists the file style, the column names in order, row and record
counts, rows per repeating instrument, and per column how many cells are filled and what kind of value
they hold. It never prints a value, an ID or a date. `export` reads only the header line of this file.

The output ends its summary with `Suggested setting: export.date_format: ...`. Put that value in
`config/settings.yaml` under `export:`. The project export of 10.11.26 writes dates with slashes, month
first. Export, `reidentify` and `evaluate` then write and read dates the same way.

## 7. Extract, reconcile, review (pilot laptop, overnight)

```bash
python -m nmosd extract --dry-run          # selection counts and token estimates only
python -m nmosd extract                    # resumable; safe to stop and re-run
python -m nmosd reconcile
python -m nmosd extract --groups edss_ambulation,edss_vision_brainstem,edss_pyramidal_cerebellar,edss_sensory_bowel_cerebral
python -m nmosd reconcile                  # again, with the disability (EDSS) answers
python -m nmosd coverage                   # every codebook field 1-303: who has a value (counts only)
python -m nmosd timing --cohort 220        # where the time went per field group, and the time for 220 patients
python -m nmosd review                     # http://127.0.0.1:8501
```
Since 16.11.26 the extraction covers every field of the codebook (1-303; BUILD_PLAN Phase 11). The disability
(EDSS) groups read the examination notes at each treatment start and stop and at the last follow-up, which the
first `reconcile` finds; hence the second `extract` and `reconcile`. Step 7 of the guided app runs all four.
**`coverage` is safe to paste**: field numbers, names and counts only. For each field read from the notes it counts
the patients with no value although the field's group read some of their notes: the places to look for misses.
On Windows, the **NMOSD Extractor** desktop shortcut (§0, step 6) does the same as `review` and opens the browser.
**Safe to paste:** the dry-run plan, the final "Done:" lines, the reconcile lines, the `coverage` table and the
`timing` table (counts only). `timing` reads only the call log's token counts and durations: per field group the
model calls, the answers cut off at `llm.num_predict` (thrown away and asked again in halves) and the time spent.
Since 17.11.26 a cut-off answer is no longer asked twice more unchanged (at temperature 0 it came back the same).

Stopping and carrying on: press Ctrl+C at any time, or close the window; at most the document in progress is
redone. If Ollama stops answering (quit, the computer slept, out of memory), a document that got no answer is
not marked done. After 3 such documents in a row the run stops by itself and says so. Either way, start Ollama
and run the same `extract` command again. The "Done:" line counts what was left (`left for the next run`).

### Speed (17.11.26)

The pilot's step 7 (5 patients, 1,762 note-and-topic pairs) took about 14 hours on an M4 Pro, about 28 s per pair.
Four changes cut that down:

1. **A cut-off answer is not asked again unchanged.** An answer that runs into `llm.num_predict` used to be asked
   twice more (identical at temperature 0) before the note was split. Now it is split straight away.
2. **Longer answers:** `llm.num_predict: 3072` (was 1024) with `llm.num_ctx: 10240` (was 8192), so the note
   excerpts stay the same size. Long problem lists and lab tables finish in one answer instead of being split.
   Changing `num_ctx` re-runs every field group once.
3. **Several questions at once:** `llm.parallel: 4`. Ollama answers them together only when it is started with
   `OLLAMA_NUM_PARALLEL` at least as high, otherwise they wait in line (no harm, no gain). Each needs its own
   context in memory: 4 x 10,240 tokens is about 6 GB on top of the model's 5 GB. If the Mac runs short of memory
   (Activity Monitor, Memory Pressure turns red) use 2. The answers are written in the planned order, so the
   database is the same as after a one-at-a-time run; the model's answers themselves can differ very slightly when
   it answers several at once. Ctrl+C can take a minute while the questions in progress finish.
   - macOS: `launchctl setenv OLLAMA_NUM_PARALLEL 4`, then quit Ollama (menu bar icon, Quit) and open it again.
     Repeat after a restart of the Mac.
   - Windows: `setx OLLAMA_NUM_PARALLEL 4` in a Command Prompt, then quit Ollama (tray icon) and open it again.
     Only worth it on a laptop with a dedicated graphics card; without one, leave `llm.parallel: 1`.
4. **Repeated lines are read once** for the conditions list (`comorbidity`) and the background facts
   (`background`): `skip_repeated_lines: true` in `config/fields/pilot_fields.yaml`. A list line already read in an
   earlier note of the same patient is left out, and a note with nothing new for the group is not read. Prose is
   never cut. Not used for groups where the same line can mean a new event (lab results, MRIs, treatments,
   infections) or where the latest mention counts (diagnosis, still on a drug). The dry run prints how many lines it
   left out.

`config/settings.yaml` is merged over `config/settings.example.yaml`: a value that is not in your file takes the
new default. If your file sets `num_ctx: 8192` or `num_predict: 1024`, change them (or delete the two lines).
After a run, `python -m nmosd timing --cohort 220` shows where the time went and the time for 220 patients; the
`Time:` line under "Done:" gives the run's length.

**Step 1 for every patient: the diagnosis check.** The app opens each patient on "Diagnosis check". The
REDCap instruments stay hidden until the reviewer confirms both:

- a documented, confirmed **NMOSD** diagnosis. The proposal comes from the most recent clinic letters and
  hospital notes. A copied-forward Dx line counts at the date of the latest note that repeats it. It is not
  MOGAD or MS, and less often autoimmune encephalitis or a spinal cord infarct;
- **at least 12 months of follow-up**, from the diagnosis date to the last contact. A contact is any of:
  - a clinical note, including infusion notes;
  - a patient message or outreach note;
  - an infusion or appointment date that a message gives.

The screen shows every diagnosis statement found (newest first), the AQP4-IgG and MOG-IgG results (AQP4-IgG
positivity is usually diagnostic), and the follow-up arithmetic. It flags disagreements, such as MOG-IgG
positive with no positive AQP4-IgG result. Accept or correct the two answers. A patient whose answer is not
NMOSD, or who has less than 12 months of follow-up, is **excluded**: nothing else needs review and the patient
is never exported. The rules, including the follow-up definition, are drafts in `config/eligibility.yaml`.

Patients often have several diagnoses. A co-existing one, such as Sjögren's or lupus, is listed but never
replaces NMOSD. Only MOGAD, MS, autoimmune encephalitis or a spinal cord infarct compete with it. **Borderline
cases** are marked in the patient list and on the screen, with the reasons. A case is borderline when any
flag is raised, or when follow-up is within 3 months of 12. For a borderline case:

- tick "Look at the other instruments (read-only)" to see the relapses, treatments and antibody tests first;
- excluding it needs a written reason. The app refuses the exclusion without one. Including it, or marking it
  unresolved, needs no reason.

Nothing is ever excluded until a reviewer decides.

If an extraction database was built before 10.11.26, run the diagnosis group again first. Its items now
include the diagnosis each note gives. Also run the new `followup` group, which collects the infusion and
appointment dates in patient messages:

```bash
python -m nmosd extract --groups diagnosis,followup --dry-run
python -m nmosd extract --groups diagnosis,followup
python -m nmosd reconcile                  # then restart `nmosd review`; earlier review decisions are kept
```

Sex, race, ethnicity and living/deceased come from the RPDR Demographics table only (16.11.26; the same table
as `csv_export/Demographics.csv`, read from the `.accdb`). The model is not asked about them, so the
`demographics` field group no longer runs with a plain `extract`. `Gender_Legal_Sex` is used when `Sex_At_Birth`
is empty or has no REDCap code, and `Race_Group` likewise for `Race1`; such a value, or a missing one, is listed
under "Outstanding conflicts" with the column it came from. To pick up this change on an existing extraction
database, only `reconcile` is needed (then restart `nmosd review`).
To check a field against the notes again, set `note_check: true` on it in `config/fields/pilot_fields.yaml` and
run `python -m nmosd extract --groups demographics`, then `reconcile`.

**Comorbidities (13.11.26).** The `comorbidity` field group reads the medical history and problem lists and fills
the REDCap `comorbidities` checkbox (and `autoimmune` when an autoimmune disease is ticked). In review, the
demographics page lists every condition found under "Conditions found in the notes", with the REDCap category it
was given, including conditions outside the categories. Run times are per field group: only a group whose own
rules changed (or a new group) runs again, so a plain `extract` (or step 7 of the guided app) now runs only
`comorbidity` (the `demographics` group no longer runs, see above). To add comorbidities alone:

```bash
python -m nmosd extract --groups comorbidity --dry-run
python -m nmosd extract --groups comorbidity
python -m nmosd reconcile                  # then restart `nmosd review`
```

`reconcile` on its own also brings in the other 13.11.26 changes: treatment start dates from "on X since 2019",
an estimated NMOSD diagnosis date (code 4, flagged) when no note states one, and ethnicity from the table only.

## 7b. Quality check by Claude (optional, 16.11.26)

After `reconcile` (and, if you like, after reviewing in the app):

```bash
python -m nmosd quality-packet             # asks for the linkage passphrase (or set NMOSD_LINKAGE_PASSPHRASE)
```

This writes `~/Documents/NMOSD quality review/packet_<date>_<time>/`, one `Patient_A.md`, `Patient_B.md`, ... per
patient. Each holds every proposed value with its codebook number, your review decision if any, and the evidence
passages with some context. For empty fields and possible missed episodes it adds passages from the notes that were
read. It is built to the HIPAA Safe Harbor standard:
- no date element but the year: dates become tokens such as `[[2019 D05]]`, and the same date is always the same
  token;
- no study or document IDs (Patient A, Note 1);
- ages over 89 become 90+;
- residual patterns are scrubbed;
- every patient's known identifiers are searched for, and nothing is written if any check fails.

It refuses notes kept without de-identification, and notes whose de-identification is not signed off (step 6).
`--ner` also runs the name-recognition models (slower). The key from Patient A / Note 1 back to the study and
document IDs is saved next to the de-identified database, not in the packet. Whether you may share the packet is for
MGB (privacy office / IRB) to decide. Then ask Claude to assess the packet; it reads only that folder (CLAUDE.md).
**Safe to paste:** the command's output (counts only).

**Whole notes (18.11.26).** For Claude to work out what every REDCap field *should* have been, not only check the
proposals, add the whole text of every note:

```bash
python -m nmosd quality-packet --whole-notes --ner
```

This adds `Patient_A_notes_01.md`, `Patient_A_notes_02.md`, ... (every note in date order, about 150,000
characters per file), scrubbed and checked exactly like the passages, with the same date tokens and note numbers.
`--ner` runs the name-recognition models over every note again: slow (count on roughly as long as the
de-identification of these patients took) but worth it now that whole notes are shared. The key now also maps each
date token (`D05`) back to its date. If a check fails, nothing is written and the message says whether the hit was
in the whole notes. Then tell Claude the packet folder's name.

## 7c. Score against reference answers, and the fast test (18.11.26)

Reference answers are files `Patient_A.json`, ... that Claude made from a whole-notes quality packet (what every
REDCap field should be). They live outside the repository, in `<packet folder>/Claude_reference/`.

```bash
python -m nmosd score --list          # the full pilot's latest Combine run against the reference answers
python -m nmosd score --back 1        # the Combine run before it (before/after a change)
python -m nmosd fast-test --list      # extract + combine + score on the fast set only
```

`score` prints, per instrument and per patient: instances in the reference, proposed and paired; fields correct,
wrong, missed (the notes give a value, the tool none), unsupported (the tool gives a value the notes do not) and
not comparable; recall and precision. `--list` adds every field that is not correct, by patient letter, instrument
and field name. **Safe to paste:** all of it (counts and field names; no values, dates or IDs).

`fast-test` reads only the notes each reference file lists as its fast set (the notes that carry the answers, plus
a few that once misled the tool: 68 notes for the pilot instead of 440) and writes to its own file
(`<extraction database>_fast`), so the full results are never touched. Work already done with the same model,
prompts and rules is not repeated: after changing one field group, only that group is asked again.

Three levels of testing after a change: (1) rules and code checks: `reconcile` then `score` (about a minute, no
model); (2) prompts: `fast-test` (minutes); (3) before trusting a number: the full pilot overnight, then `score`.
A fast-set score is higher than the tool's real accuracy, because the model is handed the right notes: use it to
see whether a change helped. If a change helps on the fast set but not on the full pilot, the problem is choosing
the notes, not reading them.

To see why a fast-test value was wrong or missed (19.11.26), write a quality packet of the fast test's results:

```bash
python -m nmosd quality-packet --fast --reference "<packet folder>/Claude_reference" --out "<quality-review folder>"
```

`--fast` reads the fast test's results file (`<extraction database>_fast`) with a review file of its own, so no
review decision is laid over the proposals. `--reference` gives the dates the same tokens as in the packet the
reference answers were written from, so the two can be laid side by side; a date that packet did not have carries
a letter (`D05a` falls after D05 and before D06). The packet passes the same checks as any other.

## 7d. How lab results are written, across every patient (18.11.26)

```bash
python -m nmosd lab-shapes            # the whole export (paths.source): every patient's notes, and its Lab table
python -m nmosd lab-shapes --staged   # only the patients staged so far
```

For CD19/CD20, immunoglobulins, lymphocyte counts and the AQP4 and MOG antibodies it prints how many notes and
patients have such a line, and the most common *shapes* of those lines (numbers become 9, words become A/Aa/a;
only laboratory words such as `CD`, `IgG`, `Titer`, `mg/dL` are kept), separately for table rows and sentences.
For the export's laboratory table (`Lab`/`Labs`) it prints the column names and the matching test names with row
and patient counts; it never reads a result. A shape or test name is shown only when at least 3 notes (rows) of
at least 2 patients share it. **Safe to paste:** all of it. Claude uses it to write readers for lab tables.

`python -m nmosd shapes` is the same search for all the structured data the instruments are filled from: lab
results as above, plus immunotherapy administrations, orders and medication-list rows, weight and height,
diagnosis and problem-list rows, disability scores (EDSS, mRS, timed walk), MRI study lines, attack lists and
admission or discharge date lines. With the export as its source it also lists every structured table's row and
patient counts and column names, and for a medication table how many rows and patients name each maintenance
drug. Read the output once before pasting: a kept word is always a drug, test, unit or heading word, but check
that nothing in it reads like a name.

## 8. Export and re-identify

```bash
python -m nmosd export                       # $PHI/deid/export/redcap_import_deidentified.csv
python -m nmosd reidentify --export "$PHI/deid/export/redcap_import_deidentified.csv" \
    --record-map "$PHI/work/record_map.csv" --add-identifiers      # approved environment only
```
Only patients whose diagnosis check is **included** are exported. The summary counts the patients excluded
at the check, and the patients whose check is not done or unresolved (a warning). Only accepted or corrected
values are exported. The summary also counts, without values, the fields left out because they were never
reviewed or are unresolved, and per field the exported values with no note text behind them. Check those
before importing.

**Format.** With `paths.redcap_template` set (§6b), the export copies that file's header exactly: every
column, in the same order and under the same record-ID name. It also copies its encoding, line endings and
quoting. Columns we never fill stay blank, including calculated fields and `mrn`/`dob` until `reidentify`.
`reidentify` keeps the same layout. A filled column that the template lacks is a validation error. Pass
`--no-template` for REDCap's standard order instead. In the Data Import Tool, keep "allow blank values to
overwrite existing values" set to **No**, because the blank columns must not erase anything.
`record_map.csv` has columns `empi,record_id[,mrn]` (PHI location). Import
`$PHI/work/reidentified/redcap_import_final.csv` into the **test** REDCap project with the Data Import Tool.

## 9. Evaluate

Export the existing REDCap records for the same 5 patients as CSV (raw codes, `YYYY-MM-DD` dates), save it in the
PHI folder, then:

```bash
python -m nmosd evaluate --gold "$PHI/work/redcap_gold.csv" \
    --predicted "$PHI/deid/export/redcap_import_deidentified.csv" \
    --linkage "$PHI/work/linkage/linkage.sqlite.enc" --record-map "$PHI/work/record_map.csv" \
    --label qwen3:8b --report "$PHI/work/reports/eval_qwen3.md"
```
**Safe to paste:** the printed report (aggregate metrics only). Repeat with each benchmark model's export to
compare models. The project export from §6b already has this format and can serve as the gold file, because
records missing from the record map are ignored. Metrics cover the patients present in both files. The
report counts the patients in the gold file who were not exported, for example because they were excluded at
the diagnosis check.

### 9b. Accuracy from the review (for a paper)

Every review decision is already saved (accept, correct with the old and new value, unresolved, add or delete an
instance). This turns them into accuracy tables at any point of the review. In the guided app this is step 10,
**Export tool accuracy**: **Export the accuracy table**, then **Download the accuracy tables (.zip)** (the
counts-only files).

```bash
python -m nmosd accuracy --label qwen3:8b
```
Each run writes a new folder `<reports>/accuracy/accuracy_YYYYMMDD_HHMMSS/` (`--out` to choose another place):
`accuracy_report.md`, `accuracy_overall.csv`, `accuracy_by_instrument.csv`, `accuracy_by_field.csv` and
`episodes.csv` (counts only), and `decisions_with_study_ids.csv` (one row per decision, with study IDs but no values
or dates; for a patient-level analysis; **keep it private**). Each field is scored against the reviewer's final value:
correct, correctly empty, wrong value, filled when it should be empty, or missed. Episodes (attacks, admissions,
MRIs, ...) are scored as confirmed, deleted (wrongly proposed) or added (missed). Unresolved and not-yet-reviewed
fields are counted but left out of the rates. The reviewer sees each proposal, which can raise agreement; §9
(`evaluate`) compares with REDCap entries made without the tool.
**Safe to paste:** the printed report and the `accuracy_*.csv` / `episodes.csv` files (counts only).

---

## Synthetic demo (no PHI)

```bash
python scripts/demo_synthetic.py           # builds everything in a temp folder and prints the review command
```

The quick demo uses a deterministic stand-in instead of the LLM. For a **full rehearsal with the local LLM**,
run every ordinary command on the synthetic data:

```bash
python scripts/demo_synthetic.py --out ~/nmosd_synthetic_run --prepare-only --model gemma3:12b
```

This writes the synthetic data and `~/nmosd_synthetic_run/settings.yaml`, with every path inside that folder.
It then prints the commands in order: stage, deid, leak-test, extract, reconcile, review, export, reidentify
and evaluate. The same list is saved in `next_steps.txt`. Every command takes `--settings settings.yaml` and
runs from inside that folder, so none of them can use `config/settings.yaml`. Run from anywhere else, a
command stops with "Settings file not found". The linkage passphrase is read from
`NMOSD_SYNTHETIC_PASSPHRASE`, never from the real `NMOSD_LINKAGE_PASSPHRASE`.

The synthetic run makes about 400 LLM calls, so start with the printed `extract --limit-docs 10` check. Its
"Done:" line should show quotes verified near 100%, invalid JSON 0 and LLM errors 0. Then run the rest. The
evaluation compares the export with the synthetic answers, so it measures the model plus your review.
Rehearsal outputs are synthetic and safe to paste back.
