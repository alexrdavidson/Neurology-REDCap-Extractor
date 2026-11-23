"""Background jobs for the guided app.

A job is a list of steps, each `python <args>` (usually `-m nmosd <command>`), run one after the other by
`nmosd.app.runner` in its own process. The job carries on when the browser tab or the app window is closed, and
the app picks it up again when it is reopened.

Files per job, in the job folder (`<phi_dir>/app_jobs`, or the app's own `.app_state/jobs` for setup downloads):

* `<name>.json`       written by the app: title, steps, process id. Never a passphrase.
* `<name>.state.json` written by the runner: current step, exit codes, end time.
* `<name>.stop`       created by the app when the user presses Stop.
* `<name>.log`        what the commands print: the same metadata-only lines as in a terminal.

Only one job runs at a time: two heavy steps at once (de-identification and the local model) would starve each
other of memory.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from nmosd.config import REPO_ROOT

SETUP_JOB_DIR = REPO_ROOT / ".app_state" / "jobs"
PHI_JOB_DIR_NAME = "app_jobs"


class JobError(RuntimeError):
    pass


@dataclass
class Step:
    label: str
    args: list[str]               # arguments after the Python executable
    allow_fail: bool = False      # carry on with the next step even if this one fails


def nmosd(label: str, *args: str, allow_fail: bool = False) -> Step:
    return Step(label, ["-m", "nmosd", *args], allow_fail)


@dataclass
class Job:
    name: str
    title: str
    folder: Path
    steps: list[Step] = field(default_factory=list)
    pid: int | None = None
    create_time: float | None = None
    started: str | None = None
    # filled from the runner's state file
    state: str = "unknown"            # starting | running | done | failed | stopped | interrupted
    step: int | None = None
    rcs: list[int] = field(default_factory=list)
    rc: int | None = None
    ended: str | None = None

    @property
    def log(self) -> Path:
        return self.folder / f"{self.name}.log"

    @property
    def current_label(self) -> str:
        if self.step is not None and 0 <= self.step < len(self.steps):
            return self.steps[self.step].label
        return ""

    def step_rc(self, label: str) -> int | None:
        for i, s in enumerate(self.steps):
            if s.label == label and i < len(self.rcs):
                return self.rcs[i]
        return None


def phi_job_dir(settings) -> Path:
    from nmosd.safety import ensure_phi_output

    phi = settings.path("phi_dir")
    if phi is None:
        raise JobError("paths.phi_dir is not set: choose the private data folder first.")
    folder = ensure_phi_output(phi / PHI_JOB_DIR_NAME / "x", "the app's job folder").parent
    return folder


def _paths(folder: Path, name: str) -> tuple[Path, Path, Path]:
    return folder / f"{name}.json", folder / f"{name}.state.json", folder / f"{name}.stop"


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _alive(pid: int | None, create_time: float | None) -> bool:
    if not pid:
        return False
    import psutil

    try:
        p = psutil.Process(pid)
        if create_time is not None and abs(p.create_time() - create_time) > 1.0:
            return False                      # the id now belongs to another process
        return p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def read(folder: Path, name: str) -> Job | None:
    spec_path, state_path, stop_path = _paths(folder, name)
    spec = _read_json(spec_path)
    if spec is None:
        return None
    job = Job(name=name, title=spec.get("title", name), folder=folder,
              steps=[Step(**s) for s in spec.get("steps", [])], pid=spec.get("pid"),
              create_time=spec.get("create_time"), started=spec.get("started"))
    st = _read_json(state_path) or {}
    job.step, job.rcs, job.rc, job.ended = st.get("step"), st.get("rcs", []), st.get("rc"), st.get("ended")
    if job.rc is not None:
        job.state = "done" if job.rc == 0 else ("stopped" if stop_path.exists() else "failed")
    elif _alive(job.pid, job.create_time):
        job.state = "running" if st else "starting"
    else:
        job.state = "stopped" if stop_path.exists() else "interrupted"
    return job


def all_jobs(folders: list[Path]) -> list[Job]:
    out = []
    for folder in folders:
        if not folder.exists():
            continue
        for p in sorted(folder.glob("*.json")):
            if p.name.endswith(".state.json"):
                continue
            j = read(folder, p.stem)
            if j:
                out.append(j)
    return out


def running(folders: list[Path]) -> Job | None:
    return next((j for j in all_jobs(folders) if j.state in {"running", "starting"}), None)


def _detach_kwargs() -> dict:
    if os.name == "nt":
        # A hidden console of its own: no window pops up, and closing the app's window does not stop the job.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        return {"creationflags": flags}
    return {"start_new_session": True}


def start(folder: Path, name: str, title: str, steps: list[Step], settings_path: Path | None,
          env_extra: dict[str, str] | None = None, busy_folders: list[Path] | None = None) -> Job:
    """Start a job unless one is already running. `env_extra` (e.g. the passphrase) reaches the process only."""
    busy = running(busy_folders or [folder])
    if busy is not None:
        raise JobError(f"'{busy.title}' is still running. Wait for it to finish, or stop it first.")
    folder.mkdir(parents=True, exist_ok=True)
    spec_path, state_path, stop_path = _paths(folder, name)
    for p in (state_path, stop_path):
        p.unlink(missing_ok=True)
    spec = {"name": name, "title": title, "steps": [asdict(s) for s in steps],
            "started": datetime.now().isoformat(timespec="seconds")}
    _write_json(spec_path, spec)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    if settings_path is not None:
        env["NMOSD_SETTINGS"] = str(settings_path)
    env.update(env_extra or {})
    log = (folder / f"{name}.log").open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen([sys.executable, "-u", "-m", "nmosd.app.runner", str(spec_path)],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=REPO_ROOT,
                                env=env, **_detach_kwargs())
    finally:
        log.close()
    spec["pid"] = proc.pid
    try:
        import psutil

        spec["create_time"] = psutil.Process(proc.pid).create_time()
    except Exception:
        spec["create_time"] = None
    _write_json(spec_path, spec)
    return read(folder, name)  # type: ignore[return-value]


def stop(folder: Path, name: str, timeout: float = 10.0) -> None:
    """Stop a job and everything it started. Extraction carries on where it stopped when started again."""
    import psutil

    job = read(folder, name)
    _, _, stop_path = _paths(folder, name)
    stop_path.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    if job is None or not _alive(job.pid, job.create_time):
        return
    try:
        parent = psutil.Process(job.pid)
        procs = [*parent.children(recursive=True), parent]
    except psutil.NoSuchProcess:
        return
    for p in procs:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.terminate()
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.kill()


def forget(folder: Path, name: str) -> None:
    for p in (*_paths(folder, name), folder / f"{name}.log"):
        p.unlink(missing_ok=True)


def tail(job: Job, max_chars: int = 20000) -> str:
    """The end of the job's output. Progress bars rewrite their line with \\r: keep only the last version."""
    try:
        with job.log.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_chars))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [ln.rsplit("\r", 1)[-1] for ln in raw.replace("\r\n", "\n").split("\n")]
    return "\n".join(ln for ln in lines if not ln.startswith("W1") or "Redirects" not in ln)


def section(text: str, label: str) -> str:
    """The output of one step (between its `=== label ===` header and the next header)."""
    m = re.search(rf"^=== {re.escape(label)} ===\n(.*?)(?=^=== |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


_PROGRESS = [
    # extract: "[12/430] DOC-... treatment: 1 chunk(s) · ETA 01:02:03"
    re.compile(r"^\[(\d+)/(\d+)\].*?ETA (\d+):(\d\d):(\d\d)\s*$", re.M),
    # deid / leak-test: "Progress: 120/440 documents · ETA 00:41:10"
    re.compile(r"^Progress: (\d+)/(\d+) \w+.*?ETA (\d+):(\d\d):(\d\d)\s*$", re.M),
]
_PULL = re.compile(r"^Downloading .+?: (\d+)%$", re.M)


@dataclass
class Progress:
    done: int
    total: int
    eta_s: int | None

    @property
    def fraction(self) -> float:
        return min(1.0, self.done / self.total) if self.total else 0.0

    @property
    def eta_text(self) -> str:
        if self.eta_s is None:
            return ""
        h, m = divmod(self.eta_s // 60, 60)
        return f"about {h} h {m:02d} min left" if h else f"about {max(1, m)} min left"


def progress(text: str) -> Progress | None:
    """The latest progress line in a job's output, if any."""
    best: tuple[int, Progress] | None = None
    for rx in _PROGRESS:
        for m in rx.finditer(text):
            p = Progress(int(m.group(1)), int(m.group(2)),
                         int(m.group(3)) * 3600 + int(m.group(4)) * 60 + int(m.group(5)))
            if best is None or m.start() > best[0]:
                best = (m.start(), p)
    for m in _PULL.finditer(text):
        if best is None or m.start() > best[0]:
            best = (m.start(), Progress(int(m.group(1)), 100, None))
    return best[1] if best else None


def wait(folder: Path, name: str, timeout: float = 600.0, interval: float = 0.5) -> Job:
    """Block until the job has finished (tests and scripts)."""
    deadline = time.monotonic() + timeout
    while True:
        job = read(folder, name)
        if job is None:
            raise JobError(f"No job named {name}")
        if job.state not in {"running", "starting"}:
            return job
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {name} still running after {timeout} s")
        time.sleep(interval)
