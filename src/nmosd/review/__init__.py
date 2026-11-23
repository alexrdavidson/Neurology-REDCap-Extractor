"""Review store and Streamlit app."""

from __future__ import annotations

import sys
import time
from pathlib import Path

LOADED_AT = time.time()  # when this server process imported the review code


def code_changed_since_start() -> bool:
    """True if any loaded nmosd module was edited after the running app imported it (restart needed)."""
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if name.startswith("nmosd") and f:
            try:
                if Path(f).stat().st_mtime > LOADED_AT:
                    return True
            except OSError:
                continue
    return False
