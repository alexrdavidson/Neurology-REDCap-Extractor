"""One interface over the RPDR Access export, with three backends.

* `pyodbc`   — Windows, via the Microsoft Access Database Engine ODBC driver.
* `mdbtools` — macOS/Linux, via the `mdb-tables` / `mdb-export` command-line tools.
* `sqlite`   — the synthetic Access-equivalent dataset (and any SQLite copy).

All backends yield rows as dict[str, str] with '' for NULL, preserving text byte-for-byte
(multi-line memo fields included).
"""

from __future__ import annotations

import csv
import io
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

csv.field_size_limit(sys.maxsize)

ACCESS_DRIVER = "Microsoft Access Driver (*.mdb, *.accdb)"
INSTALL_HELP = (
    "No backend can read Access files on this machine.\n"
    "  Windows: install the 'Microsoft Access Database Engine 2016 Redistributable' (64-bit if your Python is\n"
    "           64-bit; IT may need to approve it under App Control), then: pip install pyodbc\n"
    "  macOS:   brew install mdbtools\n"
    "  Linux:   sudo apt install mdbtools"
)


class BackendUnavailable(RuntimeError):
    pass


class AccessReader(Protocol):
    backend: str

    def tables(self) -> list[str]: ...

    def columns(self, table: str) -> list[str]: ...

    def rows(self, table: str) -> Iterator[dict[str, str]]: ...

    def close(self) -> None: ...


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class SqliteReader:
    backend = "sqlite"

    def __init__(self, path: Path):
        self.con = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)

    def tables(self) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    def columns(self, table: str) -> list[str]:
        return [r[1] for r in self.con.execute(f"PRAGMA table_info({qident(table)})")]

    def rows(self, table: str) -> Iterator[dict[str, str]]:
        cur = self.con.execute(f"SELECT * FROM {qident(table)}")
        cols = [d[0] for d in cur.description]
        for r in cur:
            yield {c: _s(v) for c, v in zip(cols, r, strict=True)}

    def close(self) -> None:
        self.con.close()


class MdbToolsReader:
    backend = "mdbtools"

    def __init__(self, path: Path):
        if not (shutil.which("mdb-tables") and shutil.which("mdb-export")):
            raise BackendUnavailable(INSTALL_HELP)
        self.path = Path(path)

    def tables(self) -> list[str]:
        out = subprocess.run(["mdb-tables", "-1", str(self.path)], capture_output=True, text=True, check=True).stdout
        return [t.strip() for t in out.splitlines() if t.strip()]

    def _export(self, table: str) -> Iterator[list[str]]:
        proc = subprocess.Popen(["mdb-export", "-D", "%Y-%m-%d %H:%M:%S", str(self.path), table],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        completed = False
        try:
            text = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace", newline="")
            yield from csv.reader(text)
            completed = True
        finally:
            if not completed:  # caller stopped early (e.g. header only): stop the export quietly
                proc.terminate()
            proc.stdout.close()
            proc.wait()
            if completed and proc.returncode != 0:
                err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
                # never echo values: mdb-export errors are about structure, but cap length anyway
                raise RuntimeError(f"mdb-export failed for table '{table}' (exit {proc.returncode}): {err[:120]}")
            if proc.stderr:
                proc.stderr.close()

    def columns(self, table: str) -> list[str]:
        it = self._export(table)
        try:
            return next(it)
        except StopIteration:
            return []
        finally:
            it.close()

    def rows(self, table: str) -> Iterator[dict[str, str]]:
        it = self._export(table)
        try:
            header = next(it)
        except StopIteration:
            return
        for r in it:
            yield {h: (r[i] if i < len(r) else "") for i, h in enumerate(header)}

    def close(self) -> None:
        pass


class PyodbcReader:
    backend = "pyodbc"

    def __init__(self, path: Path):
        try:
            import pyodbc  # type: ignore
        except ImportError as e:  # pragma: no cover - environment dependent
            raise BackendUnavailable(INSTALL_HELP) from e
        if ACCESS_DRIVER not in pyodbc.drivers():
            raise BackendUnavailable(INSTALL_HELP)
        self.con = pyodbc.connect(f"DRIVER={{{ACCESS_DRIVER}}};DBQ={Path(path).resolve()};", readonly=True)

    def tables(self) -> list[str]:
        cur = self.con.cursor()
        return sorted(r.table_name for r in cur.tables(tableType="TABLE"))

    def columns(self, table: str) -> list[str]:
        cur = self.con.cursor()
        return [r.column_name for r in cur.columns(table=table)]

    def rows(self, table: str) -> Iterator[dict[str, str]]:
        cur = self.con.cursor()
        cur.execute(f"SELECT * FROM [{table.replace(']', ']]')}]")
        cols = [d[0] for d in cur.description]
        while True:
            batch = cur.fetchmany(500)
            if not batch:
                break
            for r in batch:
                yield {c: _s(v) for c, v in zip(cols, r, strict=True)}

    def close(self) -> None:
        self.con.close()


def available_backends() -> list[str]:
    found = ["sqlite"]
    try:
        import pyodbc  # type: ignore

        if ACCESS_DRIVER in pyodbc.drivers():
            found.append("pyodbc")
    except ImportError:
        pass
    if shutil.which("mdb-tables") and shutil.which("mdb-export"):
        found.append("mdbtools")
    return found


def open_source(path: str | Path, backend: str = "auto") -> AccessReader:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Source not found: {p.name}")
    if backend == "auto":
        if p.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
            backend = "sqlite"
        else:
            have = available_backends()
            if sys.platform.startswith("win") and "pyodbc" in have:
                backend = "pyodbc"
            elif "mdbtools" in have:
                backend = "mdbtools"
            elif "pyodbc" in have:
                backend = "pyodbc"
            else:
                raise BackendUnavailable(INSTALL_HELP)
    return {"sqlite": SqliteReader, "mdbtools": MdbToolsReader, "pyodbc": PyodbcReader}[backend](p)
