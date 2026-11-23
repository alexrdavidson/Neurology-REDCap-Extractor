#!/usr/bin/env python3
"""Make "Neurology REDCap Extractor.app": the guided app in its own window on a Mac, with no Terminal window.

Run it once on each Mac, in the app folder, with the project's virtual environment (after
`.venv/bin/pip install -e ".[ner,app,window]"`):

    .venv/bin/python scripts/make_mac_app.py                 # the app goes in the app folder
    .venv/bin/python scripts/make_mac_app.py --dest ~/Applications

Then double-click it, or drag it to the Dock. It runs `.venv/bin/python -m nmosd app --window` in this app folder,
so make it again if the folder moves. Its messages go to ~/Library/Logs/Neurology REDCap Extractor/. It holds no
data paths: each condition's settings are read from config/ at launch.

    --dry-run   show what would be made and change nothing
"""

from __future__ import annotations

import argparse
import importlib.util
import plistlib
import shlex
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NAME = "Neurology REDCap Extractor"
BUNDLE_ID = "org.neurology.redcap-extractor"
ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)


def info_plist() -> dict:
    return {
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "launcher",
        "CFBundleIconFile": "AppIcon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
    }


def launcher_script(repo: Path = REPO) -> str:
    q = shlex.quote(str(repo))
    # the message reaches AppleScript as arguments, never pasted into the script
    alert = " ".join(f"-e {shlex.quote(line)}" for line in (
        "on run argv", "display alert (item 1 of argv) message (item 2 of argv) as critical", "end run"))
    lines = [
        "#!/bin/bash",
        f"# {NAME}: made by scripts/make_mac_app.py for the app folder below. Make it again if that folder moves.",
        f"APP={q}",
        f'LOGS="$HOME/Library/Logs/{NAME}"',
        'mkdir -p "$LOGS"',
        f'cd "$APP" 2>/dev/null || {{ osascript {alert} "{NAME}" '
        '"The app folder was not found. Make the app again: .venv/bin/python scripts/make_mac_app.py"; exit 1; }',
        'if [ ! -x .venv/bin/python ]; then',
        f'  osascript {alert} "{NAME} is not set up yet" '
        '"In the app folder: python3.11 -m venv .venv && .venv/bin/pip install -e \\".[ner,app,window]\\""',
        "  exit 1",
        "fi",
        'exec ./.venv/bin/python -m nmosd app --window >"$LOGS/launcher.log" 2>&1 </dev/null',
        "",
    ]
    return "\n".join(lines)


def _icon_module():
    spec = importlib.util.spec_from_file_location("make_icon", REPO / "scripts" / "make_icon.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_icon"] = mod
    spec.loader.exec_module(mod)
    return mod


def write_icns(out: Path) -> None:
    """The desktop icon (scripts/make_icon.py), drawn at every size a Mac uses."""
    icons = _icon_module().build(ICNS_SIZES)
    largest = icons[max(ICNS_SIZES)]
    largest.save(out, format="ICNS", append_images=[icons[s] for s in ICNS_SIZES if s != max(ICNS_SIZES)])


def build(dest: Path, repo: Path = REPO) -> Path:
    app = dest / f"{NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump(info_plist(), f)
    launcher = contents / "MacOS" / "launcher"
    launcher.write_text(launcher_script(repo), encoding="utf-8")
    launcher.chmod(0o755)
    write_icns(contents / "Resources" / "AppIcon.icns")
    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", type=Path, default=REPO, help="folder for the app (default: the app folder)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be made; change nothing")
    args = ap.parse_args(argv)
    dest = args.dest.expanduser()
    print(f"App       {dest / (NAME + '.app')}\nRuns      {REPO / '.venv/bin/python'} -m nmosd app --window\n"
          f"Logs      ~/Library/Logs/{NAME}/")
    if args.dry_run:
        return 0
    if sys.platform != "darwin":
        print("\nThis makes a Mac app: run it on a Mac (--dry-run previews it here). On Windows use "
              "scripts/make_desktop_shortcut.py.", file=sys.stderr)
        return 1
    if importlib.util.find_spec("webview") is None:
        print('\nNote: pywebview is not installed, so the app opens in a Chrome/Edge window or the browser. For its '
              'own window: .venv/bin/pip install -e ".[window]"', file=sys.stderr)
    app = build(dest)
    print(f"\nMade {app}\nDouble-click it to start {NAME}, or drag it to the Dock.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
