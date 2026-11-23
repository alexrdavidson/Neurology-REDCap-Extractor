"""The start screen's choice of condition (config/conditions.yaml) and the app's own window (19.11.26).

Nothing here starts a server or opens a window: the window toolkits, browsers and dialogs are replaced by fakes.
"""

from __future__ import annotations

import importlib.util
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from nmosd import conditions as cnd
from nmosd.config import CONFIG_DIR, ConfigError, _not_a_rule, load_settings
from tests.conftest import REPO

GUIDE = REPO / "src" / "nmosd" / "app" / "guide.py"
EXAMPLE = REPO / "config" / "settings.example.yaml"


# ============================================================================================== the conditions list
def test_the_committed_list_has_nmosd_ready_and_the_rest_planned():
    conds = cnd.load_conditions()
    nmosd = cnd.find(conds, "nmosd")
    assert nmosd is not None and nmosd.ready and nmosd.settings == "settings.yaml"   # the file it always used
    assert cnd.settings_path(nmosd) == CONFIG_DIR / "settings.yaml"
    assert [c.id for c in conds if c.ready] == ["nmosd"]
    assert {"mogad", "ms", "myasthenia_gravis", "autoimmune_encephalitis", "epilepsy", "parkinsons", "als",
            "stroke", "dementia", "migraine"} == {c.id for c in conds if not c.ready}
    assert list(cnd.by_group(conds)) == ["Neuroimmunology", "General neurology", "Dementia & headache"]
    assert all(c.name and c.full_name for c in conds)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "conditions.yaml"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.parametrize("body, match", [
    ("conditions: []", "non-empty"),
    ("conditions:\n- {id: a b, name: X, group: G, status: ready}", "id must be"),
    ("conditions:\n- {id: example, name: X, group: G, status: planned}", "id must be"),
    ("conditions:\n- {id: xx, name: X, group: G, status: maybe}", "status must be"),
    ("conditions:\n- {id: xx, name: X, group: G, status: planned}\n- {id: xx, name: Y, group: G, status: planned}",
     "listed twice"),
    ("conditions:\n- {id: xx, name: '', group: G, status: planned}", "needs a name"),
    # a settings file the .gitignore would not cover, or the shared template, is refused
    ("conditions:\n- {id: xx, name: X, group: G, status: ready, settings: my_paths.yaml}", "kept out of git"),
    ("conditions:\n- {id: xx, name: X, group: G, status: ready, settings: settings.example.yaml}", "kept out of git"),
    ("conditions:\n- {id: aa, name: A, group: G, status: ready, settings: settings.yaml}\n"
     "- {id: bb, name: B, group: G, status: ready, settings: settings.yaml}", "shares its settings"),
])
def test_a_broken_list_is_refused(tmp_path, body, match):
    with pytest.raises(ConfigError, match=match):
        cnd.load_conditions(_write(tmp_path, body))


def test_each_ready_condition_gets_its_own_git_ignored_settings_file(tmp_path):
    (c,) = cnd.load_conditions(_write(tmp_path, "conditions:\n- {id: mogad, name: MOGAD, group: G, status: ready}"))
    assert c.settings == "settings.mogad.yaml"
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"config/settings.yaml", "config/settings.*.yaml", "!config/settings.example.yaml"} <= set(ignore)


def test_settings_and_the_conditions_list_are_not_extraction_rules():
    assert _not_a_rule(CONFIG_DIR / "conditions.yaml")
    assert _not_a_rule(CONFIG_DIR / "settings.yaml") and _not_a_rule(CONFIG_DIR / "settings.mogad.yaml")
    assert not _not_a_rule(CONFIG_DIR / "settings.example.yaml")
    assert not _not_a_rule(CONFIG_DIR / "fields" / "pilot_fields.yaml")


# ============================================================================================== the start screen
def _guide(monkeypatch, argv: list[str]):
    st_testing = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setattr(sys, "argv", ["guide.py", *argv])
    return st_testing.AppTest.from_file(str(GUIDE), default_timeout=60)


def _run(at):
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _texts(at) -> str:
    return " ".join(x.value for kind in ("title", "subheader", "info", "markdown", "caption") for x in getattr(at, kind))


@pytest.mark.streamlit
def test_the_app_opens_on_the_choice_of_condition(monkeypatch):
    import nmosd.app.workspace as ws

    real = ws.load_settings

    def only_the_template(path):
        assert Path(path).name == "settings.example.yaml", "no condition's settings before one is opened"
        return real(path)

    monkeypatch.setattr(ws, "load_settings", only_the_template)
    at = _run(_guide(monkeypatch, []))
    assert at.title[0].value == "Neurology REDCap Extractor"
    assert "Which condition are you working on?" in _texts(at)
    keys = {b.key for b in at.button}
    assert {f"cond_{c.id}" for c in cnd.load_conditions()} <= keys
    assert at.button(key="cond_nmosd").label == "Open" and at.button(key="cond_mogad").label == "What's needed"
    assert not [r for r in at.radio if r.key == "g_step"], "no steps before a condition is chosen"

    # a condition that is not set up yet explains what is needed, and goes back
    at.button(key="cond_mogad").click()
    at = _run(at)
    assert at.title[0].value == "MOGAD" and "not set up in this app yet" in _texts(at)
    assert "data dictionary" in _texts(at) and not [r for r in at.radio if r.key == "g_step"]
    at.button(key="cond_back").click()
    at = _run(at)
    assert "Which condition are you working on?" in _texts(at)


@pytest.mark.streamlit
def test_opening_nmosd_shows_the_guide_and_change_condition_goes_back(monkeypatch, tmp_path):
    settings = tmp_path / "settings.yaml"
    at = _run(_guide(monkeypatch, ["--settings", str(settings)]))
    at.button(key="cond_nmosd").click()
    at = _run(at)
    assert at.title[0].value == "NMOSD" and "Neuromyelitis optica" in _texts(at)
    steps = at.radio(key="g_step")
    assert len(steps.options) == 10 and "Check this computer" in steps.options[0]
    at.session_state["g_typed"] = "something typed for this condition"
    at.button(key="change_condition").click()
    at = _run(at)
    assert "Which condition are you working on?" in _texts(at)
    assert "g_typed" not in at.session_state and "condition" not in at.session_state, "nothing carries over"
    assert not settings.exists(), "only step 2 writes the settings"


# ============================================================================================== the window
def test_the_window_falls_back_to_an_app_mode_browser_then_the_browser(monkeypatch):
    import nmosd.app.window as win

    opened: list[str] = []
    monkeypatch.setattr(win.webbrowser, "open", opened.append)
    monkeypatch.setattr(win, "webview_window", lambda url: True)
    assert win.open_window("http://127.0.0.1:8501") == "window" and opened == []
    monkeypatch.setattr(win, "webview_window", lambda url: False)
    monkeypatch.setattr(win, "chromium_window", lambda url: True)
    assert win.open_window("http://127.0.0.1:8501") == "app-mode"
    monkeypatch.setattr(win, "chromium_window", lambda url: False)
    assert win.open_window("http://127.0.0.1:8501") == "browser" and opened == ["http://127.0.0.1:8501"]


def test_the_app_mode_window_uses_a_throwaway_profile(monkeypatch, tmp_path):
    import nmosd.app.window as win

    exe = tmp_path / "msedge.exe"
    exe.write_bytes(b"")
    seen: dict = {}

    class FakeBrowser:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            seen["profile"] = Path(next(a for a in cmd if a.startswith("--user-data-dir=")).split("=", 1)[1])
            assert seen["profile"].is_dir()

        def wait(self):
            return 0

    monkeypatch.setattr(win, "find_chromium", lambda: exe)
    monkeypatch.setattr(win.subprocess, "Popen", FakeBrowser)
    assert win.chromium_window("http://127.0.0.1:8502") is True
    assert seen["cmd"][0] == str(exe) and "--app=http://127.0.0.1:8502" in seen["cmd"]
    assert not seen["profile"].exists(), "the profile is deleted when the window closes"
    monkeypatch.setattr(win, "find_chromium", lambda: None)
    assert win.chromium_window("http://127.0.0.1:8502") is False


def test_with_no_terminal_prints_go_to_a_log_outside_the_app_folder(monkeypatch, tmp_path):
    import nmosd.app.window as win

    assert not win.log_dir().is_relative_to(REPO), "never inside the app folder"
    monkeypatch.setattr(win, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    log = win.ensure_streams()
    try:
        print("hello from pythonw")
        sys.stdout.flush()
    finally:
        sys.stdout.close()
    assert log == tmp_path / "logs" / "launcher.log" and "hello from pythonw" in log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------- `nmosd app --window`
class _Server:
    pid = 4343
    started: ClassVar[list] = []

    def __init__(self, cmd, env=None, **io):
        self.cmd, self.io, self.terminated, self.rc = cmd, io, False, None
        _Server.started.append(self)

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated, self.rc = True, 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.rc = -9


def _window_launch(monkeypatch, tmp_path, how: str, serving: bool = True, console: bool = False, settings=None):
    import nmosd.app.launch as L

    shown: list[tuple[str, bool]] = []
    _Server.started = []
    monkeypatch.setattr(L, "SERVER_FILE", tmp_path / "server.json")
    monkeypatch.setattr(L, "port_in_use", lambda port: False)
    monkeypatch.setattr(L, "running_guide_port", lambda: None)
    monkeypatch.setattr(L, "wait_until_serving", lambda port, proc: serving)
    monkeypatch.setattr(L.subprocess, "Popen", _Server)
    monkeypatch.setattr(L.win, "open_window", lambda url: how)
    monkeypatch.setattr(L.win, "has_console", lambda: console)
    monkeypatch.setattr(L.win, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(L.win, "message", lambda text, title="", error=False: shown.append((text, error)))
    rc = L.launch_app(load_settings(EXAMPLE), settings, window=True)
    return rc, _Server.started[0] if _Server.started else None, shown


def test_closing_the_window_closes_the_app(monkeypatch, tmp_path):
    rc, server, shown = _window_launch(monkeypatch, tmp_path, "window")
    assert rc == 0 and server.terminated and shown == []
    assert "--settings" not in server.cmd, "each condition uses its own settings file"
    assert server.io["stdin"] is subprocess.DEVNULL and (tmp_path / "logs" / "server.log").exists()


def test_a_settings_file_given_on_the_command_line_reaches_the_app(monkeypatch, tmp_path):
    _, server, _ = _window_launch(monkeypatch, tmp_path, "app-mode", console=True, settings=tmp_path / "s.yaml")
    assert server.cmd[-2:] == ["--settings", str(tmp_path / "s.yaml")] and server.terminated
    assert server.io == {}, "started from a terminal: the server's messages stay in the terminal"


def test_in_the_browser_with_no_terminal_a_dialog_closes_the_app(monkeypatch, tmp_path):
    rc, server, shown = _window_launch(monkeypatch, tmp_path, "browser")
    assert rc == 0 and server.terminated
    assert len(shown) == 1 and "open in your web browser" in shown[0][0] and not shown[0][1]


def test_a_server_that_fails_to_start_is_reported_in_a_dialog(monkeypatch, tmp_path):
    import nmosd.app.launch as L

    monkeypatch.setattr(L.subprocess, "Popen", _Server)
    monkeypatch.setattr(_Server, "poll", lambda self: 1)
    rc, _, shown = _window_launch(monkeypatch, tmp_path, "window", serving=False)
    assert rc == 1 and shown and shown[0][1] and "could not start" in shown[0][0] and "server.log" in shown[0][0]


def test_the_cli_starts_the_window_and_reports_a_crash(monkeypatch):
    import nmosd.app.launch as L
    import nmosd.app.window as win
    import nmosd.cli as cli

    seen: dict = {}
    monkeypatch.setattr(L, "launch_app", lambda settings, **kw: seen.update(kw) or 0)
    assert cli.main(["app", "--window"]) == 0
    assert seen["window"] is True and seen["settings_path"] is None

    def crash(settings, **kw):
        raise RuntimeError("boom")

    shown: list[str] = []
    monkeypatch.setattr(L, "launch_app", crash)
    monkeypatch.setattr(win, "message", lambda text, title="", error=False: shown.append(text))
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("the window mode never waits for Enter"))
    assert cli.main(["app", "--window"]) == 1
    assert len(shown) == 1 and "RuntimeError" in shown[0]


# ============================================================================================== the Mac app
def _mac_script():
    spec = importlib.util.spec_from_file_location("make_mac_app", REPO / "scripts" / "make_mac_app.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_mac_app"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_mac_app_runs_the_window_from_this_folder(tmp_path):
    image = pytest.importorskip("PIL.Image")
    mod = _mac_script()
    app = mod.build(tmp_path)
    assert app.name == "Neurology REDCap Extractor.app"
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleName"] == "Neurology REDCap Extractor" and info["CFBundleExecutable"] == "launcher"
    launcher = app / "Contents" / "MacOS" / "launcher"
    text = launcher.read_text(encoding="utf-8")
    assert launcher.stat().st_mode & 0o111 and "exec ./.venv/bin/python -m nmosd app --window" in text
    assert str(REPO) in text and "Library/Logs/Neurology REDCap Extractor" in text
    if shutil.which("bash"):
        assert subprocess.run(["bash", "-n", str(launcher)], capture_output=True).returncode == 0
    with image.open(app / "Contents" / "Resources" / "AppIcon.icns") as im:
        assert im.size == (1024, 1024)
    assert mod.main(["--dry-run", "--dest", str(tmp_path / "elsewhere")]) == 0
    assert not (tmp_path / "elsewhere").exists()
