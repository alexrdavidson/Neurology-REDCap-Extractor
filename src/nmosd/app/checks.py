"""Is this computer ready? Checks for the guided app's first step. Nothing here reads patient data."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nmosd.safety import SafetyError, ensure_local_model, ensure_loopback

ACCESS_DRIVER_HINT = "Microsoft Access Driver (*.mdb, *.accdb)"
ACE_URL = "https://www.microsoft.com/en-us/download/details.aspx?id=54920"
OLLAMA_URL = "https://ollama.com/download"


@dataclass
class Check:
    key: str
    label: str
    ok: bool
    detail: str
    fix: str = ""


@dataclass
class OllamaInfo:
    reachable: bool
    models: list[str] = field(default_factory=list)
    error: str = ""


def ollama_info(settings, timeout: float = 3.0) -> OllamaInfo:
    try:
        import ollama

        host = ensure_loopback(settings.get("llm.host", "http://127.0.0.1:11434"))
        client = ollama.Client(host=host, timeout=timeout, trust_env=False, follow_redirects=False)
        resp = client.list()
        models = getattr(resp, "models", None) or (resp.get("models", []) if isinstance(resp, dict) else [])
        names = []
        for m in models:
            name = getattr(m, "model", None) or (m.get("model") or m.get("name") if isinstance(m, dict) else None)
            if name:
                names.append(name)
        return OllamaInfo(True, sorted(names))
    except SafetyError as e:
        return OllamaInfo(False, error=str(e))
    except Exception as e:
        return OllamaInfo(False, error=type(e).__name__)


def usable_models(settings, installed: list[str]) -> list[str]:
    """Pulled models that the settings allow (never a cloud model)."""
    allowed = settings.get("llm.allowed_models") or []
    out = []
    for m in allowed:
        try:
            ensure_local_model(m, allowed)
        except SafetyError:
            continue
        if m in installed:
            out.append(m)
    return out


def ner_models_missing(settings) -> list[str]:
    """Names of the de-identification models that are not in the local cache (primary nor fallback)."""
    cfg = settings.get("deid.ner") or {}
    if not cfg.get("models"):
        return []
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return [m["name"] for m in cfg["models"]]
    missing = []
    for m in cfg["models"]:
        found = False
        for repo, rev in ((m.get("hf_id"), m.get("revision")), (m.get("fallback_hf_id"), m.get("fallback_revision"))):
            if not repo:
                continue
            try:
                p = Path(snapshot_download(repo, revision=rev, local_files_only=True))
                if (p / "config.json").exists():
                    found = True
                    break
            except Exception:
                continue
        if not found:
            missing.append(m["name"])
    return missing


def access_reader(source: Path | None) -> tuple[bool, str, str]:
    """(ok, detail, fix) for reading the RPDR Access export on this computer."""
    if source is not None and source.suffix.lower() == ".sqlite":
        return True, "Test data (SQLite): no Access reader needed.", ""
    if sys.platform == "win32":
        try:
            import pyodbc  # type: ignore

            if any("accdb" in d.lower() for d in pyodbc.drivers()):
                return True, "Microsoft Access driver found.", ""
            return (False, "The Microsoft Access driver is not installed.",
                    f"Install the 'Microsoft Access Database Engine 2016 Redistributable' (64-bit) from {ACE_URL}. "
                    "Your IT team may need to approve it.")
        except ImportError:
            return False, "The pyodbc package is missing.", "Run 'Install Neurology REDCap Extractor' again."
    if shutil.which("mdb-export") and shutil.which("mdb-tables"):
        return True, "mdbtools found.", ""
    return False, "mdbtools is not installed.", "On a Mac: brew install mdbtools"


HEAVY = {"stage", "deid", "leak-test", "deid-report", "extract", "reconcile", "export", "reidentify"}


def pipeline_command(cmdline: list[str]) -> str | None:
    """The nmosd subcommand a process runs (`python -m nmosd extract ...`, `nmosd extract ...`), else None."""
    for i, tok in enumerate(cmdline):
        name = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if name in {"nmosd", "nmosd.exe"} and (i == 0 or cmdline[i - 1] == "-m" or "/" in tok.replace("\\", "/")):
            rest, skip = cmdline[i + 1:], False
            for t in rest:
                if skip:
                    skip = False
                    continue
                if t == "--settings":
                    skip = True
                    continue
                if not t.startswith("-"):
                    return t
            return None
    return None


def outside_runs() -> list[str]:
    """Heavy pipeline commands running outside the app (e.g. in a terminal); the app's own jobs are not listed."""
    try:
        import psutil
    except ImportError:
        return []
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = p.info.get("cmdline") or []
            sub = pipeline_command(cmd)
            if sub not in HEAVY or "--dry-run" in cmd:
                continue
            if any("nmosd.app.runner" in " ".join(a.cmdline()) for a in p.parents()):
                continue
            out.append(sub)
        except (psutil.Error, OSError):
            continue
    return sorted(set(out))


def memory_gb() -> float | None:
    try:
        import psutil

        return psutil.virtual_memory().total / 1024 ** 3
    except Exception:
        return None


def run_checks(settings, ollama: OllamaInfo | None = None) -> list[Check]:
    out = [Check("python", "Python", sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}",
                 "Python 3.11 or newer is needed: run the installer again.")]
    ollama = ollama or ollama_info(settings)
    model = settings.get("llm.model", "qwen3:8b")
    backend = settings.get("llm.backend", "ollama")
    if backend == "synthetic-oracle":
        out.append(Check("ollama", "Local AI (Ollama)", True, "Test mode: a built-in stand-in answers instead of a model."))
    elif not ollama.reachable:
        out.append(Check("ollama", "Local AI (Ollama)", False, "Ollama is not running on this computer.",
                         "Open the Ollama app (Windows: Start menu → Ollama; Mac: Applications → Ollama). If it is "
                         f"not installed, run the installer again or get it from {OLLAMA_URL}."))
    else:
        out.append(Check("ollama", "Local AI (Ollama)", True, "Ollama is running on this computer (127.0.0.1)."))
        has = model in ollama.models
        out.append(Check("model", f"AI model {model}", has,
                         "Downloaded." if has else "Not downloaded yet.",
                         "" if has else "Use the button below (about 5 GB, needs internet once)."))
    ner_enabled = (settings.get("deid.ner") or {}).get("enabled", True)
    if not settings.get("deid.enabled", True):
        out.append(Check("ner", "De-identification models", True,
                         "Not needed: the notes are kept as they are (de-identification is switched off)."))
    elif ner_enabled:
        missing = ner_models_missing(settings)
        out.append(Check("ner", "De-identification models", not missing,
                         "Downloaded." if not missing else f"Missing: {', '.join(missing)}.",
                         "" if not missing else "Use the button below (about 1 GB, needs internet once)."))
    else:
        out.append(Check("ner", "De-identification models", True,
                         "Switched off in the settings (test data only: never for real notes)."))
    ok, detail, fix = access_reader(settings.path("source"))
    out.append(Check("access", "Reading the RPDR export", ok, detail, fix))
    gb = memory_gb()
    if gb is not None:
        out.append(Check("memory", "Memory", gb >= 15, f"{gb:.0f} GB",
                         "" if gb >= 15 else "16 GB or more is recommended for the local AI model."))
    return out
