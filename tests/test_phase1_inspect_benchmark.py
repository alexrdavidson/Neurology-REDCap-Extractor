"""Phase 1: metadata-only inspector (all backends) and the Ollama benchmark (synthetic notes, fake server)."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import types

import pytest

from nmosd.cli import main
from nmosd.config import load_settings
from nmosd.extract.benchmark import BenchOutput, run_benchmark, synthetic_note_of
from nmosd.extract.ollama_client import OllamaClient, PromptTooLong, estimate_tokens
from nmosd.ingest import access_reader
from nmosd.ingest.access_reader import (
    BackendUnavailable,
    MdbToolsReader,
    PyodbcReader,
    SqliteReader,
    open_source,
)
from nmosd.ingest.inspect_source import inspect
from nmosd.safety import SafetyError


def _phi_values(seeded_phi):
    return {x["value"] for v in seeded_phi.values() for x in v if len(x["value"]) >= 4}


# --- inspector -------------------------------------------------------------------------------
def test_inspect_prints_metadata_only(synthetic_db, seeded_phi, synthetic_tables):
    buf = io.StringIO()
    summary = inspect(SqliteReader(synthetic_db), out=buf)
    out = buf.getvalue()
    assert "VisitNote" in out and "Report_Text" in out and "Truncation check" in out
    assert summary["tables"]["VisitNote"]["rows"] == len(synthetic_tables["VisitNote"])
    assert summary["tables"]["VisitNote"]["patients"] == 6
    for v in _phi_values(seeded_phi):
        assert v not in out, "inspector leaked a value"
    assert "Office Visit" not in out, "categories hidden by default"
    assert "neuroimmunology" not in out.lower()
    for r in synthetic_tables["Demographics"]:
        assert r["EMPI"] not in out


def test_inspect_show_categories(synthetic_db, capsys):
    rc = main(["inspect", "--source", str(synthetic_db), "--show-categories"])
    cap = capsys.readouterr()
    assert rc == 0
    assert "Office Visit" in cap.out
    assert "WARNING" in cap.err


def test_inspect_cli_no_values(synthetic_db, capsys, seeded_phi):
    assert main(["inspect", "--source", str(synthetic_db)]) == 0
    out = capsys.readouterr().out
    assert "CRLF 100%" in out and "[report_end] 100%" in out
    for v in _phi_values(seeded_phi):
        assert v not in out


# --- mdbtools backend via fake executables -----------------------------------------------------
FAKE_TOOL = """
import csv, sqlite3, sys, os
con = sqlite3.connect(os.environ["FAKE_MDB_SQLITE"])
tool, args = sys.argv[1], sys.argv[2:]
if tool == "mdb-tables":
    for (n,) in con.execute("select name from sqlite_master where type='table'"):
        print(n)
    sys.exit(0)
table = args[-1]  # mdb-export [-D fmt] file table
cur = con.execute('select * from "' + table.replace('"', '""') + '"')
w = csv.writer(sys.stdout, quoting=csv.QUOTE_MINIMAL, lineterminator="\\n")
w.writerow([d[0] for d in cur.description])
for r in cur:
    w.writerow(["" if v is None else v for v in r])
"""


@pytest.fixture
def fake_mdbtools(tmp_path, monkeypatch, synthetic_db):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = tmp_path / "fake_mdb.py"
    script.write_text(FAKE_TOOL, encoding="utf-8")
    for name in ("mdb-tables", "mdb-export"):
        p = bindir / name
        p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" {name} "$@"\n', encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_MDB_SQLITE", str(synthetic_db))
    fake_accdb = tmp_path / "fake.accdb"
    fake_accdb.write_bytes(b"not really access")
    return fake_accdb


@pytest.mark.skipif(sys.platform.startswith("win"), reason="fake POSIX executables")
def test_mdbtools_reader_preserves_multiline_crlf(fake_mdbtools, synthetic_tables):
    r = MdbToolsReader(fake_mdbtools)
    assert "VisitNote" in r.tables()
    assert r.columns("VisitNote")[-1] == "Report_Text"
    rows = list(r.rows("VisitNote"))
    assert len(rows) == len(synthetic_tables["VisitNote"])
    assert rows[0]["Report_Text"] == synthetic_tables["VisitNote"][0]["Report_Text"]
    assert "\r\n" in rows[0]["Report_Text"]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="fake POSIX executables")
def test_open_source_picks_mdbtools_for_accdb(fake_mdbtools, monkeypatch):
    monkeypatch.setattr(access_reader.sys, "platform", "darwin")
    assert open_source(fake_mdbtools).backend == "mdbtools"


def test_no_backend_gives_install_help(tmp_path, monkeypatch):
    f = tmp_path / "x.accdb"
    f.write_bytes(b"")
    monkeypatch.setattr(access_reader.shutil, "which", lambda _: None)
    monkeypatch.setitem(sys.modules, "pyodbc", None)
    with pytest.raises(BackendUnavailable) as e:
        open_source(f)
    assert "brew install mdbtools" in str(e.value) and "Access Database Engine" in str(e.value)


def test_pyodbc_reader_with_fake_driver(tmp_path, monkeypatch):
    class Cur:
        description = (("EMPI",), ("Report_Text",))

        def tables(self, tableType):
            return [types.SimpleNamespace(table_name="VisitNote")]

        def columns(self, table):
            return [types.SimpleNamespace(column_name=c) for c in ("EMPI", "Report_Text")]

        def execute(self, sql):
            assert sql == "SELECT * FROM [VisitNote]"
            self._rows = [("1", "a\r\nb[report_end]")]

        def fetchmany(self, n):
            out, self._rows = self._rows, []
            return out

    class Con:
        def cursor(self):
            return Cur()

        def close(self):
            pass

    fake = types.ModuleType("pyodbc")
    fake.drivers = lambda: [access_reader.ACCESS_DRIVER]
    fake.connect = lambda *a, **k: Con()
    monkeypatch.setitem(sys.modules, "pyodbc", fake)
    r = PyodbcReader(tmp_path / "x.accdb")
    assert r.tables() == ["VisitNote"]
    assert list(r.rows("VisitNote")) == [{"EMPI": "1", "Report_Text": "a\r\nb[report_end]"}]


# --- Ollama client safety + truncation -------------------------------------------------------------
class FakeOllama:
    def __init__(self, content=None, prompt_eval_count=None, fail_think=False, models=("qwen3:8b", "gemma3:12b")):
        self.content, self.pec, self.fail_think, self.calls, self._models = content, prompt_eval_count, fail_think, [], models

    def list(self):
        return {"models": [{"model": m, "digest": f"sha256:{i}"} for i, m in enumerate(self._models)]}

    def chat(self, **kw):
        self.calls.append(kw)
        if self.fail_think and "think" in kw:
            raise RuntimeError('"gemma3:12b" does not support thinking')
        prompt_tokens = self.pec if self.pec is not None else sum(estimate_tokens(m["content"]) for m in kw["messages"])
        content = self.content if self.content is not None else json.dumps(
            {"treatments": [{"drug": "rituximab", "event": "start", "event_date": "2018-04-16",
                             "quote": "First rituximab infusion"}]})
        return {"message": {"content": content}, "prompt_eval_count": prompt_tokens, "eval_count": 40,
                "prompt_eval_duration": 2_000_000_000, "eval_duration": 4_000_000_000,
                "load_duration": 1_000_000_000, "total_duration": 7_000_000_000}


def test_client_refuses_cloud_and_remote_hosts():
    with pytest.raises(SafetyError):
        OllamaClient(model="gpt-oss:120b-cloud", _client=FakeOllama())
    with pytest.raises(SafetyError):
        OllamaClient(host="http://10.0.0.5:11434", _client=FakeOllama())
    with pytest.raises(SafetyError):
        OllamaClient(host="https://ollama.com", _client=FakeOllama())
    with pytest.raises(SafetyError):
        OllamaClient(model="llama3:70b", allowed_models=["qwen3:8b"], _client=FakeOllama())


def test_client_sets_options_and_structured_format():
    fake = FakeOllama()
    c = OllamaClient(num_ctx=8192, _client=fake, backoff_s=0)
    res = c.generate_json("sys", "prompt", BenchOutput.model_json_schema())
    call = fake.calls[0]
    assert call["options"]["num_ctx"] == 8192 and call["options"]["temperature"] == 0
    assert call["think"] is False and call["format"]["type"] == "object"
    assert res.parsed["treatments"][0]["drug"] == "rituximab"
    assert res.digest == "sha256:0" and not res.truncation_suspected


def test_client_detects_silent_truncation():
    c = OllamaClient(num_ctx=4096, _client=FakeOllama(prompt_eval_count=4095), backoff_s=0)
    res = c.generate_json("sys", "x " * 2000, {"type": "object"})
    assert res.truncation_suspected and "num_ctx" in res.truncation_reason
    c2 = OllamaClient(num_ctx=8192, _client=FakeOllama(prompt_eval_count=50), backoff_s=0)
    res2 = c2.generate_json("sys", "word " * 3000, {"type": "object"})
    assert res2.truncation_suspected


def test_client_refuses_prompts_that_cannot_fit():
    c = OllamaClient(num_ctx=2048, _client=FakeOllama(), backoff_s=0)
    with pytest.raises(PromptTooLong):
        c.generate_json("sys", "word " * 4000, {"type": "object"})


def test_client_retries_without_think_and_on_bad_json():
    fake = FakeOllama(fail_think=True)
    c = OllamaClient(model="gemma3:12b", _client=fake, backoff_s=0)
    assert c.generate_json("s", "p", {"type": "object"}).parsed is not None
    bad = FakeOllama(content="not json")
    c2 = OllamaClient(_client=bad, retries=2, backoff_s=0)
    res = c2.generate_json("s", "p", {"type": "object"})
    assert res.parsed is None and len(bad.calls) == 3


class CutOffOllama(FakeOllama):
    """An answer that ran into num_predict: the JSON never closes."""

    def chat(self, **kw):
        out = super().chat(**kw)
        out.update(done_reason="length", eval_count=kw["options"]["num_predict"])
        return out


def test_client_does_not_repeat_a_cut_off_answer():
    """17.11.26: at temperature 0 a cut-off answer comes back the same, so it is returned at once (one call, not
    three) and the caller splits the excerpt."""
    fake = CutOffOllama(content='{"items": [{"name": "Migraine", "quote": "migr')
    res = OllamaClient(_client=fake, retries=2, backoff_s=0).generate_json("s", "p", {"type": "object"})
    assert res.parsed is None and len(fake.calls) == 1 and res.extra["cut_off"]


# --- benchmark -------------------------------------------------------------------------------------
def test_synthetic_note_sizes():
    for n in (500, 2000, 6000):
        t = estimate_tokens(synthetic_note_of(n))
        assert 0.6 * n <= t <= 1.1 * n


def test_benchmark_with_fake_server():
    settings = load_settings(os.environ["NMOSD_SETTINGS"])
    buf = io.StringIO()

    def factory(m):
        return OllamaClient(model=m, num_ctx=8192, _client=FakeOllama(), backoff_s=0)

    rows = run_benchmark(["qwen3:8b", "gemma3:12b"], 8192, [500, 2000, 6000], settings, out=buf, client_factory=factory)
    assert len(rows) == 6 and all(r.validated for r in rows)
    assert all(r.prompt_tps and r.gen_tps for r in rows)
    out = buf.getvalue()
    assert "| qwen3:8b | 6000 |" in out and "Valid JSON" in out


def test_benchmark_refuses_cloud_model():
    settings = load_settings(os.environ["NMOSD_SETTINGS"])
    with pytest.raises(SafetyError):
        run_benchmark(["deepseek-v3.1:671b-cloud"], 8192, [500], settings, out=io.StringIO(),
                      client_factory=lambda m: None)


def test_benchmark_reports_missing_model():
    settings = load_settings(os.environ["NMOSD_SETTINGS"])
    buf = io.StringIO()
    run_benchmark(["llama3.1:8b"], 8192, [500], settings, out=buf,
                  client_factory=lambda m: OllamaClient(model=m, _client=FakeOllama(), backoff_s=0))
    assert "ollama pull llama3.1:8b" in buf.getvalue()


def test_cli_help_lists_all_plan_commands(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for c in ["inspect", "stage", "deid", "leak-test", "deid-report", "extract", "review", "export", "reidentify",
              "evaluate", "benchmark", "select-pilot"]:
        assert c in out


def test_debug_options_need_phi_ack(tmp_path):
    with pytest.raises(SystemExit) as e:
        main(["deid", "--staging", str(tmp_path / "s.sqlite"), "--out", str(tmp_path / "o" / "d.sqlite"),
              "--linkage", str(tmp_path / "l" / "k.enc"), "--debug-doc", "1"])
    assert "--i-understand-this-prints-phi" in str(e.value)


