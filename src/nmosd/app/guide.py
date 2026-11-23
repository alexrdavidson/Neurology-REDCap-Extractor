"""Neurology REDCap Extractor: the guided app. Start it with `nmosd app --window` (or the Mac app / desktop shortcut).

Streamlit on 127.0.0.1 only. It opens on the choice of condition (config/conditions.yaml). A condition that is set up
opens two pages: the step-by-step guide, and the review screens; the others explain what is needed to set them up.
Long steps run as background jobs (`nmosd.app.jobs`) that carry on when the window is closed.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import streamlit as st
import yaml

from nmosd import conditions as cnd
from nmosd.app import checks, jobs
from nmosd.app import workspace as ws
from nmosd.app.window import APP_NAME
from nmosd.config import REPO_ROOT, SETTINGS_ENV
from nmosd.extract.prefilter import second_pass_groups
from nmosd.safety import SafetyError, ensure_phi_output

ICON = REPO_ROOT / "assets" / "icon" / "nmosd_extractor.png"
RUNNING = {"running", "starting"}
STEPS = [
    ("check", "Check this computer"),
    ("files", "Choose your files"),
    ("patients", "Choose patients"),
    ("identifiers", "Confirm names and numbers"),
    ("deid", "Remove identifiers"),
    ("signoff", "Check the de-identification"),
    ("extract", "Extract with the local AI"),
    ("review", "Review the results"),
    ("export", "Export for REDCap"),
    ("accuracy", "Export tool accuracy"),
]
ICONS = {"done": "✅", "warn": "⚠️", "running": "⏳", "todo": "⬜"}


def _explicit_settings() -> Path | None:
    """A settings file given when the app was started (`nmosd --settings X app`, or $NMOSD_SETTINGS). It is used for
    the condition that is opened instead of that condition's own file (config/conditions.yaml)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings")
    a, _ = ap.parse_known_args(sys.argv[1:])
    if a.settings:
        return Path(a.settings)
    return Path(os.environ[SETTINGS_ENV]) if os.environ.get(SETTINGS_ENV) else None


# The condition chosen on the start screen, for this window only (a new window starts at the choice again).
CONDITIONS = cnd.load_conditions()
CONDITION = cnd.find(CONDITIONS, st.session_state.get("condition"))
OPEN = CONDITION is not None and CONDITION.ready
SETTINGS_PATH = (_explicit_settings() or cnd.settings_path(CONDITION)) if OPEN else None
S = ws.load(SETTINGS_PATH) if SETTINGS_PATH else ws.defaults()
CONFIGURED = OPEN and ws.is_configured(S)
SETTINGS_ERROR = ""
try:
    PHI_JOBS = jobs.phi_job_dir(S) if CONFIGURED else None
except (SafetyError, jobs.JobError) as _e:
    PHI_JOBS, CONFIGURED, SETTINGS_ERROR = None, False, str(_e)
FOLDERS = [jobs.SETUP_JOB_DIR, *([PHI_JOBS] if PHI_JOBS else [])]
PASS_ENV = S.get("deid.passphrase_env") or "NMOSD_LINKAGE_PASSPHRASE"


# ============================================================================================== helpers
def P(key: str) -> Path:
    p = S.path(key)
    assert p is not None, key
    return p


def job(name: str) -> jobs.Job | None:
    folder = jobs.SETUP_JOB_DIR if name.startswith("setup_") else PHI_JOBS
    return jobs.read(folder, name) if folder else None


def busy() -> jobs.Job | None:
    return jobs.running(FOLDERS)


def flash(kind: str, msg: str) -> None:
    st.session_state.setdefault("g_flash", []).append((kind, msg))


def show_flash() -> None:
    for kind, msg in st.session_state.pop("g_flash", []):
        getattr(st, kind)(msg)


def start_job(name: str, title: str, steps: list[jobs.Step], env: dict[str, str] | None = None) -> None:
    """Button callback: start a background job, or say why not."""
    folder = jobs.SETUP_JOB_DIR if name.startswith("setup_") else PHI_JOBS
    if folder is None:
        flash("error", "Choose your files first (step 2).")
        return
    try:
        jobs.start(folder, name, title, steps, SETTINGS_PATH, env, busy_folders=FOLDERS)
    except jobs.JobError as e:
        flash("error", str(e))


def no_ner_args() -> list[str]:
    return [] if (S.get("deid.ner") or {}).get("enabled", True) else ["--no-ner"]


def deid_on() -> bool:
    """The user's choice (settings `deid.enabled`, 13.11.26): de-identify the notes, or keep them as they are."""
    return bool(S.get("deid.enabled", True))


DEID_CHOICES = {True: "Yes: remove names, numbers and other identifiers, and shift the dates",
                False: "No: keep the notes as they are (they never leave this computer)"}


def _save_deid_choice(key: str) -> None:
    on = st.session_state[key] == DEID_CHOICES[True]
    raw = ws.read_raw(SETTINGS_PATH)
    raw["deid"] = {**(raw.get("deid") or {}), "enabled": on}
    ws.save(SETTINGS_PATH, raw)
    flash("success", "Saved: the notes will be de-identified." if on else
          "Saved: the notes will be kept as they are. Steps 4 and 6 are not needed.")


def deid_choice(key: str) -> bool:
    """Ask whether to de-identify the notes (optional). Returns the saved choice."""
    on = deid_on()
    st.radio("De-identify the notes?", list(DEID_CHOICES.values()), index=0 if on else 1, key=key,
             on_change=_save_deid_choice, args=(key,),
             help="Optional. Without it the local AI reads the original notes, on this computer only, and you review "
                  "them with real names and dates. The REDCap file uses study IDs until the last step puts your "
                  "record IDs back.")
    return on


def notes_deidentified(m: dict) -> bool:
    """What step 5 actually did last time (a database from before 13.11.26 was always de-identified)."""
    return bool(m.get("deidentified", True))


def output(name: str, label: str) -> str:
    j = job(name)
    return jobs.section(jobs.tail(j), label) if j else ""


def show_job_result(name: str, label: str | None = None, ok_text: str = "Finished.", markdown: bool = False) -> None:
    j = job(name)
    if j is None or j.state in RUNNING:
        return
    text = jobs.tail(j)
    body = jobs.section(text, label) if label else text
    when = f" ({j.ended.replace('T', ' ')})" if j.ended else ""
    if j.state == "done":
        st.success(ok_text + when)
    elif j.state == "stopped":
        st.warning("Stopped before the end" + when + ".")
    elif j.state == "interrupted":
        st.warning("This did not finish: the computer may have restarted or the process was closed. Start it again.")
    else:
        st.error("This did not finish: see the details below" + when + ".")
    if body.strip():
        if markdown:
            st.markdown(body)
        else:
            st.code(body[-6000:], language=None)


def open_path(p: Path) -> None:
    """Open a file or folder on this computer (the app runs on the same computer as the browser)."""
    if p.suffix.lower() in {".html", ".htm"}:
        webbrowser.open(p.resolve().as_uri())
    elif os.name == "nt":
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


@st.cache_resource
def can_browse() -> bool:
    try:
        return subprocess.run([sys.executable, "-c", "import tkinter"], capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


BROWSE = """
import sys, tkinter as tk
from tkinter import filedialog as fd
r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True)
kind, init = sys.argv[1], sys.argv[2] or None
p = fd.askdirectory(initialdir=init, mustexist=False) if kind == "folder" else fd.askopenfilename(initialdir=init)
print(p or "")
"""


def _browse(key: str, kind: str) -> None:
    cur = Path(clean_path(st.session_state.get(key, "")) or Path.home())
    init = str(cur if cur.is_dir() else cur.parent if cur.parent.exists() else Path.home())
    try:
        r = subprocess.run([sys.executable, "-c", BROWSE, kind, init], capture_output=True, text=True, timeout=900)
        picked = r.stdout.strip()
    except Exception:
        picked = ""
    if picked:
        st.session_state[key] = str(Path(picked))


def clean_path(text: str) -> str:
    return (text or "").strip().strip('"').strip("'").strip()


def path_input(label: str, key: str, current: Path | None, kind: str, help: str | None = None) -> str:
    if key not in st.session_state:
        st.session_state[key] = str(current) if current else ""
    c1, c2 = st.columns([6, 1], vertical_alignment="bottom")
    c1.text_input(label, key=key, help=help)
    if can_browse():
        c2.button("Browse…", key=f"{key}_browse", on_click=_browse, args=(key, kind))
    return clean_path(st.session_state[key])


def read_manifest() -> dict:
    deid = S.path("deid_db")
    if not deid or not deid.exists():
        return {}
    from nmosd.store.db import connect, get_meta

    try:
        con = connect(deid, readonly=True)
        try:
            return get_meta(con, "manifest")
        finally:
            con.close()
    except Exception:
        return {}


def leak_result(text: str) -> tuple[int, bool] | None:
    m = re.search(r"Known-identifier hits: (\d+) \((PASS|FAIL)\)", text)
    return (int(m.group(1)), m.group(2) == "PASS") if m else None


def outside_warning() -> bool:
    """Warn (and return True) if a heavy pipeline command started outside the app is still running."""
    if busy() is not None:
        return False
    outside = checks.outside_runs()
    if outside:
        st.warning(f"A pipeline command started outside the app is still running ({', '.join(outside)}), most "
                   "likely in a Terminal window. Let it finish, or stop it there with Ctrl+C, before starting here.")
    return bool(outside)


def needs(ok: bool, msg: str) -> bool:
    if not ok:
        st.info(msg)
    return ok


# ============================================================================================== state
def states() -> dict[str, str]:
    out = {k: "todo" for k, _ in STEPS}
    res = checks.run_checks(S)
    out["check"] = "done" if all(c.ok for c in res if c.key != "memory") else "todo"
    if not CONFIGURED:
        return out
    out["files"] = "done"
    staging, empis, ident = P("staging_db"), S.path("empi_list"), P("identifiers")
    deid, extract = P("deid_db"), P("extract_db")
    if staging.exists() and ws.newer(staging, empis):
        out["patients"] = "done"
    keep = not deid_on()
    m = read_manifest()
    if keep:
        out["identifiers"] = "done"          # not needed
    elif ident.exists():
        out["identifiers"] = "done" if ws.newer(ident, staging) else "warn"
    dj = job("deid")
    if dj and dj.state in RUNNING:
        out["deid"] = "running"
    elif deid.exists() and ws.newer(deid, staging) and (keep or ws.newer(deid, ident)):
        leak_ok = keep or (dj.step_rc("Leak test") if dj else 0) == 0
        out["deid"] = "done" if leak_ok and notes_deidentified(m) != keep else "warn"
    if m.get("signoff_complete") or (keep and deid.exists() and not notes_deidentified(m)):
        out["signoff"] = "done"
    ej = job("extract")
    if ej and ej.state in RUNNING:
        out["extract"] = "running"
    elif ej and ej.state == "done" and extract.exists():
        out["extract"] = "done"
    elif extract.exists():
        out["extract"] = "warn"
    final = P("export_dir") / "redcap_import_deidentified.csv"
    if final.exists() and ws.newer(final, S.path("review_db")):
        out["export"] = "done"
    aj, snaps = job("accuracy"), accuracy_snapshots()
    if aj and aj.state in RUNNING:
        out["accuracy"] = "running"
    elif snaps:   # ticked when the latest export includes every review decision so far
        out["accuracy"] = "done" if ws.newer(snaps[-1] / "accuracy_report.md", S.path("review_db")) else "warn"
    return out


# ============================================================================================== running job
@st.fragment(run_every=3)
def running_panel() -> None:
    j = busy()
    if j is None:
        if st.session_state.get("g_was_running"):
            st.session_state["g_was_running"] = False
            st.rerun(scope="app")
        return
    st.session_state["g_was_running"] = True
    with st.container(border=True):
        st.markdown(f"**⏳ {j.title}**" + (f" · {j.current_label}" if j.current_label else ""))
        text = jobs.tail(j)
        pr = jobs.progress(jobs.section(text, j.current_label) if j.current_label else text)
        if pr:
            st.progress(pr.fraction, text=f"{pr.done} of {pr.total}" + (f" · {pr.eta_text}" if pr.eta_text else ""))
        else:
            st.caption("Working… (this part shows no progress bar)")
        st.caption("You can keep using the computer, and you can close this browser tab: the work carries on in "
                   "the background. Open the app again to see how far it has got.")
        with st.expander("Details"):
            st.code("\n".join(text.strip().splitlines()[-25:]), language=None)
        if st.button("Stop", key="g_stop_running"):
            jobs.stop(j.folder, j.name)
            st.rerun(scope="app")


# ============================================================================================== steps
def step_check() -> None:
    st.write("These parts are installed once. After that the app works without internet, and nothing ever leaves "
             "this computer.")
    info = checks.ollama_info(S)
    res = checks.run_checks(S, info)
    for c in res:
        st.markdown(f"{'✅' if c.ok else '❌'} **{c.label}**: {c.detail}")
        if not c.ok and c.fix:
            st.caption(c.fix)
    missing = {c.key for c in res if not c.ok}
    running = busy() is not None
    if "ner" in missing:
        st.button("Download the de-identification models (about 1 GB)", key="g_dl_ner", disabled=running,
                  on_click=start_job, args=("setup_ner", "Downloading the de-identification models",
                                            [jobs.nmosd("Download", "download-models")]))
    model = S.get("llm.model", "qwen3:8b")
    if "model" in missing:
        st.button(f"Download {model} (about 5 GB)", key="g_dl_model", disabled=running,
                  on_click=start_job, args=("setup_model", f"Downloading {model}",
                                            [jobs.nmosd("Download", "pull-model", "--model", model)]))
    for name in ("setup_ner", "setup_model"):
        j = job(name)
        if j and j.state not in RUNNING and j.state != "done":
            show_job_result(name)
    st.button("Check again", key="g_recheck")


def step_files() -> None:
    st.write("Two things: the **RPDR export** you received (an Access `.accdb` file), and **one private folder** for "
             "all the working files. Use a folder only you can open, outside the app folder, ideally on an "
             "encrypted drive.")
    st.caption("The app makes two folders inside it: `work` (identifiable: the patient list, the identifiers, the "
               "encrypted key that links results back to patients) and `deid` (the de-identified notes and results).")
    raw = ws.read_raw(SETTINGS_PATH)
    if SETTINGS_ERROR:
        st.error(f"The current settings can't be used: {SETTINGS_ERROR}")
    if CONFIGURED:
        st.success(f"Set up: **{P('source').name}** · private folder `{ws.data_folder(S)}`")
    src = path_input("RPDR export file (.accdb)", "g_src", S.path("source") if S.source else None, "file")
    ddir = path_input("Private data folder", "g_dir", ws.data_folder(S) if S.source else None, "folder")
    tpl = path_input("Your REDCap project's data export (CSV), optional", "g_tpl",
                     S.path("redcap_template") if S.source else None, "file",
                     help="Only its first line (the column names) is read, so the file the app makes has the same "
                          "columns in the same order. Leave empty to use REDCap's standard order.")
    cur_fmt = S.get("export.date_format", "M/D/YYYY")
    fmt = st.selectbox("How your REDCap export writes dates", ws.DATE_FORMATS,
                       index=ws.DATE_FORMATS.index(cur_fmt) if cur_fmt in ws.DATE_FORMATS else 0, key="g_fmt",
                       help="M/D/YYYY means e.g. 3/7/2021 for 7 March 2021.")
    if ddir and (w := ws.synced_folder_warning(Path(ddir))):
        st.warning(w)
    if st.button("Save", key="g_save_files", type="primary"):
        s_p, d_p, t_p = (Path(src) if src else None), (Path(ddir) if ddir else None), (Path(tpl) if tpl else None)
        probs = ws.problems(s_p, d_p, t_p)
        for p in probs:
            st.error(p)
        if not probs and s_p and d_p:
            d_p.mkdir(parents=True, exist_ok=True)
            ws.save(SETTINGS_PATH, ws.build_settings(raw, s_p, d_p, t_p, fmt))
            flash("success", "Saved.")
            st.rerun()


def _stage(all_patients: bool) -> None:
    if not all_patients:
        lst = ws.parse_empis(st.session_state.get("g_empis", ""))
        if not lst.empis:
            flash("error", "Add at least one EMPI.")
            return
        ws.write_empis(P("empi_list"), lst.empis)
    args = ["stage", "--all-patients"] if all_patients else ["stage"]
    start_job("stage", "Copying the patients' notes", [jobs.nmosd("Copy notes", *args)])


def step_patients() -> None:
    if not needs(CONFIGURED, "Choose your files first (step 2)."):
        return
    mode = st.radio("Which patients?", ["Only the patients I list", "Every patient in the RPDR export"],
                    key="g_pt_mode", horizontal=True)
    every = mode.startswith("Every")
    if not every:
        st.write("Paste the patients' **EMPIs** (the patient number in the RPDR export), one per line. You can copy "
                 "a column from a spreadsheet.")
        if "g_empis" not in st.session_state:
            st.session_state["g_empis"] = "\n".join(ws.read_empis(S.path("empi_list")))
        st.text_area("EMPIs", key="g_empis", height=180)
        lst = ws.parse_empis(st.session_state["g_empis"])
        st.caption(f"{len(lst.empis)} patients" + (f" · {lst.rejected} lines ignored (not a number)" if lst.rejected else ""))
    st.button("Copy these patients' notes into the private folder", key="g_stage", type="primary",
              disabled=busy() is not None, on_click=_stage, args=(every,))
    show_job_result("stage", "Copy notes", ok_text="Notes copied.", markdown=True)
    m = re.search(r"Staged (\d+) of (\d+) requested patients \((\d+) not found", output("stage", "Copy notes"))
    if m and int(m.group(3)):
        st.warning(f"{m.group(3)} of the EMPIs were not found in the RPDR export. Check the list.")


def _load_suggestions() -> None:
    sugg = P("identifiers").with_name("identifiers.suggested.yaml")
    if sugg.exists():
        st.session_state["g_ident_text"] = sugg.read_text(encoding="utf-8")


def _save_identifiers() -> None:
    text = st.session_state.get("g_ident_text", "")
    try:
        data = yaml.safe_load(text)
        ok = isinstance(data, dict) and isinstance(data.get("patients"), dict)
    except yaml.YAMLError as e:
        ok, data = False, None
        flash("error", f"The list can't be read any more (line {getattr(getattr(e, 'problem_mark', None), 'line', -1) + 1}). "
                       "Check the indentation around that line, or press 'Start again from the suggestions'.")
        return
    if not ok:
        flash("error", "The list must start with 'patients:' followed by one entry per patient.")
        return
    path = ensure_phi_output(P("identifiers"), "identifiers.yaml")
    path.write_text(text, encoding="utf-8")
    flash("success", f"Saved and confirmed ({len(data['patients'])} patients).")


def step_identifiers() -> None:
    if not needs(P("staging_db").exists(), "Copy the patients' notes first (step 3)."):
        return
    if not deid_choice("g_deid_choice_4"):
        st.info("Not needed: you chose to keep the notes as they are. Go to step 5.")
        return
    st.write("The app collects each patient's names, record numbers, date of birth and address from the note "
             "headers. **Check the list, add what is missing** (nicknames, relatives' names, phone numbers, email "
             "and street addresses) and save it. Everything on the list is removed from every note in the next step.")
    ident = P("identifiers")
    sugg = ident.with_name("identifiers.suggested.yaml")
    st.button("Find names and numbers in the notes", key="g_suggest", disabled=busy() is not None,
              on_click=start_job, args=("identifiers", "Finding names and numbers",
                                        [jobs.nmosd("Find identifiers", "suggest-identifiers", "--out", str(sugg))]))
    out = output("identifiers", "Find identifiers")
    low = [(t, int(a), int(b)) for t, a, b in re.findall(r"^\s+(\w+): (\d+)/(\d+)$", out, re.M) if int(b)]
    if out:
        show_job_result("identifiers", "Find identifiers", ok_text="Done.")
    if any(a / b < 0.95 for _, a, b in low):
        st.warning("For some note types the app did not recognise the header block (the lines with the patient's "
                   "name and numbers). Run the header check and send its output to Alex: it shows only the shape "
                   "of the lines (digits as 9, letters as A/a), never their content.")
        st.button("Run the header check", key="g_shapes", disabled=busy() is not None,
                  on_click=start_job, args=("header_shapes", "Checking note headers",
                                            [jobs.nmosd("Header check", "header-shapes")]))
        show_job_result("header_shapes", "Header check", ok_text="Header check done (safe to share).")
    if not st.session_state.get("g_ident_text"):
        src = ident if ident.exists() else sugg if sugg.exists() else None
        if src is None:
            return
        st.session_state["g_ident_text"] = src.read_text(encoding="utf-8")
    st.caption("🔒 Patient identifiers: this list stays in your private folder on this computer.")
    with st.expander("How to edit this list"):
        st.markdown("- Each patient starts with their EMPI. Under it: `names`, `mrns`, `dob`, `relatives`, `address`, "
                    "`phones`, `emails`, `other_ids`.\n- Add an item as a new line starting with `- ` under the "
                    "right heading, lined up with the items above it. Empty lists look like `[]`: replace them, e.g. "
                    "`relatives: [Mary Smith, John Smith]`.\n- `staff` lists clinicians' names, also removed.")
    st.text_area("Identifiers", key="g_ident_text", height=460, label_visibility="collapsed")
    c1, c2 = st.columns(2)
    c1.button("Save and confirm", key="g_ident_save", type="primary", on_click=_save_identifiers)
    if sugg.exists():
        c2.button("Start again from the suggestions", key="g_ident_reset", on_click=_load_suggestions)
    if ident.exists():
        when = datetime.fromtimestamp(ident.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        if ws.newer(ident, P("staging_db")):
            st.success(f"Confirmed on {when}.")
        else:
            st.warning("The patients changed after this list was confirmed. Press 'Find names and numbers', then "
                       "'Start again from the suggestions' (or add the new patients), check, and save again.")


def _start_deid(new_key: bool) -> None:
    p1, p2 = st.session_state.get("g_pass1", ""), st.session_state.get("g_pass2", "")
    linkage = P("linkage")
    if new_key:
        if len(p1) < 12:
            flash("error", "The passphrase needs at least 12 characters.")
            return
        if p1 != p2:
            flash("error", "The two passphrases are not the same.")
            return
    else:
        from nmosd.deid.linkage import load_linkage

        try:
            load_linkage(linkage, p1)
        except Exception:
            flash("error", "That passphrase does not open the existing key. Try again.")
            return
    nn = no_ner_args()
    switch = ["--switch-mode"] if st.session_state.get("g_switch") else []
    if deid_on():
        steps = [jobs.nmosd("Remove identifiers", "deid", *nn, *switch),
                 jobs.nmosd("Leak test", "leak-test", *nn, allow_fail=True),
                 jobs.nmosd("Report", "deid-report")]
        start_job("deid", "Removing identifiers", steps, env={PASS_ENV: p1})
    else:
        start_job("deid", "Preparing the notes", [jobs.nmosd("Prepare notes", "deid", "--keep-identifiers", *switch)],
                  env={PASS_ENV: p1})
    st.session_state["g_pass1"] = st.session_state["g_pass2"] = ""


def switch_needed() -> bool:
    """The last run of step 5 stopped because some patients were prepared the other way before."""
    j = job("deid")
    return bool(j and j.state not in RUNNING and j.state != "done" and "--switch-mode" in jobs.tail(j))


def step_deid() -> None:
    keep = not deid_choice("g_deid_choice_5")
    if keep:
        _step_prepare()
        return
    ident = P("identifiers")
    if not needs(ident.exists(), "Confirm the names and numbers first (step 4)."):
        return
    st.write("Removes names, numbers, addresses and other identifiers from every note, and moves every date by a "
             "fixed number of days per patient. Then a **leak test** searches the result for anything on the "
             "confirmed list, and a **report** is made for the check in step 6.")
    st.caption("Time: about 3 hours for 400 notes on a fast laptop, longer on an older one (half removing the "
               "identifiers, half the leak test, which also marks the spots to check in the report). You can keep "
               "working, and you can close this browser tab.")
    if no_ner_args():
        st.warning("The de-identification models are switched off in the settings. Use this for test data only.")
    new_key = passphrase_inputs()
    switch_box()
    outside = outside_warning()
    st.button("Start removing identifiers", key="g_deid", type="primary", disabled=busy() is not None or outside,
              on_click=_start_deid, args=(new_key,))
    j = job("deid")
    if j and j.state not in RUNNING:
        text = jobs.tail(j)
        leak = leak_result(jobs.section(text, "Leak test"))
        if leak and leak[1]:
            st.success("✅ Leak test passed: nothing on the confirmed list is left in the de-identified notes.")
        elif leak:
            st.error(f"❌ Leak test failed: {leak[0]} known identifiers are still in the notes. Do not continue; "
                     "tell Alex the counts below (never the values).")
        show_job_result("deid", None, ok_text="De-identification finished.")
    elif j is None and P("deid_db").exists():
        st.info("Notes were de-identified earlier (outside the app). Run it again here to see the leak test result.")


def switch_box() -> None:
    if switch_needed():
        st.warning("Some of these patients were prepared the other way last time (de-identified, or kept as they "
                   "were). Their earlier extraction, review decisions and REDCap files used the other dates. If you "
                   "need the final file from that work, make it in step 9 first.")
        st.checkbox("Switch them: their extraction runs again and their dates need reviewing again", key="g_switch")


def passphrase_inputs() -> bool:
    new_key = not P("linkage").exists()
    if new_key:
        st.markdown("Choose a **passphrase** of at least 12 characters and keep it safe, e.g. in your password "
                    "manager. It locks the key that links the study IDs back to the patients. Without it the results "
                    "can't be matched back to patients, and it can't be recovered.")
        st.text_input("New passphrase", type="password", key="g_pass1")
        st.text_input("The same passphrase again", type="password", key="g_pass2")
    else:
        st.text_input("Passphrase (the one chosen the first time)", type="password", key="g_pass1")
    return new_key


def _step_prepare() -> None:
    """Step 5 without de-identification: the notes go into the working database as they are, under study IDs."""
    if not needs(P("staging_db").exists(), "Copy the patients' notes first (step 3)."):
        return
    st.write("Copies the notes **as they are** (names, numbers and real dates) into the working database, giving "
             "each patient a study ID. Nothing is removed, so there is no leak test and no check in step 6. It "
             "takes a few minutes.")
    st.caption("🔒 The notes stay in your private folder on this computer, and the local AI reads them there.")
    new_key = passphrase_inputs()
    switch_box()
    outside = outside_warning()
    st.button("Prepare the notes", key="g_prepare", type="primary", disabled=busy() is not None or outside,
              on_click=_start_deid, args=(new_key,))
    j = job("deid")
    if j and j.state not in RUNNING:
        show_job_result("deid", None, ok_text="Notes prepared.")
    elif j is None and P("deid_db").exists() and notes_deidentified(read_manifest()):
        st.info("The notes in the working database were de-identified earlier. Prepare them again here to use "
                "the original notes.")


def _add_signoff() -> None:
    up = st.session_state.get("g_signoff_up")
    if up is None:
        flash("error", "Choose the sign-off file first.")
        return
    from nmosd.deid.review_report import import_signoff

    dest = ensure_phi_output(P("deid_report").with_name("deid_signoff.json"), "the sign-off file")
    dest.write_bytes(up.getvalue())
    buf = io.StringIO()
    try:
        import_signoff(P("deid_db"), dest, stdout=buf)
        flash("success", buf.getvalue().strip())
    except Exception as e:
        flash("error", f"That file could not be read as a sign-off ({type(e).__name__}).")


def _start_report() -> None:
    passphrase = st.session_state.get("g_pass_report", "")
    from nmosd.deid.linkage import load_linkage

    try:
        load_linkage(P("linkage"), passphrase)
    except Exception:
        flash("error", "That passphrase does not open the key. Try again.")
        return
    start_job("deid_report", "Making the de-identification report", [jobs.nmosd("Report", "deid-report")],
              env={PASS_ENV: passphrase})
    st.session_state["g_pass_report"] = ""


def _start_marks() -> None:
    passphrase = st.session_state.get("g_pass_marks", "")
    from nmosd.deid.linkage import load_linkage

    try:
        load_linkage(P("linkage"), passphrase)
    except Exception:
        flash("error", "That passphrase does not open the key. Try again.")
        return
    nn = no_ner_args()
    start_job("leak_marks", "Marking the spots to check", [jobs.nmosd("Leak test", "leak-test", *nn, allow_fail=True),
                                                            jobs.nmosd("Report", "deid-report")],
              env={PASS_ENV: passphrase})
    st.session_state["g_pass_marks"] = ""


def marks_missing(report: Path) -> bool:
    """True if the report cannot mark the leak test's spots yet (no positions saved, or saved before the last
    de-identification run in the app)."""
    cand = report.with_name("leak_candidates.json")
    if not cand.exists():
        return True
    dj = job("deid")
    if dj and dj.started:     # the leak test runs inside that job, after the de-identification
        return datetime.fromtimestamp(cand.stat().st_mtime) < datetime.fromisoformat(dj.started)
    return False


def step_signoff() -> None:
    if not deid_on():
        st.info("Not needed: you chose to keep the notes as they are, so there is nothing to check.")
        return
    report = P("deid_report")
    if not report.exists():
        if not needs(P("deid_db").exists(), "Remove the identifiers first (step 5): it makes the report."):
            return
        st.write("The identifiers were removed, but the report for this check has not been made yet (for example "
                 "because step 5 was run outside the app). Enter the passphrase to make it: it takes a few minutes.")
        st.text_input("Passphrase", type="password", key="g_pass_report")
        st.button("Make the report", key="g_make_report", type="primary",
                  disabled=busy() is not None or outside_warning(), on_click=_start_report)
        show_job_result("deid_report", "Report", ok_text="Report made.")
        return
    st.write("Before any result leaves this computer, a person checks every de-identified note. The report shows "
             "each note before and after, with every change highlighted.")
    st.markdown("1. **Open the report** and read every note. Look for anything that could identify the patient.\n"
                "2. Tick **reviewed — no residual PHI** for each note. If something was missed, describe where it "
                "is (not what it says).\n"
                "3. At the end, click **Download sign-off JSON** and add that file below.")
    if marks_missing(report):
        st.warning("This report does not mark the spots the leak test flagged yet (possible names, phone numbers "
                   "and other identifiers that the de-identification may have missed). To mark them, run the leak "
                   "test again here. It takes about as long as removing the identifiers (1–2 hours for 400 notes) and "
                   "can't run at the same time as the extraction.")
        st.text_input("Passphrase", type="password", key="g_pass_marks")
        st.button("Mark the spots to check", key="g_marks", disabled=busy() is not None or outside_warning(),
                  on_click=_start_marks)
    show_job_result("leak_marks", "Report", ok_text="The report now marks the spots to check.")
    st.markdown("In the report, the spots to check are listed at the top and marked in red. **Show only notes with "
                "spots to check** hides the others.")
    if st.button("Open the report", key="g_open_report"):
        open_path(report)
    st.caption(f"The report is in your private folder: `{report}`")
    st.info("You can start the extraction (step 7) now and do this check while it runs. Nothing is exported until "
            "the check is complete.")
    st.file_uploader("Sign-off file (deid_signoff.json)", type=["json"], key="g_signoff_up")
    st.button("Add the sign-off", key="g_signoff_add", on_click=_add_signoff)
    m = read_manifest()
    if m.get("signoff_date"):
        msg = (f"Signed off by {m.get('signoff_reviewer') or '—'}: {m.get('signoff_reviewed_docs')}/"
               f"{m.get('signoff_total_docs')} notes reviewed, {m.get('signoff_docs_with_misses')} with misses.")
        (st.success if m.get("signoff_complete") else st.warning)(msg)


def step_extract() -> None:
    deid = P("deid_db")
    if not needs(deid.exists(), "Remove the identifiers first (step 5)."):
        return
    if notes_deidentified(read_manifest()) != deid_on():
        st.warning("The notes in the working database were prepared the other way (de-identified or kept as they "
                   "are) than you have now chosen in step 5. Run step 5 again first.")
        return
    dj = job("deid")
    if deid_on() and dj and dj.state not in RUNNING and dj.step_rc("Leak test") not in (0, None):
        st.error("The leak test failed (step 5). Fix that before running the extraction.")
        return
    st.write(f"The local AI model reads the {'de-identified ' if deid_on() else ''}notes **on this computer** and "
             "proposes a value for each REDCap field, with the sentence it took it from. You check every proposal "
             "in step 8.")
    st.caption("This takes hours. You can keep using the computer (it may feel a little slower), or start it at the "
               "end of the day; keep the computer plugged in. You can stop and continue later: finished notes are "
               "kept.")
    model_args: list[str] = []
    if S.get("llm.backend", "ollama") == "synthetic-oracle":
        st.warning("Test mode: a built-in stand-in answers instead of a model (synthetic data only).")
    else:
        info = checks.ollama_info(S)
        if not info.reachable:
            st.error("The local AI (Ollama) is not running. Open the Ollama app, then press 'Check again'.")
            st.button("Check again", key="g_ollama_again")
            return
        usable = checks.usable_models(S, info.models)
        if not usable:
            st.error("No allowed model is downloaded yet: see step 1.")
            return
        default = S.get("llm.model", "qwen3:8b")
        model = st.selectbox("Model", usable, index=usable.index(default) if default in usable else 0, key="g_model")
        model_args = ["--model", model]
    running = busy() is not None or outside_warning()
    ej = job("extract")
    started = ej is not None or P("extract_db").exists()
    c1, c2 = st.columns(2)
    c1.button("Continue" if started else "Start", key="g_extract", type="primary", disabled=running,
              on_click=start_job, args=("extract", "Extracting with the local AI",
                                        [jobs.nmosd("Extract", "extract", *model_args),
                                         jobs.nmosd("Combine", "reconcile"),
                                         # the disability (EDSS) groups read the examination notes at each treatment
                                         # start and stop and the last follow-up, which the first Combine finds
                                         jobs.nmosd("Disability", "extract", "--groups", ",".join(second_pass_groups()),
                                                    *model_args),
                                         jobs.nmosd("Combine again", "reconcile")]))
    c2.button("Preview the amount of work", key="g_plan", disabled=running,
              on_click=start_job, args=("extract_plan", "Planning the extraction",
                                        [jobs.nmosd("Plan", "extract", "--dry-run", *model_args)]))
    if ej and ej.state not in RUNNING:
        if ej.state == "done":
            st.success("Extraction finished. Go to step 8 to review.")
        else:
            st.warning("The extraction stopped before the end. Press **Continue**: finished notes are kept.")
        text = jobs.tail(ej)
        for label in ("Extract", "Disability"):
            done = re.search(r"^Done: .*$", jobs.section(text, label), re.M)
            if done:
                st.caption(f"{label}: {done.group(0)}")
        with st.expander("Details"):
            st.code(text[-6000:], language=None)
    pj = job("extract_plan")
    if pj and pj.state not in RUNNING:
        with st.expander("The amount of work", expanded=ej is None):
            st.markdown(jobs.section(jobs.tail(pj), "Plan"))


def step_review() -> None:
    ex = P("extract_db")
    if not needs(ex.exists(), "Run the extraction first (step 7)."):
        return
    st.write(f"For each patient: first the **diagnosis check** ({CONDITION.name} confirmed, enough follow-up), "
             "then each REDCap section. Accept or correct each proposed value; the sentence it came from is shown "
             "next to it. Every decision is saved straight away.")
    st.page_link(REVIEW_PAGE, label="Open the review screens", icon="📝")
    st.caption("Step 10 exports how accurate the tool was, from these decisions (also part-way through the review).")


# step 10: the tool's accuracy, from the review decisions (`nmosd accuracy`, 18.11.26)
def accuracy_dir() -> Path:
    return P("deid_report").parent / "accuracy"


def accuracy_snapshots() -> list[Path]:
    from nmosd.eval.accuracy import snapshots

    return snapshots(accuracy_dir())


def _start_accuracy() -> None:
    args = ["accuracy", "--out", str(accuracy_dir())]
    label = (st.session_state.get("g_acc_label") or "").strip()
    if label:
        args += ["--label", label]
    start_job("accuracy", "Exporting the tool accuracy", [jobs.nmosd("Accuracy", *args)])


def step_accuracy() -> None:
    rv = S.path("review_db")
    if not needs(P("extract_db").exists() and rv is not None and rv.exists(),
                 "Review some patients first (step 8): the accuracy comes from your review decisions."):
        return
    st.write("How accurate the tool was, judged by your decisions in step 8, for a paper or a report. For every "
             "field: was the value it proposed **kept**, **corrected**, **filled when it should have been empty**, "
             "or **missed**? Also: did it find the right attacks, admissions, MRIs and treatment courses, how far "
             "off were its dates, and the diagnosis check. Accuracy, precision and recall are given overall, per "
             "REDCap section and per field. You can export part-way through the review: fields not reviewed yet are "
             "counted separately.")
    st.caption("The reviewer sees each proposal before deciding, which can make agreement look higher. Step 9's "
               "**Compare with the values already in REDCap** measures against entries made without the tool.")
    if "g_acc_label" not in st.session_state:
        st.session_state["g_acc_label"] = S.get("llm.model", "") or ""
    st.text_input("Name for this export (optional, e.g. the AI model)", key="g_acc_label")
    st.button("Export the accuracy table", key="g_accuracy", type="primary", disabled=busy() is not None,
              on_click=_start_accuracy)
    show_job_result("accuracy", "Accuracy", ok_text="Accuracy table exported.", markdown=True)
    snaps = accuracy_snapshots()
    if not snaps:
        return
    from nmosd.eval.accuracy import shareable_zip

    last = snaps[-1]
    st.subheader("Latest export")
    st.caption(f"`{last}`" + (f" · {len(snaps) - 1} earlier export(s) kept next to it" if len(snaps) > 1 else ""))
    c1, c2 = st.columns(2)
    c1.download_button("Download the accuracy tables (.zip)", shareable_zip(last), file_name=f"{last.name}.zip",
                       mime="application/zip", key="g_acc_zip",
                       help="The report and the CSV tables: counts and percentages only, safe to share.")
    if c2.button("Open the folder", key="g_open_accuracy"):
        open_path(last)
    st.caption("The zip holds counts only. `decisions_with_study_ids.csv` (one row per decision, for a per-patient "
               "analysis) stays in the folder: it has study IDs, so keep it private.")


def step_export() -> None:
    if not needs(P("extract_db").exists(), "Run the extraction (step 7) and review the results (step 8) first."):
        return
    m = read_manifest()
    test = False
    if notes_deidentified(m) and not m.get("signoff_complete"):
        st.warning("The de-identification check (step 6) is not complete. Results go into REDCap only after it.")
        test = st.checkbox("This is a test: I will not import the file into REDCap", key="g_export_test")
        if not test:
            return
    st.write("Makes the file to import into REDCap, from the values you accepted or corrected in step 8. Patients "
             "excluded in the diagnosis check are left out.")
    args = ["export"] if S.path("redcap_template") else ["export", "--no-template"]
    st.button("Make the REDCap file", key="g_export", type="primary", disabled=busy() is not None,
              on_click=start_job, args=("export", "Making the REDCap file", [jobs.nmosd("Export", *args)]))
    show_job_result("export", "Export", ok_text="File made.")
    out = P("export_dir") / "redcap_import_deidentified.csv"
    if out.exists():
        st.caption(f"{'De-identified file' if notes_deidentified(m) else 'File with study IDs'}: `{out}`")
        if st.button("Open the folder", key="g_open_export"):
            open_path(out.parent)
    st.divider()
    st.subheader("Match to your REDCap records")
    st.write("Two things below need a **record map**: a CSV file with two columns, `empi` and `record_id`, saying "
             "which REDCap record each patient is. Make it yourself and keep it in your private folder. They also "
             "need the passphrase.")
    default_map = P("phi_dir") / "record_map.csv"
    path_input("Record map (CSV with columns empi, record_id)", "g_rmap",
               default_map if default_map.exists() else None, "file")
    st.text_input("Passphrase", type="password", key="g_pass_reid")
    gold = S.path("redcap_template")
    running = busy() is not None
    c1, c2 = st.columns(2)
    c1.button("Make the final file (real record IDs and dates)", key="g_reid", disabled=running or not out.exists(),
              on_click=_start_matched, args=("reidentify", out))
    c2.button("Compare with the values already in REDCap", key="g_eval",
              disabled=running or not out.exists() or not (gold and gold.exists()),
              on_click=_start_matched, args=("evaluate", out),
              help="Uses your REDCap project's data export (step 2) as the reference. Prints counts and "
                   "percentages only.")
    if not (gold and gold.exists()):
        st.caption("To compare, add your REDCap project's data export in step 2.")
    show_job_result("reidentify", "Re-identify", ok_text="Final file made (in the identifiable `work` folder).")
    final = P("reidentified_dir") / "redcap_import_final.csv"
    if final.exists() and st.button("Open the folder with the final file", key="g_open_final"):
        open_path(final.parent)
    show_job_result("evaluate", "Compare", ok_text="Comparison done. These are counts and percentages only: safe to "
                                                   "share.", markdown=True)


def _start_matched(kind: str, export_csv: Path) -> None:
    """Callback for 'Make the final file' and 'Compare': both need the record map and the passphrase."""
    passphrase = st.session_state.get("g_pass_reid", "")
    rmap = clean_path(st.session_state.get("g_rmap", ""))
    if not rmap or not Path(rmap).exists():
        flash("error", "Choose the record map file (CSV with columns empi, record_id).")
        return
    from nmosd.deid.linkage import load_linkage

    try:
        load_linkage(P("linkage"), passphrase)
    except Exception:
        flash("error", "That passphrase does not open the key. Try again.")
        return
    if kind == "reidentify":
        steps = [jobs.nmosd("Re-identify", "reidentify", "--export", str(export_csv), "--record-map", rmap)]
        title = "Putting back record IDs and dates"
    else:
        label = st.session_state.get("g_model") or S.get("llm.model", "")
        report = P("deid_report").with_name("evaluation.md")
        steps = [jobs.nmosd("Compare", "evaluate", "--gold", str(S.path("redcap_template")), "--predicted",
                            str(export_csv), "--linkage", str(P("linkage")), "--record-map", rmap,
                            "--report", str(report), "--label", label)]
        title = "Comparing with the values in REDCap"
    start_job(kind, title, steps, env={PASS_ENV: passphrase})
    st.session_state["g_pass_reid"] = ""


STEP_FUNCS = {"check": step_check, "files": step_files, "patients": step_patients, "identifiers": step_identifiers,
              "deid": step_deid, "signoff": step_signoff, "extract": step_extract, "review": step_review,
              "export": step_export, "accuracy": step_accuracy}


# ============================================================================================== start screen
PER_ROW = 4


def _choose(cid: str) -> None:
    st.session_state["condition"] = cid


def _change_condition() -> None:
    """Back to the start screen. Everything typed for the condition (paths, patient lists, identifiers, passphrases)
    is cleared from this window, so nothing carries over to the next condition."""
    for key in list(st.session_state.keys()):
        del st.session_state[key]


def condition_switch() -> None:
    st.markdown(f"**{CONDITION.name}**")
    st.button("Change condition", key="change_condition", on_click=_change_condition,
              help="Back to the list of conditions. Work running in the background carries on.")


def start_page() -> None:
    if CONDITION is not None:          # chosen, but not set up yet
        not_set_up_page(CONDITION)
        return
    st.title(APP_NAME)
    st.subheader("Which condition are you working on?")
    st.caption("Each condition has its own REDCap project, its own rules for reading the notes, and its own private "
               "data folder.")
    for group, items in cnd.by_group(CONDITIONS).items():
        st.markdown(f"##### {group}")
        for row in range(0, len(items), PER_ROW):
            cols = st.columns(PER_ROW)
            for col, c in zip(cols, items[row:row + PER_ROW], strict=False):
                with col.container(border=True):
                    st.markdown(f"**{c.name}**")
                    st.caption(c.full_name or " ")
                    st.markdown("✅ Ready" if c.ready else "🕓 Not set up yet")
                    st.button("Open" if c.ready else "What's needed", key=f"cond_{c.id}",
                              type="primary" if c.ready else "secondary", on_click=_choose, args=(c.id,))
    st.divider()
    st.caption("🔒 Everything runs on this computer. Nothing is sent anywhere.")
    st.caption("Condition not listed? The list is `config/conditions.yaml` in the app folder.")


def not_set_up_page(c: cnd.Condition) -> None:
    st.title(c.name)
    if c.full_name:
        st.caption(c.full_name)
    st.info(f"{c.name} is not set up in this app yet. The app reads a condition's REDCap fields from the notes once "
            "its rules have been built and checked, as was done for NMOSD.")
    st.markdown("**What is needed to set it up**\n\n"
                "1. The REDCap project's **data dictionary**: in REDCap, *Project Setup → Data Dictionary → Download "
                "the current Data Dictionary* (or the printed codebook). It holds no patient data.\n"
                "2. For each field: is it read from the **notes**, or from **structured data** such as the "
                "demographics table? A clinician signs this list off.\n"
                "3. The **diagnosis check**: what confirms the diagnosis, and how much follow-up a patient needs to "
                "be included.\n"
                "4. A few **pilot patients** to tune and test it on (NMOSD used 5).")
    st.button("← Choose another condition", key="cond_back", type="primary", on_click=_change_condition)


# ============================================================================================== pages
def guide_page() -> None:
    state = states()
    keys = [k for k, _ in STEPS]
    if "g_step" not in st.session_state:
        st.session_state["g_step"] = next((k for k in keys if state[k] != "done"), "review")
    titles = dict(STEPS)
    if not deid_on():
        titles |= {"identifiers": titles["identifiers"] + " (not needed)", "deid": "Prepare the notes",
                   "signoff": titles["signoff"] + " (not needed)"}
    with st.sidebar:
        condition_switch()
        st.radio("Steps", keys, key="g_step",
                 format_func=lambda k: f"{ICONS[state[k]]} {keys.index(k) + 1}. {titles[k]}")
        st.caption("🔒 Everything runs on this computer. Nothing is sent anywhere.")
    k = st.session_state["g_step"]
    st.title(CONDITION.name)
    st.caption(f"{CONDITION.full_name} · {APP_NAME}")
    running_panel()
    st.header(f"{keys.index(k) + 1} · {titles[k]}")
    show_flash()
    STEP_FUNCS[k]()


def review_page() -> None:
    with st.sidebar:
        condition_switch()
    ex, de, rv = S.path("extract_db"), S.path("deid_db"), S.path("review_db")
    if not (CONFIGURED and ex and ex.exists() and de and de.exists() and rv):
        st.title("Review")
        st.info("Nothing to review yet: finish steps 1–7 of the guide first.")
        return
    from nmosd.extract.run import resolve_dictionary
    from nmosd.review.app import render

    render(ex, de, ensure_phi_output(rv, "the review database"), resolve_dictionary(None, io.StringIO()))


st.set_page_config(page_title=APP_NAME, page_icon=str(ICON) if ICON.exists() else "🧠", layout="wide")
if OPEN:
    GUIDE_PAGE = st.Page(guide_page, title="Guide", icon="🧭", default=True)
    REVIEW_PAGE = st.Page(review_page, title="Review", icon="📝", url_path="review")
    st.navigation([GUIDE_PAGE, REVIEW_PAGE]).run()
else:
    st.navigation([st.Page(start_page, title="Choose a condition", icon="🧠", default=True)], position="hidden").run()
