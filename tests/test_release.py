"""The release zip (scripts/make_release.py) and the Windows installer files. Nothing here installs anything."""

from __future__ import annotations

import importlib.util
import re
import sys
import zipfile

import pytest

from tests.conftest import REPO


def _release():
    spec = importlib.util.spec_from_file_location("make_release", REPO / "scripts" / "make_release.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_release"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_release_zip_has_the_app_and_nothing_else(tmp_path):
    mod = _release()
    out, n = mod.build(tmp_path / "r.zip")
    names = zipfile.ZipFile(out).namelist()
    assert len(names) == n and all(x.startswith("Neurology REDCap Extractor/") for x in names)
    rel = {x.split("/", 1)[1] for x in names}
    for needed in ("Install Neurology REDCap Extractor.bat", "Start Neurology REDCap Extractor.bat",
                   "Start Neurology REDCap Extractor.command", "START HERE.txt", "pyproject.toml",
                   "scripts/windows/install.ps1", "scripts/make_desktop_shortcut.py", "scripts/make_mac_app.py",
                   "src/nmosd/app/guide.py", "src/nmosd/app/window.py", "config/conditions.yaml",
                   "assets/icon/nmosd_extractor.ico", "docs/USER_GUIDE.md", "config/settings.example.yaml"):
        assert needed in rel, needed
    assert "config/settings.yaml" not in rel
    assert not any(part in {".venv", "__pycache__", ".app_state", "dist"} for x in rel for part in x.split("/"))
    assert not any(x.endswith((".sqlite", ".db", ".accdb", ".mdb", ".enc")) for x in rel)
    assert not {"CLAUDE.md", "TODO.md", "BUILD_PLAN.md"} & rel
    assert not any("quality review" in x or "/packet_" in x or "/Patient_" in x for x in rel)


def test_release_refuses_files_that_could_hold_data(tmp_path):
    mod = _release()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "src" / "leftover.sqlite").write_bytes(b"")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "patients.csv").write_text("empi\n")
    (tmp_path / "config").mkdir()
    for name in ("settings.yaml", "settings.mogad.yaml", "settings.example.yaml"):
        (tmp_path / "config" / name).write_text("paths: {}\n")
    packet = tmp_path / "docs" / "NMOSD quality review" / "packet_20261118_1521"
    packet.mkdir(parents=True)
    (packet / "Patient_A_notes_01.md").write_text("scrubbed notes\n")
    (tmp_path / "docs" / "Patient_B.md").write_text("a packet file outside its folder\n")
    (tmp_path / "src" / "Neurology REDCap Extractor.app" / "Contents").mkdir(parents=True)
    (tmp_path / "src" / "Neurology REDCap Extractor.app" / "Contents" / "Info.plist").write_text("<plist/>")
    files = mod.collect(tmp_path)
    rels = {f.relative_to(tmp_path).as_posix() for f in files}
    assert {r for r in rels if r.startswith("config/")} == {"config/settings.example.yaml"}, "only the template"
    assert not any("packet_" in r or ".app/" in r for r in rels)
    assert sorted(mod.refuse_data(files, tmp_path)) == ["docs/Patient_B.md", "docs/patients.csv",
                                                        "src/leftover.sqlite"]
    with pytest.raises(SystemExit, match="Refusing to build"):
        mod.build(tmp_path / "out.zip", tmp_path)


def test_windows_launchers_point_at_real_files():
    install = (REPO / "Install Neurology REDCap Extractor.bat").read_bytes()
    start = (REPO / "Start Neurology REDCap Extractor.bat").read_bytes()
    for raw in (install, start):
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")    # CRLF only
    assert b"scripts\\windows\\install.ps1" in install and (REPO / "scripts" / "windows" / "install.ps1").exists()
    assert b'start "" ".venv\\Scripts\\pythonw.exe" -m nmosd app --window' in start    # no console window
    mac = (REPO / "Start Neurology REDCap Extractor.command").read_text(encoding="utf-8")
    assert "-m nmosd app --window" in mac and (REPO / "Start Neurology REDCap Extractor.command").stat().st_mode & 0o111


def test_installer_is_ascii_and_installs_existing_extras():
    ps1 = (REPO / "scripts" / "windows" / "install.ps1").read_bytes()
    ps1.decode("ascii")                       # Windows PowerShell 5.1 reads a BOM-less script as ANSI
    text = ps1.decode()
    extras = {e for group in re.findall(r'pip install --disable-pip-version-check -e "\.\[([\w,]+)\]"', text)
              for e in group.split(",")}
    assert {"app", "window"} <= extras
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    for e in extras:
        assert re.search(rf"^{e} = \[", pyproject, re.M), e
    assert "127.0.0.1:11434" in text and "0.0.0.0" not in text
    assert "qwen3:8b" in text and "download-models" in text and "make_desktop_shortcut.py" in text
    # every function used is defined
    used = set(re.findall(r"\b((?:Say|Stop|Has|Test|Find)-\w+)", text))
    defined = set(re.findall(r"^function ([\w-]+)", text, re.M))
    assert used - {"Test-Path"} <= defined, used - defined
