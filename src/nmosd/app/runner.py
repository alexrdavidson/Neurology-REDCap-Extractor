"""Runs one guided-app job: its steps in order, each in its own Python process, output to the job's log.

    python -m nmosd.app.runner <job>.json

Started by `nmosd.app.jobs.start`, never by hand. Keeps the computer awake while it runs (not the screen).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def keep_awake() -> None:
    """Stop the computer from going to sleep while the job runs (the screen may still turn off)."""
    if os.name == "nt":
        try:
            import ctypes

            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)  # type: ignore[attr-defined]
        except Exception:
            pass
    elif sys.platform == "darwin" and shutil.which("caffeinate"):
        # ends by itself when this process ends
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write_state(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    spec_path = Path(argv[0])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    state_path = spec_path.with_name(spec_path.stem + ".state.json")
    keep_awake()
    rcs: list[int] = []
    overall = 0
    for i, step in enumerate(spec["steps"]):
        _write_state(state_path, {"step": i, "rcs": rcs})
        print(f"\n=== {step['label']} ===", flush=True)
        try:
            rc = subprocess.call([sys.executable, "-u", *step["args"]], stdin=subprocess.DEVNULL)
        except OSError as e:
            print(f"Could not start this step: {type(e).__name__}", flush=True)
            rc = 1
        rcs.append(rc)
        if rc != 0:
            overall = overall or rc
            if not step.get("allow_fail"):
                break
    _write_state(state_path, {"step": len(rcs) - 1, "rcs": rcs, "rc": overall,
                              "ended": datetime.now().isoformat(timespec="seconds")})
    print(f"\n=== Finished ({'OK' if overall == 0 else 'with errors'}) ===", flush=True)
    return overall


if __name__ == "__main__":
    raise SystemExit(main())
