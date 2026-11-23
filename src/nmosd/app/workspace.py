"""The settings file and the standard folder layout the guided app sets up.

The user chooses two things: the RPDR export file and one private data folder. Everything the pipeline writes goes
into fixed places below that folder:

    <data folder>/work/   identifiable: patient list, identifiers, staging copy, linkage key, reports
    <data folder>/deid/   notes (de-identified unless `deid.enabled: false`), extraction, review decisions,
                          the REDCap file (study IDs)

Settings the user did not set in the app (e.g. hand-edited ones) are kept when the app saves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from nmosd.config import CONFIG_DIR, REPO_ROOT, Settings, load_settings
from nmosd.safety import SafetyError, ensure_outside_repo

DATE_FORMATS = ["M/D/YYYY", "MM/DD/YYYY", "YYYY-MM-DD", "D/M/YYYY", "DD/MM/YYYY"]
SOURCE_SUFFIXES = {".accdb", ".mdb", ".sqlite"}
SYNCED_FOLDER_HINTS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "box sync", "mobile documents")

LAYOUT = {
    "phi_dir": "work",
    "empi_list": "work/patients.txt",
    "identifiers": "work/identifiers.yaml",
    "staging_db": "work/staging/staging.sqlite",
    "pilot_selection_db": "work/staging/pilot_selection.sqlite",
    "linkage": "work/linkage/linkage.sqlite.enc",
    "deid_report": "work/reports/deid_report.html",
    "reidentified_dir": "work/reidentified",
    "deid_db": "deid/deid.sqlite",
    "extract_db": "deid/extract.sqlite",
    "review_db": "deid/review.sqlite",
    "export_dir": "deid/export",
}

HEADER = ("# Neurology REDCap Extractor: one condition's settings on this computer (written by the guided app; safe to "
          "edit by hand).\n"
          "# Paths only: no patient data. Every patient-data path must be outside the app folder.\n")


def read_raw(path: Path) -> dict:
    """The settings file as written (not merged with the defaults)."""
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def defaults() -> Settings:
    """The example settings, with no source file (nothing chosen yet)."""
    s = load_settings(CONFIG_DIR / "settings.example.yaml")
    s.source = None
    return s


def load(path: Path) -> Settings:
    """The settings at `path`; the defaults (with no source file) if it does not exist yet."""
    return load_settings(path) if path.exists() else defaults()


def is_configured(settings: Settings) -> bool:
    src, phi = settings.path("source"), settings.path("phi_dir")
    return bool(settings.source and src and phi and src.exists())


def data_folder(settings: Settings) -> Path | None:
    """The private data folder: the parent of `work/` in the standard layout."""
    phi = settings.path("phi_dir")
    if phi is None:
        return None
    return phi.parent if phi.name == "work" else phi


def layout(data_dir: Path, existing_paths: dict | None = None) -> dict[str, str]:
    """Every pipeline path below `data_dir`. Paths already set inside `data_dir` are kept as they are."""
    data_dir = data_dir.expanduser()
    out = {}
    for key, rel in LAYOUT.items():
        old = (existing_paths or {}).get(key)
        if old and _within(Path(str(old)).expanduser(), data_dir):
            out[key] = str(old)
        else:
            out[key] = str(data_dir / rel)
    return out


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def problems(source: Path | None, data_dir: Path | None, template: Path | None = None) -> list[str]:
    """Reasons the chosen files cannot be used (empty when they are fine)."""
    out = []
    if not source or not str(source).strip():
        out.append("Choose the RPDR export file (.accdb).")
    else:
        if not source.exists():
            out.append("The RPDR export file was not found at that path.")
        elif source.suffix.lower() not in SOURCE_SUFFIXES:
            out.append("The RPDR export should be an Access file (.accdb or .mdb).")
        try:
            ensure_outside_repo(source, "the RPDR export")
        except SafetyError:
            out.append("The RPDR export is inside the app folder. Move it to your private data folder.")
    if not data_dir or not str(data_dir).strip():
        out.append("Choose a private data folder.")
    else:
        try:
            ensure_outside_repo(data_dir, "the private data folder")
        except SafetyError:
            out.append("The private data folder must be outside the app folder.")
        if data_dir.exists() and not data_dir.is_dir():
            out.append("The private data folder path is a file, not a folder.")
    if template and str(template).strip() and not template.exists():
        out.append("The REDCap export (CSV) was not found at that path. Leave it empty if you don't have one.")
    return out


def synced_folder_warning(data_dir: Path) -> str | None:
    low = str(data_dir).lower()
    if any(h in low for h in SYNCED_FOLDER_HINTS):
        return ("This folder looks like it is synced to a cloud service. Patient data will be copied there: check "
                "that your institution allows that, or choose a folder that is not synced.")
    return None


def build_settings(raw: dict, source: Path, data_dir: Path, template: Path | None, date_format: str) -> dict:
    data = dict(raw or {})
    paths = dict(data.get("paths") or {})
    paths.update(layout(data_dir, paths))
    paths["source"] = str(source.expanduser())
    paths["redcap_template"] = str(template.expanduser()) if template and str(template).strip() else None
    data["paths"] = paths
    data["export"] = {**(data.get("export") or {}), "date_format": date_format}
    return data


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


# ------------------------------------------------------------------------------------------ the patient list
EMPI_RX = re.compile(r"^\d{3,20}$")


@dataclass
class PatientList:
    empis: list[str]
    rejected: int            # lines that are not a plain number


def parse_empis(text: str) -> PatientList:
    seen, out, bad = set(), [], 0
    for line in text.replace(",", "\n").replace(";", "\n").splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        if not EMPI_RX.match(t):
            bad += 1
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return PatientList(out, bad)


def read_empis(path: Path | None) -> list[str]:
    if not path or not path.exists():
        return []
    return parse_empis(path.read_text(encoding="utf-8")).empis


def write_empis(path: Path, empis: list[str]) -> None:
    from nmosd.safety import ensure_phi_output

    ensure_phi_output(path, "the patient list")
    path.write_text("\n".join(empis) + "\n", encoding="utf-8")


def newer(a: Path | None, b: Path | None) -> bool:
    """True if file `a` exists and is at least as new as `b` (or `b` does not exist)."""
    if not a or not a.exists():
        return False
    if not b or not b.exists():
        return True
    return a.stat().st_mtime >= b.stat().st_mtime - 1


def repo_root() -> Path:
    return REPO_ROOT
