"""`nmosd benchmark`: local Ollama throughput on SYNTHETIC notes of ~500 / 2,000 / 6,000 tokens.

Reports prompt-eval tokens/s, generation tokens/s, wall time, model load time, peak RAM of the
Ollama processes, whether the structured output validated, and truncation warnings.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Literal, TextIO

from pydantic import BaseModel, ValidationError

from nmosd.extract.ollama_client import LLMClient, OllamaClient, estimate_tokens
from nmosd.safety import ensure_local_model


class BenchTreatment(BaseModel):
    drug: Literal["rituximab", "mycophenolate", "eculizumab", "ravulizumab", "inebilizumab", "satralizumab",
                  "azathioprine", "tocilizumab", "other", "not_established"]
    event: Literal["start", "stop", "ongoing", "planned", "not_established"]
    event_date: str
    quote: str


class BenchOutput(BaseModel):
    treatments: list[BenchTreatment]


SYSTEM = ("You extract maintenance-therapy events for NMOSD from a clinical note. Return JSON only. "
          "A plan or prescription is NOT a start. Use YYYY-MM-DD or 'not_established' for dates. "
          "Quote the supporting text verbatim.")


def synthetic_note_of(tokens: int, seed: int = 20261109) -> str:
    """Concatenate synthetic visit notes until ~`tokens` tokens (synthetic data only)."""
    from nmosd.synth.build import build_dataset

    ds = build_dataset(seed)
    notes = [n.text() for n in ds.notes if n.table == "VisitNote" and n.description == "Office Visit"]
    out, i = [], 0
    while estimate_tokens("\r\n".join(out)) < tokens:
        out.append(notes[i % len(notes)])
        i += 1
    text = "\r\n".join(out)
    target_chars = int(tokens * 3.6)
    cut = text.rfind("\r\n\r\n", 0, target_chars)
    return text[: cut if cut > target_chars // 2 else target_chars]


class PeakRSS:
    """Samples the summed RSS of Ollama processes while a call runs."""

    def __init__(self, interval: float = 0.2):
        self.interval, self.peak, self._stop = interval, 0, threading.Event()
        try:
            import psutil  # noqa: F401

            self.available = True
        except ImportError:
            self.available = False

    def _sample(self) -> int:
        import psutil

        total = 0
        for p in psutil.process_iter(["name"]):
            try:
                if "ollama" in (p.info["name"] or "").lower():
                    total += p.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total

    def __enter__(self):
        if self.available:
            self._t = threading.Thread(target=self._run, daemon=True)
            self._t.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, self._sample())
            time.sleep(self.interval)

    def __exit__(self, *exc):
        self._stop.set()
        if self.available:
            self._t.join(timeout=2)


@dataclass
class BenchRow:
    model: str
    size: int
    prompt_tokens: int | None
    prompt_tps: float | None
    gen_tokens: int | None
    gen_tps: float | None
    wall_s: float
    load_s: float | None
    peak_ram_gb: float | None
    validated: bool
    truncation: str | None
    error: str | None


def _tps(count, ns) -> float | None:
    return round(count / (ns / 1e9), 1) if count and ns else None


def run_benchmark(models: list[str], num_ctx: int, sizes: list[int], settings, out: TextIO | None = None,
                  client_factory=None) -> list[BenchRow]:
    out = out or sys.stdout
    rows: list[BenchRow] = []
    schema = BenchOutput.model_json_schema()
    for model in models:
        ensure_local_model(model)
        client: LLMClient = (client_factory or (lambda m: OllamaClient.from_settings(
            settings, model=m, num_ctx=num_ctx, allowed_models=None)))(model)
        if isinstance(client, OllamaClient):
            try:
                installed = client.installed_models()
            except Exception as e:
                print(f"Cannot reach Ollama at {client.host}: {type(e).__name__}. Is `ollama serve` running?", file=out)
                return rows
            if model not in installed:
                print(f"- {model}: not pulled. Run: ollama pull {model}", file=out)
                continue
        for size in sizes:
            note = synthetic_note_of(size)
            prompt = f"DOCUMENT DATE: 2026-01-01\nNOTE:\n{note}\n\nReturn the treatments JSON."
            with PeakRSS() as mem:
                res = client.generate_json(SYSTEM, prompt, schema)
            try:
                ok = res.parsed is not None and BenchOutput.model_validate(res.parsed) is not None
            except ValidationError:
                ok = False
            rows.append(BenchRow(model, size, res.prompt_eval_count, _tps(res.prompt_eval_count, res.prompt_eval_duration_ns),
                                 res.eval_count, _tps(res.eval_count, res.eval_duration_ns), round(res.wall_s, 1),
                                 round(res.load_duration_ns / 1e9, 1) if res.load_duration_ns else None,
                                 round(mem.peak / 2**30, 2) if mem.peak else None, ok, res.truncation_reason, res.error))
    print_table(rows, num_ctx, out)
    return rows


def print_table(rows: list[BenchRow], num_ctx: int, out: TextIO) -> None:
    print(f"\n# Ollama benchmark (synthetic notes, num_ctx={num_ctx})\n", file=out)
    print("| Model | ~Tokens | Prompt tok | Prompt tok/s | Gen tok | Gen tok/s | Wall s | Load s | Peak RAM GB "
          "| Valid JSON | Truncation | Error |", file=out)
    print("|---|---|---|---|---|---|---|---|---|---|---|---|", file=out)
    for r in rows:
        print(f"| {r.model} | {r.size} | {r.prompt_tokens or '–'} | {r.prompt_tps or '–'} | {r.gen_tokens or '–'} | "
              f"{r.gen_tps or '–'} | {r.wall_s} | {r.load_s or '–'} | {r.peak_ram_gb or '–'} | "
              f"{'yes' if r.validated else 'NO'} | {r.truncation or '–'} | {r.error or '–'} |", file=out)
