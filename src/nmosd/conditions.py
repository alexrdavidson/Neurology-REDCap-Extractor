"""The neurological conditions the app offers (config/conditions.yaml).

The app opens on a choice of condition. A condition is *ready* when its REDCap fields, diagnosis check and
extraction rules are built (today: NMOSD); the others are listed as not set up yet. Each ready condition has its
own settings file in config/, so each has its own private data folder and the work for two conditions never mixes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from nmosd.config import CONFIG_DIR, ConfigError

CONDITIONS_FILE = CONFIG_DIR / "conditions.yaml"
STATUSES = ("ready", "planned")
ID_RX = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
# Every name this allows is git-ignored (.gitignore: config/settings.yaml, config/settings.*.yaml).
SETTINGS_RX = re.compile(r"^settings(\.[a-z0-9_]+)?\.yaml$")


@dataclass(frozen=True)
class Condition:
    id: str
    name: str
    full_name: str
    group: str
    status: str
    settings: str            # file name in config/

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def load_conditions(path: Path = CONDITIONS_FILE) -> list[Condition]:
    """The conditions in file order. Raises ConfigError on anything the app could not show or use safely."""
    if not path.exists():
        raise ConfigError(f"Missing committed config file: {path.name}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = raw.get("conditions") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{path.name}: expected a non-empty 'conditions:' list")
    out: list[Condition] = []
    seen_ids: set[str] = set()
    seen_settings: set[str] = set()
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ConfigError(f"{path.name}: entry {i} is not a mapping")
        cid = str(item.get("id") or "")
        where = f"{path.name}: condition '{cid or i}'"
        if not ID_RX.match(cid) or cid == "example":
            raise ConfigError(f"{where}: id must be 2-40 lower-case letters, digits or _ (and not 'example')")
        if cid in seen_ids:
            raise ConfigError(f"{where}: listed twice")
        status = str(item.get("status") or "")
        if status not in STATUSES:
            raise ConfigError(f"{where}: status must be one of {', '.join(STATUSES)}")
        name = str(item.get("name") or "").strip()
        group = str(item.get("group") or "").strip()
        if not name or not group:
            raise ConfigError(f"{where}: needs a name and a group")
        settings = str(item.get("settings") or f"settings.{cid}.yaml")
        if not SETTINGS_RX.match(settings) or settings == "settings.example.yaml":
            raise ConfigError(f"{where}: settings must be a file name like settings.{cid}.yaml (kept out of git)")
        if status == "ready" and settings in seen_settings:
            raise ConfigError(f"{where}: shares its settings file with another condition")
        seen_ids.add(cid)
        if status == "ready":
            seen_settings.add(settings)
        out.append(Condition(cid, name, str(item.get("full_name") or "").strip(), group, status, settings))
    return out


def find(conditions: list[Condition], cid: str | None) -> Condition | None:
    return next((c for c in conditions if c.id == cid), None) if cid else None


def by_group(conditions: list[Condition]) -> dict[str, list[Condition]]:
    """Conditions under their group heading; groups in the order they first appear."""
    out: dict[str, list[Condition]] = {}
    for c in conditions:
        out.setdefault(c.group, []).append(c)
    return out


def settings_path(condition: Condition) -> Path:
    return CONFIG_DIR / condition.settings
