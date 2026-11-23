"""`nmosd review`: start the Streamlit app bound to 127.0.0.1, headless, usage statistics off."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from nmosd.extract.run import resolve_dictionary
from nmosd.safety import ensure_phi_output


def streamlit_command(extract: Path, deid: Path, review: Path, dictionary: Path, port: int = 8501) -> list[str]:
    app = Path(__file__).with_name("app.py")
    return [sys.executable, "-m", "streamlit", "run", str(app),
            "--server.address", "127.0.0.1", "--server.port", str(port), "--server.headless", "true",
            "--browser.gatherUsageStats", "false", "--server.fileWatcherType", "none",
            "--client.toolbarMode", "viewer",  # no Deploy button or developer menu
            "--", "--extract", str(extract), "--deid", str(deid), "--review", str(review), "--dictionary", str(dictionary)]


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_until_serving(port: int, proc: subprocess.Popen, timeout: float = 120.0, interval: float = 0.5) -> bool:
    """True once the app answers on 127.0.0.1:port; False if it exits first or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if port_in_use(port):
            return True
        time.sleep(interval)
    return False


def launch(extract: Path, deid: Path, review: Path, settings, desktop: bool = False) -> int:
    """Run the app in the foreground. `desktop` (the desktop shortcut) also opens the browser once it answers."""
    review = ensure_phi_output(review, "the review database")
    port = int(settings.get("review.port", 8501))
    if port_in_use(port):
        print(f"Port {port} is already in use, most likely by a review app that is still running (it may have old "
              "code or another dataset open). Stop it with Ctrl+C in its terminal, or close that terminal, then run "
              "this again.", file=sys.stderr)
        return 1
    dictionary = resolve_dictionary(None, sys.stdout)
    env = dict(os.environ, STREAMLIT_BROWSER_GATHER_USAGE_STATS="false", STREAMLIT_SERVER_ADDRESS="127.0.0.1")
    url = f"http://127.0.0.1:{port}"
    cmd = streamlit_command(extract, deid, review, dictionary, port)
    if not desktop:
        print(f"Review app: {url}  (Ctrl+C to stop)")
        return subprocess.call(cmd, env=env)

    print(f"Review app: {url}\nKeep this window open while you review: closing it stops the app.")
    proc = subprocess.Popen(cmd, env=env)
    try:
        if wait_until_serving(port, proc):
            webbrowser.open(url)
        elif proc.poll() is None:
            print(f"The app is slow to start. Open {url} in your browser in a minute.")
        return proc.wait()
    except KeyboardInterrupt:  # Ctrl+C reaches Streamlit too; give it time to shut down cleanly
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
        return 0
