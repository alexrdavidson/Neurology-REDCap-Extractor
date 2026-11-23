"""Create the Windows desktop shortcut that opens Neurology REDCap Extractor in its own window.

Run it once on the Windows laptop, in the repo folder, with the project's virtual environment:

    .venv\\Scripts\\python scripts\\make_desktop_shortcut.py

The shortcut runs `pythonw -m nmosd app --window` in the app folder: no console window, just the app's own window
(closing it closes the app, while work running in the background carries on). Double-clicking again while it runs
opens another window on the running app. It replaces the older "NMOSD Extractor" shortcut.
The shortcut holds no data paths: each condition's settings are read from config/ at launch.

    --dry-run   show what would be created and change nothing (works on any OS)
    --dest DIR  put the shortcut in DIR instead of on the desktop
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ICON = REPO / "assets" / "icon" / "nmosd_extractor.ico"
NAME = "Neurology REDCap Extractor"
OLD_NAME = "NMOSD Extractor"       # the shortcut made before 19.11.26, removed when the new one is made
ARGUMENTS = "-m nmosd app --window"
DESCRIPTION = "Neurology REDCap Extractor (runs on this computer only)"

# Every value reaches PowerShell through an environment variable, so no path is ever pasted into the script.
# GetFolderPath('Desktop') also finds a desktop that OneDrive has moved.
POWERSHELL = "; ".join([
    "$ErrorActionPreference = 'Stop'",
    "$dest = $env:NMOSD_SC_DEST",
    "if (-not $dest) { $dest = [Environment]::GetFolderPath('Desktop') }",
    "$lnk = Join-Path $dest ($env:NMOSD_SC_NAME + '.lnk')",
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)",
    "$s.TargetPath = $env:NMOSD_SC_TARGET",
    "$s.Arguments = $env:NMOSD_SC_ARGS",
    "$s.WorkingDirectory = $env:NMOSD_SC_WORKDIR",
    "$s.IconLocation = $env:NMOSD_SC_ICON + ',0'",
    "$s.Description = $env:NMOSD_SC_DESC",
    "$s.WindowStyle = 1",
    "$s.Save()",
    "$old = Join-Path $dest ($env:NMOSD_SC_OLD + '.lnk')",
    "if ($env:NMOSD_SC_OLD -and (Test-Path -LiteralPath $old)) { Remove-Item -LiteralPath $old }",
    "Write-Output $lnk",
])


@dataclass(frozen=True)
class Shortcut:
    name: str
    target: Path
    arguments: str
    working_dir: Path
    icon: Path
    description: str


def windowless(python: Path) -> Path:
    """pythonw.exe next to python.exe: the same Python with no console window. python.exe if there is none."""
    w = python.with_name("pythonw.exe")
    return w if python.name.lower() == "python.exe" and w.exists() else python


def build_shortcut(python: Path | None = None) -> Shortcut:
    return Shortcut(NAME, windowless(Path(python or sys.executable)), ARGUMENTS, REPO, ICON, DESCRIPTION)


def is_windows() -> bool:
    return sys.platform == "win32"


def powershell_command(sc: Shortcut, dest: Path | None) -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ, NMOSD_SC_NAME=sc.name, NMOSD_SC_TARGET=str(sc.target), NMOSD_SC_ARGS=sc.arguments,
               NMOSD_SC_WORKDIR=str(sc.working_dir), NMOSD_SC_ICON=str(sc.icon), NMOSD_SC_DESC=sc.description,
               NMOSD_SC_OLD=OLD_NAME if sc.name != OLD_NAME else "")
    env.pop("NMOSD_SC_DEST", None)
    if dest:
        env["NMOSD_SC_DEST"] = str(dest)
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL], env


def preflight(sc: Shortcut) -> list[str]:
    problems = []
    if not sc.icon.exists():
        problems.append(f"Icon not found: {sc.icon} (rebuild it with scripts/make_icon.py)")
    for module, extra in (("nmosd", "."), ("streamlit", ".[review]")):
        if importlib.util.find_spec(module) is None:
            problems.append(f"'{module}' is not installed for {sc.target}. Run this script with the project's "
                            f'venv (.venv\\Scripts\\python), or install it: pip install -e "{extra}"')
    return problems


def describe(sc: Shortcut, dest: Path | None) -> str:
    return "\n".join([
        f"Shortcut   {sc.name}.lnk  in  {dest or 'the desktop'}",
        f"Target     {sc.target}",
        f"Arguments  {sc.arguments}",
        f"Start in   {sc.working_dir}",
        f"Icon       {sc.icon}",
    ])


def manual_steps(sc: Shortcut) -> str:
    return "\n".join([
        "Could not create the shortcut automatically (PowerShell may be restricted on this computer).",
        "To make it by hand:",
        "  1. Right-click the desktop > New > Shortcut.",
        f'  2. Location:  "{sc.target}" {sc.arguments}',
        f"     Next, name it {sc.name}, then Finish.",
        f'  3. Right-click the new shortcut > Properties. Start in:  "{sc.working_dir}"',
        f"  4. Change Icon... > Browse... > {sc.icon} > OK > OK.",
    ])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create the Windows desktop shortcut for Neurology REDCap Extractor.")
    ap.add_argument("--dry-run", action="store_true", help="show what would be created; change nothing")
    ap.add_argument("--dest", type=Path, help="folder for the shortcut (default: the desktop)")
    args = ap.parse_args(argv)

    sc = build_shortcut()
    print(describe(sc, args.dest))
    if args.dry_run:
        return 0
    if not is_windows():
        print("\nThis makes a Windows shortcut: run it on the Windows laptop (--dry-run previews it here).",
              file=sys.stderr)
        return 1
    if sys.prefix == sys.base_prefix:
        print("\nNote: this is not a virtual environment's Python; the shortcut will start this Python.",
              file=sys.stderr)
    problems = preflight(sc)
    if problems:
        print("\n" + "\n".join(problems), file=sys.stderr)
        return 1

    cmd, env = powershell_command(sc, args.dest)
    try:
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"\nPowerShell could not run: {e}", file=sys.stderr)
        res = None
    created = res.stdout.strip().splitlines()[-1] if res and res.returncode == 0 and res.stdout.strip() else ""
    if not created:
        if res is not None and res.stderr.strip():
            print("\n" + res.stderr.strip().splitlines()[0], file=sys.stderr)
        print("\n" + manual_steps(sc), file=sys.stderr)
        return 1
    print(f"\nCreated {created}\nDouble-click it to start {NAME}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
