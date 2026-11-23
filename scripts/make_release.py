#!/usr/bin/env python3
"""Build the zip to send to another computer (e.g. the Windows laptop): the app only, never data or settings.

    python scripts/make_release.py            # -> dist/Neurology_REDCap_Extractor_<date>.zip

Included: the code, the committed rule files, the scripts, the icon, the user docs and the tests (synthetic only).
Left out: the virtual environment, caches, this computer's settings files (one per condition), identifiers files,
quality-review packets (`nmosd quality-packet` output, if one was put under docs/), the Mac app (it is made on each
Mac) and developer notes. The build stops if any file that could hold data (databases, Access files, encrypted keys,
CSVs other than the REDCap data dictionaries, quality-packet patient files) would end up in the zip. Patient data never lives in the app folder anyway: the
pipeline refuses to write it there.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOP = "Neurology REDCap Extractor"
INCLUDE = ["src", "config", "scripts", "assets", "docs", "tests", "pyproject.toml", "README.md", "START HERE.txt",
           "Install Neurology REDCap Extractor.bat", "Start Neurology REDCap Extractor.bat",
           "Start Neurology REDCap Extractor.command"]
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", ".app_state", ".git", "dist", ".claude",
             "NMOSD quality review", "Claude_reference"}
SKIP_FILES = {".DS_Store"}
DATA_SUFFIXES = {".sqlite", ".db", ".accdb", ".mdb", ".enc", ".sqlite-wal", ".sqlite-shm", ".parquet", ".xlsx", ".xls"}
ALLOWED_CSV_DIRS = ("config/redcap/", "tests/fixtures/redcap/")
# quality-packet patient files (Patient_A.md, Patient_A_notes_01.md, Patient_A.json, ...): never shared this way
PACKET_FILE = re.compile(r"^Patient_[A-Z]+(_|\.)")


def collect(repo: Path = REPO) -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE:
        p = repo / name
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                rel = f.relative_to(repo)
                # decided on the name alone: this computer's settings are never even opened
                if _skipped(rel) or f.is_dir():
                    continue
                files.append(f)
    return files


def _skipped(rel: Path) -> bool:
    in_config = rel.parent.as_posix() == "config"
    return (any(part in SKIP_DIRS or part.startswith("packet_") or part.endswith(".app") for part in rel.parts)
            or rel.name in SKIP_FILES
            or (in_config and rel.name.startswith("identifiers"))
            # this computer's settings, one file per condition (config/conditions.yaml); the example is shared
            or (in_config and rel.name.startswith("settings") and rel.name != "settings.example.yaml"))


def refuse_data(files: list[Path], repo: Path = REPO) -> list[str]:
    bad = []
    for f in files:
        rel = f.relative_to(repo).as_posix()
        csv_elsewhere = f.suffix.lower() == ".csv" and not rel.startswith(ALLOWED_CSV_DIRS)
        if f.suffix.lower() in DATA_SUFFIXES or csv_elsewhere or PACKET_FILE.match(f.name):
            bad.append(rel)
    return bad


def build(out: Path, repo: Path = REPO) -> tuple[Path, int]:
    files = collect(repo)
    bad = refuse_data(files, repo)
    if bad:
        raise SystemExit("Refusing to build: these files could hold data and must not be shared:\n  "
                         + "\n  ".join(bad))
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            rel = f.relative_to(repo).as_posix()
            info = zipfile.ZipInfo.from_file(f, f"{TOP}/{rel}")
            if f.suffix in {".command", ".sh"}:
                info.external_attr = 0o100755 << 16
            with f.open("rb") as fh:
                z.writestr(info, fh.read(), zipfile.ZIP_DEFLATED)
    return out, len(files)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", help="zip path (default: dist/Neurology_REDCap_Extractor_<date>.zip)")
    a = ap.parse_args(argv)
    out = Path(a.out) if a.out else REPO / "dist" / f"Neurology_REDCap_Extractor_{date.today().isoformat()}.zip"
    path, n = build(out)
    print(f"Wrote {path} ({n} files, {path.stat().st_size / 1e6:.1f} MB). No settings, no data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
