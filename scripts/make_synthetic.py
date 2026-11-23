#!/usr/bin/env python3
"""Build the synthetic RPDR-like dataset (entirely fictional; no real data involved).

    python scripts/make_synthetic.py --out tests/fixtures/synthetic --seed 20261109 --csv

Outputs: synthetic_rpdr.sqlite (Access-equivalent tables), optional csv/, gold_redcap.csv,
record_map.csv, pilot_empis.txt, identifiers.yaml, seeded_phi.json, annotations.json, manifest.json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nmosd.synth.build import build


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "tests/fixtures/synthetic")
    ap.add_argument("--seed", type=int, default=20261109)
    ap.add_argument("--csv", action="store_true", help="also write one CSV per table")
    ap.add_argument("--extra-tables", action="store_true",
                    help="also emit Mrn/Con/Med/Lab tables (not in the real export) to test auto-pickup")
    ap.add_argument("--table-names", choices=["real", "rpdr"], default="real",
                    help="'real' = names in the confirmed export (VisitNote...); 'rpdr' = classic (Vis, Prg...)")
    a = ap.parse_args(argv)
    m = build(a.out, a.seed, a.csv, a.extra_tables, a.table_names)
    print(f"Synthetic dataset written to {a.out}")
    for k, v in m["tables"].items():
        print(f"  {k}: {v} rows")
    print(f"  content sha256: {m['content_sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
