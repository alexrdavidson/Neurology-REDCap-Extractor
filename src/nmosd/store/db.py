"""SQLite helpers: one database file per stage, schemas from schema.sql sections."""

from __future__ import annotations

import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


@lru_cache(maxsize=1)
def _sections() -> dict[str, str]:
    text = SCHEMA_FILE.read_text(encoding="utf-8")
    parts = re.split(r"^-- @schema (\w+)\s*$", text, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def connect(path: Path | str, schema: str | None = None, readonly: bool = False) -> sqlite3.Connection:
    p = Path(path)
    if readonly:
        con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)  # percent-encoded; Windows-safe
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(p)
        con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    if schema and not readonly:
        con.executescript(_sections()[schema])
    return con


def set_meta(con: sqlite3.Connection, table: str, items: dict) -> None:
    con.executemany(f"INSERT OR REPLACE INTO {table}(key, value) VALUES (?, ?)",
                    [(k, v if isinstance(v, str) else json.dumps(v, sort_keys=True)) for k, v in items.items()])


def get_meta(con: sqlite3.Connection, table: str) -> dict:
    out = {}
    for r in con.execute(f"SELECT key, value FROM {table}"):
        try:
            out[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            out[r["key"]] = r["value"]
    return out
