"""Desktop shortcut (Phase 10): the icon, the shortcut script (now `pythonw -m nmosd app --window`, 19.11.26) and
`nmosd review --desktop`. Nothing here starts a server, opens a browser or writes outside tmp_path."""

from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest

from tests.conftest import REPO

ICON = REPO / "assets" / "icon" / "nmosd_extractor.ico"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------------------------- icon
def test_icon_has_every_windows_size():
    image = pytest.importorskip("PIL.Image")
    sizes = _script("make_icon").ICO_SIZES
    with image.open(ICON) as im:
        assert sorted(im.info["sizes"]) == [(s, s) for s in sizes]


def test_icon_build_is_reproducible(tmp_path):
    pytest.importorskip("PIL")
    mod = _script("make_icon")
    assert mod.main(["--out-dir", str(tmp_path)]) == 0
    assert (tmp_path / "nmosd_extractor.png").read_bytes() == (ICON.with_suffix(".png")).read_bytes()


# ---------------------------------------------------------------------------------------------- shortcut
def test_shortcut_opens_the_app_window_from_the_repo(tmp_path):
    mod = _script("make_desktop_shortcut")
    sc = mod.build_shortcut(tmp_path / "python.exe")
    assert sc.target == tmp_path / "python.exe"          # no pythonw.exe next to it
    assert sc.name == "Neurology REDCap Extractor"
    assert sc.arguments == "-m nmosd app --window"
    assert sc.working_dir == REPO
    assert sc.icon == ICON and ICON.exists()
    (tmp_path / "pythonw.exe").write_bytes(b"")
    assert mod.build_shortcut(tmp_path / "python.exe").target == tmp_path / "pythonw.exe", "no console window"


def test_paths_reach_powershell_only_through_the_environment(monkeypatch, tmp_path):
    mod = _script("make_desktop_shortcut")
    monkeypatch.setenv("NMOSD_SC_DEST", "somewhere-stale")
    sc = mod.build_shortcut(tmp_path / "py'thon.exe")
    cmd, env = mod.powershell_command(sc, None)
    assert cmd[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert str(tmp_path) not in " ".join(cmd) and '"' not in mod.POWERSHELL
    assert env["NMOSD_SC_TARGET"] == str(sc.target) and env["NMOSD_SC_WORKDIR"] == str(REPO)
    assert env["NMOSD_SC_OLD"] == "NMOSD Extractor" and "Remove-Item -LiteralPath $old" in mod.POWERSHELL
    assert "NMOSD_SC_DEST" not in env
    _, env = mod.powershell_command(sc, tmp_path)
    assert env["NMOSD_SC_DEST"] == str(tmp_path)


def test_dry_run_changes_nothing(monkeypatch, capsys):
    mod = _script("make_desktop_shortcut")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: pytest.fail("dry run must not call PowerShell"))
    assert mod.main(["--dry-run"]) == 0
    assert "-m nmosd app --window" in capsys.readouterr().out


def test_refuses_off_windows(monkeypatch, capsys):
    mod = _script("make_desktop_shortcut")
    monkeypatch.setattr(mod, "is_windows", lambda: False)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: pytest.fail("must not call PowerShell"))
    assert mod.main([]) == 1
    assert "Windows" in capsys.readouterr().err


@pytest.mark.parametrize("result", [
    subprocess.CompletedProcess([], 1, "", "Cannot create type. Only core types are supported in this language mode."),
    OSError("powershell.exe not found"),
])
def test_falls_back_to_manual_steps(monkeypatch, capsys, result):
    mod = _script("make_desktop_shortcut")
    monkeypatch.setattr(mod, "is_windows", lambda: True)
    monkeypatch.setattr(mod, "preflight", lambda sc: [])

    def run(*a, **k):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(mod.subprocess, "run", run)
    assert mod.main([]) == 1
    err = capsys.readouterr().err
    assert "Right-click the desktop" in err and "-m nmosd app --window" in err and str(ICON) in err


def test_reports_the_created_shortcut(monkeypatch, capsys):
    mod = _script("make_desktop_shortcut")
    monkeypatch.setattr(mod, "is_windows", lambda: True)
    monkeypatch.setattr(mod, "preflight", lambda sc: [])
    lnk = r"C:\Users\someone\OneDrive\Desktop\Neurology REDCap Extractor.lnk"
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, lnk + "\n", ""))
    assert mod.main([]) == 0
    assert lnk in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------- review --desktop
class _FakeApp:
    def __init__(self, cmd, env):
        self.cmd = cmd

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


def _desktop_launch(monkeypatch, tmp_path, port_answers):
    import nmosd.review.launch as launch_mod
    from nmosd.config import load_settings

    answers = iter(port_answers)
    opened: list[str] = []
    monkeypatch.setattr(launch_mod, "port_in_use", lambda port: next(answers))
    monkeypatch.setattr(launch_mod, "ensure_phi_output", lambda p, what: p)
    monkeypatch.setattr(launch_mod, "resolve_dictionary", lambda *a: tmp_path / "dict.csv")
    monkeypatch.setattr(launch_mod.subprocess, "Popen", _FakeApp)
    monkeypatch.setattr(launch_mod.subprocess, "call", lambda *a, **k: pytest.fail("desktop mode uses Popen"))
    monkeypatch.setattr(launch_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(launch_mod.webbrowser, "open", opened.append)
    settings = load_settings(REPO / "config" / "settings.example.yaml")
    rc = launch_mod.launch(tmp_path / "e", tmp_path / "d", tmp_path / "r", settings, desktop=True)
    return rc, opened, int(settings.get("review.port", 8501))


def test_desktop_launch_opens_the_browser_once_the_app_answers(monkeypatch, tmp_path):
    # first answer: the pre-start check (port free); then two polls before the app is up
    rc, opened, port = _desktop_launch(monkeypatch, tmp_path, [False, False, False, True])
    assert rc == 0 and opened == [f"http://127.0.0.1:{port}"]


def test_desktop_launch_refuses_a_second_server(monkeypatch, tmp_path, capsys):
    rc, opened, _ = _desktop_launch(monkeypatch, tmp_path, [True])
    assert rc == 1 and opened == [] and "already in use" in capsys.readouterr().err


@pytest.mark.parametrize("launch_result", [1, SystemExit("Missing --extract: pass it on the command line")])
def test_desktop_mode_holds_the_window_open_on_failure(monkeypatch, capsys, launch_result):
    import nmosd.cli as cli
    import nmosd.review.launch as launch_mod

    def fake_launch(**kwargs):
        assert kwargs["desktop"] is True
        if isinstance(launch_result, BaseException):
            raise launch_result
        return launch_result

    prompts: list[str] = []
    monkeypatch.setattr(launch_mod, "launch", fake_launch)
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")
    assert cli.main(["review", "--desktop", "--extract", "e", "--deid", "d", "--review-db", "r"]) == 1
    assert len(prompts) == 1 and "Press Enter" in prompts[0]


def test_plain_review_never_waits_for_enter(monkeypatch):
    import nmosd.cli as cli
    import nmosd.review.launch as launch_mod

    monkeypatch.setattr(launch_mod, "launch", lambda **kwargs: 1)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("only --desktop waits for Enter"))
    assert cli.main(["review", "--extract", "e", "--deid", "d", "--review-db", "r"]) == 1
