#!/usr/bin/env python3
"""Benchmark local Ollama models on synthetic notes. Same as `python -m nmosd benchmark`.

    python scripts/benchmark_ollama.py --models qwen3:8b,gemma3:12b --num-ctx 8192
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nmosd.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["benchmark", *sys.argv[1:]]))
