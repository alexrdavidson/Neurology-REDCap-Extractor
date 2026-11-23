# TODO

[docs/RUNBOOK.md](docs/RUNBOOK.md) has every command in order and says which output is safe to paste back.
The guided app (`Neurology REDCap Extractor.app`, or double-click `Start Neurology REDCap Extractor.command`) does the
same steps on screen: choose **NMOSD** on its start screen.

- [ ] (19.11.26) **Try the app window on the Mac.** Double-click `Neurology REDCap Extractor.app` in the app folder (made
  with `.venv/bin/python scripts/make_mac_app.py`; drag it to the Dock if you like). It should open its own window on
  the condition list; choose NMOSD and check that step 2 still shows your files (same `config/settings.yaml`). Close
  the window: the app stops; a running extraction carries on. If it doesn't open: `~/Library/Logs/Neurology REDCap
  Extractor/`.
- [ ] (19.11.26) **Windows**: run `Install Neurology REDCap Extractor` again (adds the window package, makes the new
  icon, removes the old NMOSD Extractor icon), then check the icon opens the window with no black console window.
- [ ] (19.11.26) **GitHub**: `gh auth login`, then push to the private repo `Neurology-REDCap-Extractor` (commands in the
  session). Quality-review packets, settings and data are git-ignored.
- [ ] (19.11.26) **Second condition**: pick which one is set up first, and get its REDCap data dictionary. The
  extraction rules are still NMOSD's (BUILD_PLAN Phase 13, "Not done").

- [ ] (16.11.26) Every codebook field 1-303 is now extracted (BUILD_PLAN Phase 11). Restart the app, then in step 7
  press **Preview the amount of work** (the new groups add roughly two thirds more model calls: run it overnight),
  then **Continue**. Step 7 now runs Extract → Combine → Disability → Combine again. Then paste back the two
  "Done:" lines and the output of `python -m nmosd coverage` (counts only) so the prompts can be tuned for the
  fields still empty. Re-check the pilot feedback: the comorbidities of NMO-4xPCRM (problem list), `otherdx`,
  `date_maintenance_estimate`, one Ravulizumab course with `still_on_therapy` from the latest notes, the positive
  AQP4, and the notes search.
- [ ] (16.11.26) Quality check by Claude: after the new extraction (and once MGB's privacy office / IRB is happy
  for the packet to be shared), run `python -m nmosd quality-packet` (RUNBOOK §7b), then ask Claude to assess the
  packet in `~/Documents/NMOSD quality review/`.
- [x] (18.11.26) Reference answers from Claude for the 5 pilot patients: done from `packet_20261118_1521`
  (`quality-packet --whole-notes`). Copy the five reference files from the session scratchpad into the packet
  folder (`Claude_reference/`) if not done yet: the scratchpad is cleared on restart.
- [ ] (18.11.26) **Combine the pilot again before judging accuracy.** The proposals in that packet were made by a
  Combine run older than the 16.11.26 rules (only relapse, treatment and antibody instances; no "How derived"
  lines). Run `python -m nmosd extract --dry-run` (paste the counts), then step 7 **Continue** in the app (or
  `reconcile`, `extract`, `reconcile`), then `python -m nmosd coverage` and a new `python -m nmosd quality-packet`.
- [ ] (18.11.26) A clinician spot-checks the reference files (summary, relapse and treatment tables, open questions).
- [ ] (18.11.26) **Fast test.** Copy the reference answers (five `Patient_X_reference.md` and five `Patient_X.json`)
  from the session scratchpad into `~/Documents/NMOSD quality review/packet_20261118_1521/Claude_reference/`, then
  after any change run `python -m nmosd fast-test --list` (68 chosen notes instead of 440; RUNBOOK §7c) and paste
  the tables. `python -m nmosd score --list` scores the full pilot; `--back 1` scores the Combine run before it.
- [ ] (18.11.26) **Run the fast test on the rebuilt tool** and paste the score tables:
  `python -m nmosd fast-test --list --reference "<quality review folder>/packet_20261118_1521/Claude_reference"`.
  Then, if it helped, the full pilot overnight (step 7; nearly every group runs again, as its rules changed).
- [ ] (18.11.26) **Add more patients to the fast test.** It is tuned on the same 5 pilot patients, so the tool can
  look better on them than on the rest. De-identify and sign off a few more patients (ones the prompts were never
  tuned on), make a `quality-packet --whole-notes` for them, and ask Claude for their reference answers
  (`Patient_F.json`, ...) so `fast-test` covers them too. Fix the packet's date flaw first (year-less month/day
  dates are read as month-year).
- [ ] (18.11.26) Hospital names: with the original notes (the 220-patient cohort), the new `hospital` group reads
  `hx_opcare`, `nr_hosp` and `nr_hosp_2` from the notes (BUILD_PLAN Phase 11). Nothing to do on the de-identified
  pilot: its notes have no hospital names, so step 7 leaves the group out. For the cohort, step 7's preview lists
  the group's notes; if the count is large, say so and the keywords can be narrowed. Clinician sign-off of the
  `hospital` group in `config/fields/pilot_fields.yaml`.
- [ ] (18.11.26) Accuracy for the paper: while or after reviewing (step 8), open step 10 **Export tool accuracy** in
  the guided app, press **Export the accuracy table**, then **Download the accuracy tables (.zip)** (or
  `python -m nmosd accuracy --label qwen3:8b`, RUNBOOK §9b). Each export is a new dated folder in
  `<reports>/accuracy/`. Paste back the printed report (counts only) if you want help with the paper's tables. Keep
  `decisions_with_study_ids.csv` private (it is not in the zip).
- [ ] (16.11.26) Confirm in REDCap (Project Setup → Repeating instruments) that **Imaging** repeats like the other
  forms (the export assumes every form but Demographics repeats).
- [ ] (16.11.26) Clinician sign-off of the new field groups in `config/fields/pilot_fields.yaml` (admission,
  admission_course, imaging x3, treatment_detail, labs, ivig, infection, background, death, the four EDSS groups,
  vitals) and of the derived fields (`src/nmosd/redcap/sources.py`). Choices to confirm: weight/height nearest the
  diagnosis date; EDSS at each treatment start/stop and the last follow-up; hidden REDCap fields are proposed too
  (marked "hidden on the REDCap form").
- [x] (13.11.26) After the first-draft review feedback: run `python -m nmosd extract --groups comorbidity`
  (new field group; or step 7 of the guided app, which now runs only `comorbidity`), then
  `python -m nmosd reconcile`, then restart the review app (RUNBOOK §7). Re-check: the rituximab start date,
  the "Conditions found in the notes" list, `date_nmo_diagnosis`, the evidence highlights and counts.
  Get the clinician to sign off the new `comorbidity` specs in `config/fields/pilot_fields.yaml`.
- [ ] (13.11.26) De-identification is now optional. For the rest of the patients without it: in the guided app,
  step 3 "Every patient in the RPDR export", then step 4 or 5 "No: keep the notes as they are", then steps 5,
  7, 8, 9 (4 and 6 are skipped). The 5 pilot patients were de-identified: step 5 stops for them until you tick
  "Switch them", which re-runs their extraction and means re-reviewing their dates. Make the pilot's final
  file (step 9) first if you want to keep the pilot as it is. RUNBOOK §5b has the commands.
- [ ] (12.11.26) Pilot on this Mac, in the guided app: step 7 (extract with qwen3:8b; let it run), step 6 (read the
  de-identification report and add the sign-off), step 8 (review the 5 patients), step 9 (make the REDCap file,
  then "Compare with the values already in REDCap" with a `record_map.csv` of `empi,record_id` for the 5
  patients). Paste back: the extraction "Done:" line and the comparison table (counts and percentages only).
- [ ] (12.11.26) Supervisor's laptop: send the newest zip in `dist/` (from 19.11.26 `Neurology_REDCap_Extractor_<date>.zip`;
  rebuild with `python scripts/make_release.py` after any change), unzip, double-click **Install Neurology REDCap
  Extractor**, then the desktop icon. The Windows installer has not been run on Windows yet: be there for the first install, and paste
  back anything in yellow or red.

- [ ] Updates of 10.11.26 (see §1c of [BUILD_PLAN.md](BUILD_PLAN.md)):
  - [ ] Copy the prepared settings into place (again, 11.11.26: the copy now has `export.date_format: M/D/YYYY`,
    the format the profile suggested). It also has every real path, including `redcap_template`:
    `cp "/private/tmp/claude-501/-Users-alex-Documents-Medicine-Year-14---PhD-Year-3-Harvard-placement-NMOSD-Extractor/860fd43c-1911-4d2c-a39f-02db9278e311/scratchpad/settings.yaml" config/settings.yaml`
    The `/private/tmp` folder is emptied on restart, so copy it before rebooting.
  - [x] Run `python -m nmosd profile-redcap-csv` and paste the output (received 10.11.26)
  - [x] Run it once more for the exact date style (11.11.26): `M/D/YYYY`, month first with no leading zeros.
  - [ ] Clinician sign-off of `config/eligibility.yaml` (change `status` to `signed off <name> <date>`). These
    decisions are already in it: follow-up from diagnosis (either start is fine); clinical notes, patient messages
    and the infusion/appointment dates they give count as follow-up; co-existing diagnoses allowed; "not enough
    information" excludes; seronegative flagged not excluded; borderline cases need a reason to exclude.
  - [ ] If an extraction DB already exists on real data: `extract --groups diagnosis,followup`, then `reconcile`,
    then restart `review`. Every patient now starts with the diagnosis check (RUNBOOK §7).
- [x] Run `python -m nmosd inspect` (11.11.26: 32 patients, 6 note tables, no truncation)
- [ ] Run `python -m nmosd benchmark` on Shamik's laptop.
- [ ] (11.11.26) On the Windows laptop, once `config\settings.yaml` is set: `.venv\Scripts\python scripts\make_desktop_shortcut.py`, then double-click **NMOSD Extractor** and check that the review app opens (RUNBOOK §0 step 6).
- [ ] Save the REDCap data dictionary CSV as `config/redcap/data_dictionary.csv`. The skipped tests then run against it automatically.
- [ ] Get a clinician to sign off `config/fields/pilot_fields.yaml`, including the structured demographics (sex, race,
  ethnicity, living/deceased from the RPDR Demographics table only since 16.11.26, with `Gender_Legal_Sex` and
  `Race_Group` as fallbacks) and the new `followup` group (dates in patient messages).
- [ ] If real data was de-identified with NER before 2026-11-10: re-run `deid` and `leak-test`. Before the
  token-window fix, long whitespace-heavy notes could overrun the 512-token NER limit.
- [x] ~~`extract --groups demographics` on real data~~: no longer needed (16.11.26). Demographics come from the
  RPDR Demographics table only; `reconcile` (or step 7 of the guided app) picks this up.
- [ ] Answer the open questions in §6 of the plan ([BUILD_PLAN.md](BUILD_PLAN.md)):
  - [ ] Whether RPDR can supply `Med`, `Lab`, `Lno`, `Mrn`, `Con` tables for the same cohort
  - [ ] The 5 pilot EMPIs and their `identifiers.yaml`
  - [ ] Confirmation of the header-line labels (e.g. "line 3 = age, sex, MRN, DOB"); labels only, never values
  - [x] Whether the REDCap project has longitudinal events: no (profile of 10.11.26)
  - [ ] Which `@HIDDEN` fields are still in use
  - [ ] Whether hospital names should be tokenised or kept (affects `nr_hosp_2`, `hx_opcare`). Decided by the
    original-notes run for the cohort (18.11.26: read from the notes); still open only for de-identified notes
  - [ ] Benchmark results → which model is the default
  - [x] (10.11.26) Follow-up counted from diagnosis or from the first MGB visit: either is fine (diagnosis used)
  - [x] (10.11.26) Which encounters count as follow-up: clinical notes incl. infusion notes, and patient messages with the dates they give
  - [x] (10.11.26) Seronegative NMOSD: flagged for a closer look, not excluded
  - [x] (10.11.26) Should excluded patients (e.g. MOGAD) be recorded in REDCap at all: no
  - [ ] (10.11.26) Are the 32-character `record_id` values in the project export the real record IDs, or hashed by the export?

`scripts/demo_synthetic.py` runs everything on fake data and prints a command that opens the review app.
With `--prepare-only` it sets up a full rehearsal with the local LLM instead (RUNBOOK, "Synthetic demo"):

- [ ] (11.11.26) Synthetic rehearsal with the local LLM: `python scripts/demo_synthetic.py --out ~/nmosd_synthetic_run --prepare-only --model gemma3:12b`, then the printed commands. Paste back the `extract` "Done:" line and the evaluation report (synthetic, safe).

## 17.11.26: speed-ups (BUILD_PLAN Phase 11, RUNBOOK §7 "Speed")
- [ ] Check `config/settings.yaml` for `num_ctx: 8192` / `num_predict: 1024`: change to 10240 / 3072, or delete the lines.
- [ ] `launchctl setenv OLLAMA_NUM_PARALLEL 4`, quit and reopen Ollama (again after each restart of the Mac).
- [ ] Next step 7 run: paste the Done and Time lines, and `nmosd timing --cohort 220`.
- [ ] Supervisor's Windows laptop: does it have a dedicated graphics card? If not, `llm.parallel: 1` there.
