"""`nmosd app`: start the guided app (Streamlit) on 127.0.0.1 and show it.

* `--window` (the Mac app, the Windows shortcut, the Start files): in its own window called "Neurology REDCap
  Extractor" (`nmosd.app.window`). Closing the window closes the app; background work carries on.
* `--desktop` (older shortcuts): in the browser, with the terminal window kept open.
* neither: in the browser; Ctrl+C in the terminal stops it.

Starting it again while it runs opens another window (or browser tab) on the running app.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

from nmosd.app import window as win
from nmosd.config import REPO_ROOT
from nmosd.review.launch import port_in_use, wait_until_serving

APP_NAME = win.APP_NAME


def app_command(settings_path: Path | None, port: int) -> list[str]:
    """The Streamlit command. Without a settings path the app uses each condition's own settings file."""
    script = Path(__file__).with_name("guide.py")
    cmd = [sys.executable, "-m", "streamlit", "run", str(script),
           "--server.address", "127.0.0.1", "--server.port", str(port), "--server.headless", "true",
           "--browser.gatherUsageStats", "false", "--server.fileWatcherType", "none",
           "--server.maxUploadSize", "20", "--theme.base", "light",
           "--client.toolbarMode", "viewer"]  # no Deploy button or developer menu
    return cmd + (["--", "--settings", str(settings_path)] if settings_path else [])


SERVER_FILE = REPO_ROOT / ".app_state" / "server.json"


def _answers(port: int) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/_stcore/health", timeout=2) as r:
            return r.read().strip() == b"ok"
    except Exception:
        return False


def remember_server(pid: int, port: int) -> None:
    SERVER_FILE.parent.mkdir(parents=True, exist_ok=True)
    SERVER_FILE.write_text(json.dumps({"pid": pid, "port": port}), encoding="utf-8")


def running_guide_port() -> int | None:
    """The port of this app's own server if it is still running (not just any Streamlit app, e.g. `nmosd review`)."""
    try:
        info = json.loads(SERVER_FILE.read_text(encoding="utf-8"))
        import psutil

        cmd = " ".join(psutil.Process(int(info["pid"])).cmdline())
    except Exception:
        return None
    port = int(info["port"])
    return port if "guide.py" in cmd and port_in_use(port) and _answers(port) else None


def free_port(start: int, tries: int = 20) -> int | None:
    return next((p for p in range(start, start + tries) if not port_in_use(p)), None)


def stop_server(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _fail(text: str, window: bool) -> int:
    if window and not win.has_console():
        win.message(text, error=True)
    else:
        print(text, file=sys.stderr)
    return 1


def _server_output(window: bool) -> dict:
    """With no terminal to show it, the server's messages go to a log file outside the app folder."""
    if not window or win.has_console():
        return {}
    folder = win.log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    log = (folder / "server.log").open("w", encoding="utf-8")
    return {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}


def _show(url: str, window: bool) -> str:
    if window:
        return win.open_window(url)
    webbrowser.open(url)
    return "browser"


def launch_app(settings, settings_path: Path | None = None, desktop: bool = False, port: int | None = None,
               window: bool = False) -> int:
    """`settings_path`: only when given on the command line; otherwise each condition uses its own settings."""
    running = running_guide_port()
    if running:
        url = f"http://127.0.0.1:{running}"
        print(f"{APP_NAME} is already running: opening {url}")
        _show(url, window)
        return 0
    wanted = port or int(settings.get("review.port", 8501))
    port = free_port(wanted)
    if port is None:
        return _fail(f"Ports {wanted}-{wanted + 19} are all in use. Restart the computer and try again.", window)
    if port != wanted:
        print(f"Port {wanted} is used by another program (perhaps a review app started with `nmosd review`); "
              f"using port {port} instead.")
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ, STREAMLIT_BROWSER_GATHER_USAGE_STATS="false", STREAMLIT_SERVER_ADDRESS="127.0.0.1",
               PYTHONUTF8="1")
    cmd = app_command(Path(settings_path) if settings_path else None, port)
    print(f"{APP_NAME}: {url}\nEverything runs on this computer. "
          + ("Closing the app window closes the app; " if window else
             "Keep this window open while you use the app (you can minimise it). Closing it closes the app; ")
          + "work running in the background carries on.")
    io = _server_output(window)
    proc = subprocess.Popen(cmd, env=env, **io)
    if io:
        io["stdout"].close()   # the server has its own copy of the file
    remember_server(proc.pid, port)
    try:
        if not wait_until_serving(port, proc):
            if proc.poll() is not None:
                where = f" Details: {win.log_dir() / 'server.log'}" if io else ""
                return _fail(f"{APP_NAME} could not start.{where}", window)
            print(f"The app is slow to start. Open {url} in your browser in a minute.")
            return proc.wait()
        how = _show(url, window)
        if how in {"window", "app-mode"}:
            stop_server(proc)         # the window was closed: the app closes with it
            return 0
        if window and not win.has_console():
            # opened in the browser, with no terminal to close: this dialog is how the app is closed
            win.message(f"{APP_NAME} is open in your web browser.\n\nClick OK when you have finished, to close "
                        "the app. Work running in the background carries on.")
            stop_server(proc)
            return 0
        return proc.wait()
    except KeyboardInterrupt:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
        return 0
