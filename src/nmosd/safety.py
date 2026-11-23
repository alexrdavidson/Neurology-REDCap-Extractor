"""PHI guardrails shared by every command.

* PHI outputs must never be written inside the repository.
* The linkage key must live in a different directory from de-identified output.
* Runtime network access is limited to Ollama on the loopback interface.
* Content-printing debug options need an explicit acknowledgement flag.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from nmosd.config import REPO_ROOT

PHI_ACK_FLAG = "--i-understand-this-prints-phi"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class SafetyError(RuntimeError):
    pass


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def ensure_outside_repo(path: Path | str, purpose: str) -> Path:
    """Refuse PHI paths inside the repository. Returns the resolved path."""
    p = Path(path).expanduser().resolve()
    if _is_within(p, REPO_ROOT):
        raise SafetyError(
            f"Refusing to use {purpose} inside the repository ({REPO_ROOT.name}/...). "
            "PHI must live outside the repo; set a path under your PHI folder."
        )
    return p


def ensure_phi_output(path: Path | str, purpose: str) -> Path:
    p = ensure_outside_repo(path, purpose)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def ensure_separate_dirs(linkage: Path, deid_out: Path) -> None:
    """The linkage key must never sit next to (or inside the folder of) de-identified output."""
    ld = Path(linkage).expanduser().resolve().parent
    dd = Path(deid_out).expanduser().resolve().parent
    if ld == dd or _is_within(ld, dd) or _is_within(dd, ld):
        raise SafetyError(
            "The linkage key and the de-identified output must be in different, non-nested "
            f"directories (got linkage dir '{ld.name}' and output dir '{dd.name}')."
        )


def ensure_loopback(url: str) -> str:
    host = urlparse(url).hostname or ""
    if host not in LOOPBACK_HOSTS:
        raise SafetyError(f"Only a local Ollama on 127.0.0.1 is allowed; got host '{host}'.")
    return url


def ensure_local_model(model: str, allowed: list[str] | None = None) -> str:
    if "cloud" in model.lower():
        raise SafetyError(f"Cloud models are not allowed: '{model}'.")
    if allowed is not None and model not in allowed:
        raise SafetyError(f"Model '{model}' is not in llm.allowed_models: {allowed}")
    return model


def enforce_offline_hf() -> None:
    """Hugging Face libraries must never reach the network at runtime."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
