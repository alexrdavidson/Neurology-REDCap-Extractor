"""`nmosd reidentify` (run in the approved environment): study ID → REDCap record_id, optional mrn/dob,
un-shift every date by its own precision, write the final import file. Prints counts only.

The final file keeps the export's layout exactly (column order, BOM, line endings, quoting), so an export
made with the REDCap template stays in the template's format."""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path
from typing import TextIO

from nmosd.deid.date_shift import Offsets, unshift_by_precision
from nmosd.deid.linkage import get_passphrase, load_linkage
from nmosd.redcap.csv_format import format_date, parse_date, sniff_style, write_csv
from nmosd.safety import ensure_phi_output

DATE_ESTIMATE = {"date_nmo_diagnosis": "date_nmo_estimate", "date_onset": "date_attack_estimate",
                 "date_starting_maint": "date_maintenance_estimate", "date_stopping_maint": "date_stopping_estimate",
                 "mog_assay_date": "date_mog_estimate", "date_admission": None, "date_discharge": None,
                 "aqp4_assay_date": None, "last_fu": None, "last_follow_up_date": None}
FINAL_NAME = "redcap_import_final.csv"


def read_record_map(path: Path | None) -> dict[str, dict]:
    if not path:
        return {}
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        return {r["empi"].strip(): r for r in csv.DictReader(f) if r.get("empi")}


def unshift_row(row: dict, off: Offsets, date_format: str = "YYYY-MM-DD") -> tuple[dict, int]:
    """Un-shift every date by its own precision. Dates are read as YYYY-MM-DD or in `date_format` and written
    back in `date_format`; missing-data codes (UNK, NA) are left alone."""
    out, n = dict(row), 0
    for f, est_field in DATE_ESTIMATE.items():
        d = parse_date(row.get(f), date_format)
        if d is None:
            continue
        est = row.get(est_field) if est_field else "1"
        out[f] = format_date(unshift_by_precision(d, off, est).isoformat(), date_format)
        n += 1
    return out, n


def run_reidentify(export_csv: Path, linkage: Path, out_dir: Path, record_map: Path | None = None,
                   staging: Path | None = None, settings=None, passphrase: str | None = None,
                   add_identifiers: bool = False, date_format: str | None = None,
                   stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    out = ensure_phi_output(Path(out_dir) / FINAL_NAME, "the re-identified REDCap import")
    date_format = date_format or (settings.get("export.date_format") if settings else None) or "YYYY-MM-DD"
    link = load_linkage(linkage, passphrase or get_passphrase(settings))
    rmap = read_record_map(record_map)
    style = sniff_style(export_csv)
    with Path(export_csv).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = list(reader.fieldnames or [])
        rows = list(reader)
    id_col = cols[0] if cols else "record_id"
    if id_col != "record_id":   # template projects may name the record ID field differently
        rows = [dict(r, record_id=r[id_col]) for r in rows]
    if add_identifiers:
        for c in ("mrn", "dob"):
            if c not in cols:
                cols.insert(1, c)
    final, counts, unmapped = [], Counter(), set()
    for r in rows:
        p = link["patients"].get(r["record_id"])
        if not p:
            unmapped.add(r["record_id"])
            continue
        rec = rmap.get(p["empi"], {})
        rid = rec.get("record_id")
        if not rid:
            unmapped.add(r["record_id"])
            continue
        new, n = unshift_row(r, Offsets(int(p["offset_days"])), date_format)
        counts["dates_unshifted"] += n
        new["record_id"] = new[id_col] = rid
        if add_identifiers and not r.get("redcap_repeat_instrument"):
            new["mrn"] = rec.get("mrn") or (p.get("mrns") or [""])[0]
            new["dob"] = format_date(p.get("dob"), date_format) or ""
        final.append(new)
        counts["rows"] += 1
    if unmapped:
        print(f"{len(unmapped)} study IDs have no REDCap record_id in the record map; nothing written.", file=stdout)
        return 2
    write_csv(out, cols, final, style)
    print(f"Re-identified {counts['rows']} rows for {len({r['record_id'] for r in final})} records; "
          f"{counts['dates_unshifted']} dates un-shifted. Import into the TEST REDCap project via the Data Import Tool.",
          file=stdout)
    return 0
