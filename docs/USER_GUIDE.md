# Neurology REDCap Extractor: user guide

Neurology REDCap Extractor helps fill in a condition's REDCap project from RPDR clinical notes. **NMOSD** (the
*NMOSD database* project) is fully set up; other neurological conditions are listed in the app and are added one
by one. It:

1. removes patient identifiers from the notes (names, record numbers, addresses, phone numbers) and shifts every
   date by a fixed number of days per patient;
2. has a **local AI model** read the de-identified notes and propose a value for each REDCap field, with the
   sentence it took the value from;
3. lets you **accept or correct every proposed value** before anything is exported.

**Everything runs on your computer.** The AI model runs on the laptop itself, and the app opens in its own
window and is only reachable from this computer. Nothing is sent over the internet after setup.

---

## What you need

- A Windows 10 or 11 laptop (or a Mac) with **16 GB of memory** and about **15 GB of free disk space**.
- The **RPDR export** for your patients: an Access file ending in `.accdb`.
- Internet access for the one-time setup only.
- The 64-bit **Microsoft Access driver** (often already installed with Office; the installer checks). Your IT team
  may need to install it.

## Setup (once)

1. Right-click the zip file, choose **Properties**, tick **Unblock** if you see it, and click OK.
2. Unzip it into your Documents folder, e.g. `Documents\Neurology REDCap Extractor`.
3. Double-click **Install Neurology REDCap Extractor**. If Windows says *"Windows protected your PC"*, click **More info**,
   then **Run anyway**. Setup installs, for your user account only:
   - the app's Python packages;
   - the de-identification models (about 1 GB);
   - **Ollama**, the program that runs the AI model on this computer, and the **qwen3:8b** model (about 5 GB).
   It then puts a **Neurology REDCap Extractor** icon on the desktop (replacing an older *NMOSD Extractor* icon).
   It takes 30–60 minutes and can be run again safely.
4. Anything shown in **yellow** at the end still needs sorting out: see [If something goes wrong](#if-something-goes-wrong).

## Using the app

Double-click **Neurology REDCap Extractor** on the desktop (on a Mac: the *Neurology REDCap Extractor* app). The
app opens in its own window.

**First, choose the condition you are working on.** Conditions marked *Ready* open the steps below; the others are
not set up yet and show what is needed to set them up. Each condition keeps its own files (its own private folder,
chosen in step 2), so work on two conditions never mixes. **Change condition**, at the top left, goes back to the
list.

The steps are listed on the left, with a tick when each one is done. Closing the window closes the app; work
running in the background carries on (see *Long steps*).

| Step | What you do | How long |
|---|---|---|
| 1. Check this computer | Make sure everything shows a green tick. Buttons download anything missing. | – |
| 2. Choose your files | Pick the RPDR export (`.accdb`) and a **private folder** for the working files. Optionally your REDCap project's data export (CSV), so the final file has the same columns. | 1 min |
| 3. Choose patients | Paste the patients' EMPIs, one per line (or choose every patient in the export). Press **Copy these patients' notes**. | 1 min |
| 4. Confirm names and numbers | Press **Find names and numbers**. Check the list, add nicknames, relatives, phone numbers, email and street addresses, then **Save and confirm**. | 10–20 min |
| 5. Remove identifiers | Choose a passphrase (first time) and press **Start**. The leak test must say *passed*; it also marks the spots to check in the report. | about 3 h per 400 notes (longer on an older laptop) |
| 6. Check the de-identification | Open the report, read every note (spots the leak test flagged are marked in red; **Show only notes with spots to check** lists them), tick each one, download the sign-off file and add it here. | as needed |
| 7. Extract with the local AI | Press **Preview the amount of work**, then **Start**. It reads the notes for every field of the REDCap codebook (1–303), then reads the examination notes again for the disability (EDSS) entries. Let it run overnight. | many hours |
| 8. Review the results | For each patient: the diagnosis check first, then accept or correct each value. Each field shows its number and label from the REDCap codebook (e.g. "45. Attack Date"); questions REDCap would skip are hidden, and a list at the top of each instrument names the fields REDCap or you fill in. **Search this patient's notes** finds words even when a line break or other words separate them. | as needed |
| 9. Export for REDCap | Make the import file; optionally put back the real record IDs and dates. | 1 min |
| 10. Export tool accuracy | Press **Export the accuracy table**: how often the tool's values were kept, corrected, filled when they should have been empty, or missed, from your review decisions (for a paper or report). **Download the accuracy tables (.zip)** gives the report and CSV tables (counts only, safe to share). Each export is kept in its own dated folder; export again after more review. | 1 min |

### Long steps

Steps 5 and 7 run **in the background**. A progress bar shows how far they have got and how long is left. You
can keep using the computer, and you can close the app window: the work carries on. Open the app again (and
choose the same condition) to check on it. The computer is kept awake while the work runs (the screen may still turn off); **keep it plugged in** and
don't close the lid.

To pause the extraction, press **Stop**. Press **Continue** later: finished notes are kept.

### The passphrase

The first time you remove identifiers, you choose a passphrase (at least 12 characters). It locks the key that
links the results back to the patients. **Keep it in your password manager.** Without it, the results can't be
matched back to patients, and nobody can recover it.

## Where your files are

Everything goes into the private folder you chose in step 2:

| Folder | Contains |
|---|---|
| `work` | Identifiable: the patient list, the confirmed identifiers, a copy of the patients' notes, the encrypted link key, the de-identification report, the final REDCap file with real record IDs. |
| `deid` | De-identified: the notes after de-identification, the AI's proposals, your review decisions, the de-identified REDCap file. |

Treat the whole private folder as patient data: don't email it, copy it to a USB stick or put it in a shared folder.
The app never writes patient data into its own folder, so the app folder can be shared or zipped safely.

## If something goes wrong

| What you see | What to do |
|---|---|
| "Windows protected your PC" | Click **More info**, then **Run anyway**. |
| Setup: Python was not found | Install Python 3.12 from python.org ("Windows installer (64-bit)"), tick *Add python.exe to PATH*, then run setup again. |
| Setup: installing the packages failed | Check the internet connection. On the hospital network, pypi.org may be blocked: try another network, or ask IT. |
| Setup: torch does not load | Ask IT to install the *Microsoft Visual C++ Redistributable (x64)*, or to allow the app folder's `.venv` in the security software. |
| Setup: the Microsoft Access driver is not installed | Install the *Microsoft Access Database Engine 2016 Redistributable* (64-bit), or ask IT. |
| Step 1: Ollama is not running | Open **Ollama** from the Start menu, then press **Check again**. |
| The icon does nothing, or the window stays blank | A message box usually says why. If not, see the log in `%LOCALAPPDATA%\Neurology REDCap Extractor\logs` (Mac: `~/Library/Logs/Neurology REDCap Extractor`). |
| The app opens in a browser tab instead of its own window | The window package did not install: run **Install Neurology REDCap Extractor** again (Mac: `.venv/bin/pip install -e ".[window]"`). The app works the same in the browser. |
| "Port 8501 is in use by another program" | Close the other program, or restart the computer. |
| The leak test failed | Don't continue. Tell Alex the counts (never the names or numbers). |
| The extraction stopped | Press **Continue** in step 7. If Ollama was closed, open it first. |
| A step shows "This did not finish" | Open **Details** and send Alex the last few lines, after checking that they contain no names, numbers or note text. |

Questions: Alex. When you report a problem, describe it or send the message from the black window or the
**Details** box. **Never send note text, names, record numbers, the identifiers list, the report or any file
from the private folder.**
