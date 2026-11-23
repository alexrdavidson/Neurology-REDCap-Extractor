"""Render aggregate evaluation metrics as Markdown (no identifiers, values or dates)."""

from __future__ import annotations


def _fmt(v) -> str:
    return "–" if v is None else (f"{v:.0%}" if isinstance(v, float) and v <= 1 else str(v))


def render_report(m: dict, label: str = "") -> str:
    lines = [f"# Evaluation{f' — {label}' if label else ''} ({m['records']} records, aggregate only)", ""]
    if m.get("patients"):
        pt = m["patients"]
        lines += [f"Patients: {pt['gold']} in the gold file, {pt['exported']} exported, {pt['compared']} compared. "
                  f"In the gold file but not exported (excluded at the diagnosis check, or not reviewed): "
                  f"{pt['gold_only']}. Exported but not in the gold file: {pt['export_only']}.", ""]
    lines += ["## Single-value fields", "", "| Field | Accuracy | n |", "|---|---|---|"]
    lines += [f"| {f} | {_fmt(v['accuracy'])} | {v['n']} |" for f, v in sorted(m["fields"].items())]
    lines += ["", "## Episodes", "", "| Instrument | Gold | Predicted | Matched | Precision | Recall | Records with exact count "
              "| Mean abs count error |", "|---|---|---|---|---|---|---|---|"]
    for inst, e in m["episodes"].items():
        lines.append(f"| {inst} | {e['gold']} | {e['predicted']} | {e['matched']} | {_fmt(e['precision'])} | "
                     f"{_fmt(e['recall'])} | {e['exact_count_records']}/{m['records']} | {e['mean_abs_count_error']} |")
    for inst, e in m["episodes"].items():
        if e["field_accuracy"]:
            lines += ["", f"**{inst} field accuracy (matched episodes)**: " +
                      ", ".join(f"{f} {_fmt(v)}" for f, v in e["field_accuracy"].items())]
    if m["dates"]:
        lines += ["", "## Date error (days; estimate code 3 compared by year)", "",
                  "| Field (estimate) | n | Median | Mean | Exact |", "|---|---|---|---|---|"]
        lines += [f"| {k} | {v['n']} | {v['median_days']} | {v['mean_days']} | {_fmt(v['exact_share'])} |"
                  for k, v in m["dates"].items()]
    if m["checkbox"]:
        lines += ["", "## Checkbox fields", "", "| Field | Precision | Recall |", "|---|---|---|"]
        lines += [f"| {k} | {_fmt(v['precision'])} | {_fmt(v['recall'])} |" for k, v in m["checkbox"].items()]
    if m.get("proposals"):
        p = m["proposals"]
        lines += ["", f"Proposals: {p['n']} · not established {_fmt(p['not_established_share'])} · "
                      f"conflict-flagged {_fmt(p['conflict_share'])}"]
    if m.get("review_outcomes"):
        lines += ["", "## How the reviewer treated the model's proposals", "",
                  "| Instrument | Accepted unchanged | Corrected | Unresolved | Share accepted | Episodes added "
                  "(missed by the model) | Episodes deleted (wrongly proposed) |", "|---|---|---|---|---|---|---|"]
        for inst, c in m["review_outcomes"].items():
            a, k, u = c.get("accept", 0), c.get("correct", 0), c.get("unresolved", 0)
            share = a / (a + k) if a + k else None
            lines.append(f"| {inst} | {a} | {k} | {u} | {_fmt(share)} | {c.get('episodes_added', 0)} | "
                         f"{c.get('episodes_deleted', 0)} |")
    if m.get("review_minutes_per_patient"):
        r = m["review_minutes_per_patient"]
        lines += [f"Review time per patient (active minutes): median {r['median']}, max {r['max']} ({r['patients']} patients)"]
    return "\n".join(lines)
