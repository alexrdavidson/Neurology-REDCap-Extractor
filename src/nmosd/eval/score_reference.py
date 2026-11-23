"""`nmosd score`: the latest proposals against reference answers (18.11.26), so a change to the tool can be judged in
minutes instead of by a review. Counts and field names only: no values, dates or IDs are printed.

The reference answers are files `Patient_A.json`, ... made outside the repository from a quality packet (BUILD_PLAN §0
rule 8): per instrument a list of instances, each with the fields the notes answer. A coded value is its REDCap code,
a checkbox a list of codes, a date a packet token (`D05`) with its precision, text a string. The packet key (PHI
location) turns "Patient A" into the study ID and each date token into its date.

Per instrument, reference instances and proposed instances are paired by date (and drug or antibody). Then every field:

* correct: both have it and they agree (dates within the tolerance of the precision the notes give);
* wrong: both have it and they differ;
* missed: the reference has a value, the proposal none (every field of a reference instance with no proposed partner);
* unsupported: the proposal has a value the reference does not (every field of a proposed instance with no partner);
* not comparable: the reference date cannot be read from the packet, or two free texts differ (not judged).

recall = correct / (correct + wrong + missed); precision = correct / (correct + wrong + unsupported).
Structured-only fields (sex, race, ...), calculated and descriptive fields are left out. A fast set (the few notes
that carry the answers) scores higher than the whole chart: use it to see whether a change helped, not as the
tool's accuracy.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TextIO

from nmosd.redcap.dictionary import Dictionary, default_dictionary_path, load_dictionary
from nmosd.redcap.schemas import STRUCTURED_ONLY
from nmosd.safety import ensure_outside_repo
from nmosd.store.db import connect

OUTCOMES = ["correct", "wrong", "missed", "unsupported", "not_comparable"]
SINGLE = ("demographics", "last_follow_up")
# the date that places an instance, and the fields that must agree for two instances to be the same one
ANCHOR = {"relapse": "date_onset", "non_relapse_hospitalization": "nr_admission", "imaging": "date_of_study",
          "treatment": "date_starting_maint", "cd1920_counts": "date_cd", "immunoglobulin_levels": "date_igg",
          "edss": "date_of_assessment", "ivig_for_hypogam": "date_starting_ivig",
          "infections_requiring_antibiotics": "infection_date", "last_follow_up": "last_follow_up_date"}
KEY_FIELD = {"treatment": "maintenance_therapy"}
WINDOW = {"exact": 31, "month": 62, "season": 120, "year": 366}     # days, for pairing instances
TOLERANCE = {"exact": 3, "month": 31, "season": 62, "year": 366}    # days, for a date to count as right
IGNORED = {"record_id", "mrn", "dob", "type_test"} | set(STRUCTURED_ONLY)
CONF_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Ref:
    kind: str                    # code | codes | date | text
    value: object                # str | frozenset[str] | date | None (a date the packet cannot give)
    precision: str = "exact"
    conf: str = "medium"


def load_reference(folder: Path) -> dict[str, dict]:
    """{"Patient A": the parsed file} for every Patient_*.json in `folder`."""
    out = {}
    for p in sorted(Path(folder).glob("Patient_*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        out[data.get("patient") or p.stem.replace("_", " ")] = data
    return out


def fast_docs(reference: dict[str, dict], key: dict) -> set[str]:
    """Document IDs of the reference's fast sets (notes and traps), through the packet key."""
    docs = set()
    for label, data in reference.items():
        notes = (key.get("patients", {}).get(label) or {}).get("notes", {})
        fs = data.get("fast_set") or {}
        for n in list(fs.get("notes", [])) + list(fs.get("traps", [])):
            if f"Note {n}" in notes:
                docs.add(notes[f"Note {n}"])
    return docs


def _iso(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def ref_field(raw: dict, dates: dict[str, str]) -> Ref:
    conf = raw.get("conf") if raw.get("conf") in CONF_RANK else "medium"
    if "date" in raw:
        d = raw["date"] or {}
        prec = d.get("precision", "exact")
        when = None
        if prec != "unusable":
            when = _iso(dates.get(str(d.get("token")))) if d.get("token") else None
            if when is None and prec == "year" and isinstance(d.get("year"), int):
                when = date(d["year"], 7, 1)
            # a token whose year is not the year the reference read: the key holds a mis-read date
            if when is not None and isinstance(d.get("year"), int) and when.year != d["year"]:
                when = None
        return Ref("date", when, prec if when is not None else "unusable", conf)
    v = raw.get("value")
    if isinstance(v, list):
        return Ref("codes", frozenset(str(c) for c in v), conf=conf)
    return Ref("text" if raw.get("kind") == "text" else "code", str(v).strip(), conf=conf)


def _num(s: str) -> float | None:
    m = re.fullmatch(r"\s*[<>]?=?\s*(\d+(?:\.\d+)?)\s*", str(s).replace(",", ""))
    return float(m.group(1)) if m else None


def _text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def compare(ref: Ref, proposed, is_checkbox: bool = False) -> str:
    """Outcome for one field that the reference has."""
    if proposed in (None, "", "[]", [], "UNK", "NA", "not_established"):
        return "missed"
    if ref.kind == "date":
        got = _iso(proposed)
        if ref.value is None:
            return "not_comparable"
        if got is None:
            return "wrong"
        if ref.precision == "year":
            return "correct" if got.year == ref.value.year else "wrong"
        if ref.precision == "month" and (got.year, got.month) == (ref.value.year, ref.value.month):
            return "correct"
        return "correct" if abs((got - ref.value).days) <= TOLERANCE.get(ref.precision, 3) else "wrong"
    if ref.kind == "codes" or is_checkbox:
        try:
            got = json.loads(proposed) if isinstance(proposed, str) and proposed.startswith("[") else proposed
        except json.JSONDecodeError:
            got = proposed
        got = frozenset(str(c) for c in (got if isinstance(got, (list, tuple, set, frozenset)) else [got]))
        want = ref.value if isinstance(ref.value, frozenset) else frozenset([str(ref.value)])
        return "correct" if got == want else "wrong"
    if ref.kind == "text":
        a, b = _num(ref.value), _num(proposed)
        if a is not None and b is not None:
            return "correct" if abs(a - b) <= max(0.5, 0.02 * max(abs(a), abs(b))) else "wrong"
        ta, tb = _text(ref.value), _text(proposed)
        return "correct" if ta and tb and (ta == tb or ta in tb or tb in ta) else "not_comparable"
    return "correct" if str(proposed).strip() == str(ref.value) else "wrong"


def scored_field(dic: Dictionary, name: str) -> bool:
    f = dic.get(name)
    return not (f is None or name in IGNORED or f.is_calc or f.is_descriptive or f.is_form_status
                or name.startswith("mrn_id"))


def _antibody(fields) -> str | None:
    names = set(fields)
    return "aqp4" if any(n.startswith("aqp4_") for n in names) else ("mog" if names & {
        "mog_assay_date", "mog_test_positive", "mog_assay_specimen", "mog_assay_location", "titer",
        "date_mog_estimate"} else None)


def _ref_anchor(instrument: str, fields: dict[str, Ref]) -> tuple[date | None, str, str | None]:
    if instrument == "antibody_lab_data":
        kind = _antibody(fields)
        r = fields.get(f"{kind}_assay_date") if kind else None
        return (r.value if r else None), (r.precision if r and r.value else "exact"), kind
    r = fields.get(ANCHOR.get(instrument, ""))
    k = fields.get(KEY_FIELD.get(instrument, ""))
    return (r.value if r else None), (r.precision if r and r.value else "exact"), (str(k.value) if k else None)


def _prop_anchor(instrument: str, fields: dict[str, str], anchor_date: str | None) -> tuple[date | None, str | None]:
    if instrument == "antibody_lab_data":
        kind = fields.get("type_test") or _antibody(fields)
        return _iso(fields.get(f"{kind}_assay_date")) if kind else None, kind
    return _iso(fields.get(ANCHOR.get(instrument, ""))) or _iso(anchor_date), fields.get(KEY_FIELD.get(instrument, ""))


def pair_instances(instrument: str, refs: list[dict[str, Ref]], props: list[tuple[dict[str, str], str | None]]
                   ) -> list[tuple[int | None, int | None]]:
    """[(reference index | None, proposal index | None)]: nearest dates first, within the window of the precision the
    reference gives; a drug or antibody type must agree. Left-overs of the same drug or antibody are then paired in
    date order (the same course with a different start date is one course with a wrong date, not two)."""
    if instrument in SINGLE:
        n = max(len(refs), len(props))
        return [(i if i < len(refs) else None, i if i < len(props) else None) for i in range(min(n, 1))] + \
               [(i, None) for i in range(1, len(refs))] + [(None, i) for i in range(1, len(props))]
    ra = [_ref_anchor(instrument, r) for r in refs]
    pa = [_prop_anchor(instrument, f, a) for f, a in props]
    cands = []
    for i, (rd, prec, rk) in enumerate(ra):
        for j, (pd, pk) in enumerate(pa):
            if rk is not None and pk is not None and str(rk) != str(pk):
                continue
            if rd is not None and pd is not None and abs((rd - pd).days) <= WINDOW.get(prec, 31):
                cands.append((abs((rd - pd).days), i, j))
    used_r, used_p, pairs = set(), set(), []
    for _gap, i, j in sorted(cands):
        if i not in used_r and j not in used_p:
            used_r.add(i)
            used_p.add(j)
            pairs.append((i, j))
    by_key: dict[str, tuple[list[int], list[int]]] = defaultdict(lambda: ([], []))
    for i, (_rd, _prec, rk) in enumerate(ra):
        if i not in used_r and rk is not None:
            by_key[str(rk)][0].append(i)
    for j, (_pd, pk) in enumerate(pa):
        if j not in used_p and pk is not None:
            by_key[str(pk)][1].append(j)
    for rs, ps in by_key.values():
        rs.sort(key=lambda i: ra[i][0] or date.max)
        ps.sort(key=lambda j: pa[j][0] or date.max)
        for i, j in zip(rs, ps, strict=False):
            used_r.add(i)
            used_p.add(j)
            pairs.append((i, j))
    pairs += [(i, None) for i in range(len(refs)) if i not in used_r]
    pairs += [(None, j) for j in range(len(props)) if j not in used_p]
    return sorted(pairs, key=lambda p: (p[0] is None, p[0] if p[0] is not None else p[1]))


def load_proposals(con, study_id: str, back: int = 0) -> dict[str, list[tuple[dict[str, str], str | None]]]:
    """The latest reconcile run (back=1: the one before it, ...): {instrument: [({field: value}, anchor date)]} in
    date order. Empty when there is no such run."""
    runs = [r[0] for r in con.execute("SELECT DISTINCT run_id FROM proposals ORDER BY run_id DESC")]
    if back >= len(runs):
        return {}
    run = runs[back]
    inst: dict[tuple[str, str], dict] = {}
    for r in con.execute("SELECT instrument, instance_key, field, value, anchor_date FROM proposals WHERE run_id=? AND "
                         "study_id=?", (run, study_id)):
        one = inst.setdefault((r["instrument"], r["instance_key"]), {"fields": {}, "anchor": r["anchor_date"]})
        if r["value"] not in (None, "", "[]"):
            one["fields"][r["field"]] = r["value"]
    out: dict[str, list] = defaultdict(list)
    for (instrument, _key), one in inst.items():
        out[instrument].append((one["fields"], one["anchor"]))
    for lst in out.values():
        lst.sort(key=lambda x: x[1] or "9999")
    return out


def score_patient(data: dict, proposals: dict, dates: dict[str, str], dic: Dictionary, min_conf: str = "low"
                  ) -> tuple[dict[str, Counter], list[tuple[str, int, str, str]]]:
    """({instrument: counts}, [(instrument, instance number, field, outcome)] for every field not correct)."""
    counts: dict[str, Counter] = defaultdict(Counter)
    detail: list[tuple[str, int, str, str]] = []
    for instrument in dic.forms:
        refs_raw = (data.get("instruments") or {}).get(instrument, [])
        refs = [{f: ref_field(v, dates) for f, v in (one.get("fields") or {}).items() if scored_field(dic, f)}
                for one in refs_raw]
        props = [({f: v for f, v in fields.items() if scored_field(dic, f)}, anchor)
                 for fields, anchor in proposals.get(instrument, [])]
        c = counts[instrument]
        optional = [bool(one.get("optional")) for one in refs_raw]
        c["reference_instances"] += sum(not o for o in optional)
        c["proposed_instances"] += len(props)
        for i, j in pair_instances(instrument, refs, props):
            ref = refs[i] if i is not None else {}
            got = props[j][0] if j is not None else {}
            if i is not None and j is None and optional[i]:
                continue                       # an optional reference instance nobody proposed: not a miss
            if i is not None and j is not None:
                c["matched_instances"] += 1
            elif i is not None:
                c["missed_instances"] += 1
            else:
                c["extra_instances"] += 1
            n = (i if i is not None else len(refs) + j) + 1
            for f, r in ref.items():
                out = compare(r, got.get(f), dic[f].is_checkbox)
                if out in ("wrong", "missed") and CONF_RANK[r.conf] < CONF_RANK[min_conf]:
                    out = "low_confidence"
                c[out] += 1
                if out != "correct":
                    detail.append((instrument, n, f, out))
            for f in got:
                if f not in ref:
                    c["unsupported"] += 1
                    detail.append((instrument, n, f, "unsupported"))
    return counts, detail


def _rate(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "–"


def run_score(extract: Path, reference: Path, key: Path, dictionary: Path | None = None, min_conf: str = "low",
              list_fields: bool = False, stdout: TextIO | None = None, back: int = 0) -> int:
    """back: 0 scores the latest Combine run, 1 the one before it (to compare before and after a change)."""
    stdout = stdout or sys.stdout
    reference = ensure_outside_repo(reference, "the reference answers")
    ref = load_reference(reference)
    if not ref:
        print(f"No Patient_*.json reference files in {reference}.", file=stdout)
        return 1
    if not Path(key).exists():
        print("The packet key was not found: give it with --key (quality_packet_key_<packet>.json, next to the "
              "de-identified database).", file=stdout)
        return 1
    keyd = json.loads(Path(key).read_text(encoding="utf-8"))
    dic = load_dictionary(dictionary or default_dictionary_path())
    con = connect(extract, readonly=True)
    n_runs = con.execute("SELECT COUNT(DISTINCT run_id) FROM proposals").fetchone()[0]
    if back >= n_runs:
        con.close()
        print(f"There are only {n_runs} Combine run(s) in this database: nothing {back} run(s) back.", file=stdout)
        return 1
    total: dict[str, Counter] = defaultdict(Counter)
    per_patient: dict[str, Counter] = {}
    details: list[tuple[str, str, int, str, str]] = []
    try:
        for label, data in ref.items():
            entry = keyd.get("patients", {}).get(label)
            if not entry:
                print(f"{label}: not in the packet key, left out.", file=stdout)
                continue
            counts, detail = score_patient(data, load_proposals(con, entry["study_id"], back),
                                           entry.get("dates", {}), dic, min_conf)
            per_patient[label] = Counter()
            for instrument, c in counts.items():
                total[instrument] += c
                per_patient[label] += c
            details += [(label, *d) for d in detail]
    finally:
        con.close()
    cols = ["reference_instances", "proposed_instances", "matched_instances", *OUTCOMES]
    head = ["Ref.", "Proposed", "Paired", "Correct", "Wrong", "Missed", "Unsupported", "Not comparable"]
    which = "the latest Combine run" if not back else f"the Combine run {back} before the latest (of {n_runs})"
    print(f"# Score against the reference answers (counts only): {which}\n", file=stdout)
    print(f"{len(per_patient)} patients. Fields the reference rates below '{min_conf}' confidence that were wrong or "
          f"missed are counted apart ({sum(c['low_confidence'] for c in total.values())}).\n", file=stdout)
    print("| Instrument | " + " | ".join(head) + " | Recall | Precision |", file=stdout)
    print("|---|" + "---|" * (len(head) + 2), file=stdout)

    def row(name: str, c: Counter) -> str:
        need = c["correct"] + c["wrong"] + c["missed"]
        given = c["correct"] + c["wrong"] + c["unsupported"]
        return (f"| {name} | " + " | ".join(str(c[k]) for k in cols)
                + f" | {_rate(c['correct'], need)} | {_rate(c['correct'], given)} |")

    grand = Counter()
    for instrument in dic.forms:
        c = total.get(instrument, Counter())
        grand += c
        print(row(instrument, c), file=stdout)
    print(row("**All**", grand), file=stdout)
    print("\n| Patient | " + " | ".join(head) + " | Recall | Precision |", file=stdout)
    print("|---|" + "---|" * (len(head) + 2), file=stdout)
    for label, c in per_patient.items():
        print(row(label, c), file=stdout)
    print("\nRecall: of the values the notes give, how many the tool got right. Precision: of the values the tool "
          "gave, how many were right. Instances: Ref. = in the reference, Proposed = by the tool, Paired = the same "
          "one in both.", file=stdout)
    if list_fields:
        print("\n## Fields not correct (no values shown)\n", file=stdout)
        for label, instrument, n, f, out in details:
            num = dic[f].number if f in dic else "?"
            print(f"- {label} · {instrument} #{n} · #{num} {f}: {out.replace('_', ' ')}", file=stdout)
    return 0


# ------------------------------------------------------------------------------------------
# where things are by default, and the fast test
# ------------------------------------------------------------------------------------------
REFERENCE_DIR = "Claude_reference"


def default_reference(base: Path) -> Path | None:
    """The newest packet_*/Claude_reference folder under the quality-review folder that holds reference files."""
    found = sorted(p for p in Path(base).expanduser().glob(f"packet_*/{REFERENCE_DIR}") if any(p.glob("Patient_*.json")))
    return found[-1] if found else None


def default_key(reference: Path, deid: Path) -> Path:
    """quality_packet_key_<packet>.json next to the de-identified database (where `nmosd quality-packet` wrote it)."""
    reference = Path(reference).expanduser()
    packet = reference.parent if reference.name == REFERENCE_DIR else reference
    return Path(deid).parent / f"quality_packet_key_{packet.name}.json"


def fast_db(extract: Path) -> Path:
    """The fast test's own results file, next to the extraction database: the full results are never touched."""
    extract = Path(extract)
    return extract.with_name(f"{extract.stem}_fast{extract.suffix}")


def run_fast_test(deid: Path, extract: Path, reference: Path, key: Path, settings, *, groups: list[str] | None = None,
                  llm: str = "ollama", model: str | None = None, annotations: Path | None = None,
                  dictionary: Path | None = None, min_conf: str = "low", list_fields: bool = False,
                  stdout: TextIO | None = None) -> int:
    """Extract → combine → disability notes → combine → score, on the reference's fast set only (the few notes that
    carry the answers, plus the notes that once misled the tool). Work already done with the same model, prompts and
    rules is not run again, so after a change to one field group only that group is asked."""
    import time

    from nmosd.extract.prefilter import second_pass_groups
    from nmosd.extract.run import run_extraction
    from nmosd.extract.stage2_reconcile import run_reconcile

    stdout = stdout or sys.stdout
    reference = ensure_outside_repo(reference, "the reference answers")
    ref = load_reference(reference)
    if not ref or not Path(key).exists():
        print("The reference answers (Patient_*.json) or the packet key were not found: give --reference and --key.",
              file=stdout)
        return 1
    docs = fast_docs(ref, json.loads(Path(key).read_text(encoding="utf-8")))
    if not docs:
        print("The reference files list no fast-set notes that the packet key knows.", file=stdout)
        return 1
    out = fast_db(extract)
    t0 = time.perf_counter()
    kw = dict(llm=llm, model=model, annotations=annotations, dictionary=dictionary, stdout=stdout, only_docs=docs)
    rc = run_extraction(deid, out, settings, groups=groups, **kw)
    if rc:
        return rc
    run_reconcile(out, deid, settings, stdout=stdout)
    if groups is None and second_pass_groups():
        rc = run_extraction(deid, out, settings, groups=second_pass_groups(), **kw)
        if rc:
            return rc
        run_reconcile(out, deid, settings, stdout=stdout)
    print(f"\nFast test: {len(docs)} notes, {(time.perf_counter() - t0) / 60:.1f} minutes.\n", file=stdout)
    return run_score(out, reference, key, dictionary, min_conf, list_fields, stdout)
