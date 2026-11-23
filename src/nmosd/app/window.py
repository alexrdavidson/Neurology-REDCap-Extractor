"""The app's own window: the guided app in a window called "Neurology REDCap Extractor" instead of a browser tab.

`nmosd app --window` (the Mac app, the Windows shortcut and the Start files) tries, in order:

1. pywebview (the `window` extra): a native window. WebKit on a Mac, Microsoft Edge WebView2 on Windows.
2. Microsoft Edge or Google Chrome in app mode (`--app=`): a window with no tabs or address bar, using a fresh
   profile in a temporary folder that is deleted when the window closes.
3. The default browser, as before.

The window only shows the app served on 127.0.0.1. Closing it closes the app; work running in the background
(de-identification, extraction) carries on, and the app picks it up when it is opened again.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

from nmosd.config import REPO_ROOT

APP_NAME = "Neurology REDCap Extractor"
ICON_PNG = REPO_ROOT / "assets" / "icon" / "nmosd_extractor.png"
ICON_ICO = REPO_ROOT / "assets" / "icon" / "nmosd_extractor.ico"
SIZE = (1440, 920)
MIN_SIZE = (960, 640)


# ---------------------------------------------------------------------------------------------- 1. native window
def _mac_app_name() -> None:
    """Name the menu bar and the Quit/Hide/About items after the app instead of "Python". Must run before the
    window toolkit starts the Cocoa application."""
    try:
        import AppKit  # comes with pywebview on a Mac (pyobjc)

        bundle = AppKit.NSBundle.mainBundle()
        for info in (bundle.localizedInfoDictionary(), bundle.infoDictionary()):
            if info is not None:
                info["CFBundleName"] = APP_NAME
    except Exception:
        pass


def webview_window(url: str) -> bool:
    """Show `url` in a native window until it is closed. False if pywebview is missing or cannot start."""
    if sys.platform == "darwin":
        _mac_app_name()
    try:
        import webview
    except Exception:
        return False
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True                 # the accuracy zip, the review exports
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.create_window(APP_NAME, url, width=SIZE[0], height=SIZE[1], min_size=MIN_SIZE, text_select=True,
                              zoomable=True)
        icon = ICON_ICO if os.name == "nt" else ICON_PNG
        # private_mode: nothing the window shows is kept on disk after it closes
        webview.start(private_mode=True, icon=str(icon) if icon.exists() else None)
    except Exception as e:
        print(f"The app window could not open ({type(e).__name__}); trying another way.", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------------------------- 2. Edge/Chrome app mode
def chromium_candidates() -> list[Path]:
    """Edge or Chrome executables that may exist on this computer, in order of preference."""
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
        return [Path(root) / rel for rel in rels for root in roots if root]
    if sys.platform == "darwin":
        apps = ["Google Chrome", "Microsoft Edge", "Chromium"]
        bases = [Path("/Applications"), Path.home() / "Applications"]
        return [b / f"{a}.app" / "Contents" / "MacOS" / a for a in apps for b in bases]
    return [Path(p) for n in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser")
            if (p := shutil.which(n))]


def find_chromium() -> Path | None:
    return next((p for p in chromium_candidates() if p.is_file()), None)


def chromium_command(exe: Path, url: str, profile: Path) -> list[str]:
    return [str(exe), f"--app={url}", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--disable-sync", "--disable-extensions", f"--window-size={SIZE[0]},{SIZE[1]}"]


def chromium_window(url: str) -> bool:
    """Show `url` in an Edge/Chrome app window until it is closed. False if neither browser is installed.

    A profile of its own makes this a separate browser process, so the call returns when the window closes."""
    exe = find_chromium()
    if exe is None:
        return False
    profile = Path(tempfile.mkdtemp(prefix="nrx_window_"))
    try:
        proc = subprocess.Popen(chromium_command(exe, url, profile), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait()
    except OSError:
        return False
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return True


# ---------------------------------------------------------------------------------------------- choose
def open_window(url: str) -> str:
    """Open the app and return how: "window" or "app-mode" (both return once the window is closed), or "browser"
    (returns at once: the browser tab is not ours to watch)."""
    if webview_window(url):
        return "window"
    if chromium_window(url):
        return "app-mode"
    webbrowser.open(url)
    return "browser"


# ---------------------------------------------------------------------------------------------- no terminal
def has_console() -> bool:
    """Started from a terminal (not the Mac app, the Windows shortcut or the Start file)."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def log_dir() -> Path:
    """Where the app's own messages go when there is no terminal: outside the app folder, never patient data."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME / "logs"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / APP_NAME
    return base


def ensure_streams() -> Path | None:
    """With no terminal (pythonw on Windows has none at all), send prints to a log file instead of nowhere."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    try:
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "launcher.log"
        stream = log.open("w", encoding="utf-8", buffering=1)
    except OSError:
        return None
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return log


def message(text: str, title: str = APP_NAME, error: bool = False) -> None:
    """A dialog the user must close (blocks until then). Falls back to printing."""
    print(text, file=sys.stderr if error else sys.stdout)
    with contextlib.suppress(Exception):
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            # the text reaches AppleScript as an argument, never pasted into the script
            script = ["on run argv", f"display alert (item 1 of argv) message (item 2 of argv){' as critical' if error else ''}",
                      "end run"]
            args = [a for line in script for a in ("-e", line)]
            subprocess.run(["osascript", *args, title, text], capture_output=True, check=False)
