"""scripts/demo_synthetic.py --prepare-only: a synthetic rehearsal that can never reach real data or settings."""

from __future__ import annotations

import importlib.util
import sys

import yaml

from tests.conftest import REPO


def _demo():
    spec = importlib.util.spec_from_file_location("demo_synthetic", REPO / "scripts" / "demo_synthetic.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_prepare_only_keeps_everything_inside_the_run_folder(tmp_path, monkeypatch, capsys):
    demo = _demo()
    root = tmp_path / "run"
    monkeypatch.setattr(sys, "argv", ["demo_synthetic.py", "--out", str(root), "--prepare-only",
                                      "--model", "gemma3:12b"])
    assert demo.main() == 0
    s = yaml.safe_load((root / "settings.yaml").read_text(encoding="utf-8"))
    for key, value in s["paths"].items():
        assert value is None or root.resolve() in (root / value).resolve().parents, key
    assert s["paths"]["redcap_template"] is None
    # never the real passphrase variable, so a shell left with it set cannot lock real data with it
    assert s["deid"]["passphrase_env"] == "NMOSD_SYNTHETIC_PASSPHRASE"
    steps = (root / "next_steps.txt").read_text(encoding="utf-8")
    assert "config/settings.yaml" not in steps and "NMOSD_LINKAGE_PASSPHRASE" not in steps
    commands = [ln.strip() for ln in steps.splitlines() if ln.strip().startswith("python -m nmosd")]
    assert len(commands) == 11 and all(c.startswith("python -m nmosd --settings settings.yaml ") for c in commands)
    # zsh does not treat a pasted "#" as a comment, so a note on a command line would become extra arguments
    assert not any("#" in c for c in commands)
    assert any("extract --model gemma3:12b" in c for c in commands)
    assert "Synthetic data and settings are ready" in capsys.readouterr().out


def test_prepare_only_refuses_a_folder_that_already_holds_a_run(tmp_path, monkeypatch):
    demo = _demo()
    (tmp_path / "deid").mkdir()
    monkeypatch.setattr(sys, "argv", ["demo_synthetic.py", "--out", str(tmp_path), "--prepare-only"])
    assert demo.main() == 1
    assert not (tmp_path / "settings.yaml").exists()
