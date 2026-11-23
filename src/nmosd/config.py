"""Settings and committed configuration files.

`config/settings.yaml` is git-ignored and holds real (PHI-location) paths. Committed rule files
(doc types, boilerplate, sections, de-identification patterns, field specs) live under `config/`.
"""

from __future__ import annotations

import copy
import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
SETTINGS_ENV = "NMOSD_SETTINGS"


class ConfigError(RuntimeError):
    pass


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Settings:
    """Thin wrapper around the settings dict with path helpers."""

    def __init__(self, raw: dict, source: Path | None):
        self.raw = raw
        self.source = source

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, key: str) -> Path | None:
        """Resolve `paths.<key>`; relative paths are relative to the repo root."""
        value = self.get(f"paths.{key}")
        if not value:
            return None
        p = Path(os.path.expanduser(str(value)))
        if not p.is_absolute():
            p = (REPO_ROOT / p).resolve()
        return p


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings: explicit path > $NMOSD_SETTINGS > config/settings.yaml > example defaults.

    Values are merged over `settings.example.yaml`, so new keys always have a default.
    """
    example = CONFIG_DIR / "settings.example.yaml"
    base = yaml.safe_load(example.read_text(encoding="utf-8")) if example.exists() else {}
    candidate: Path | None = None
    if path:
        candidate = Path(path)
    elif os.environ.get(SETTINGS_ENV):
        candidate = Path(os.environ[SETTINGS_ENV])
    elif (CONFIG_DIR / "settings.yaml").exists():
        candidate = CONFIG_DIR / "settings.yaml"
    if candidate is None:
        return Settings(base or {}, None)
    if not candidate.exists():
        raise ConfigError(f"Settings file not found: {candidate}")
    override = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
    return Settings(_deep_merge(base or {}, override), candidate)


@lru_cache(maxsize=64)
def _load_rule_file(rel: str) -> Any:
    p = CONFIG_DIR / rel
    if not p.exists():
        raise ConfigError(f"Missing committed config file: config/{rel}")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def load_rules(rel: str) -> Any:
    """Load a committed config file (e.g. 'doc_types.yaml', 'deid/allowlist.yaml'). Returns a copy."""
    return copy.deepcopy(_load_rule_file(rel))


def _not_a_rule(p: Path) -> bool:
    """This computer's settings (config/settings*.yaml, one per condition), identifiers files, and the app's list of
    conditions (config/conditions.yaml): none of them changes what the pipeline does to the notes."""
    if p.relative_to(CONFIG_DIR).as_posix() == "conditions.yaml" or p.name.startswith("identifiers"):
        return True
    return p.name.startswith("settings") and p.name != "settings.example.yaml"


def rules_hash(rels: list[str] | None = None) -> str:
    """Stable hash over committed rule files (for manifests / run configs)."""
    h = hashlib.sha256()
    files = sorted(rels) if rels else sorted(
        str(p.relative_to(CONFIG_DIR)) for p in CONFIG_DIR.rglob("*.yaml") if not _not_a_rule(p)
    )
    for rel in files:
        p = CONFIG_DIR / rel
        if p.exists():
            h.update(rel.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]
