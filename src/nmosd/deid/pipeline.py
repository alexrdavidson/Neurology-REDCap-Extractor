"""`nmosd deid`: de-identify the staging DB into a de-identified SQLite plus an encrypted linkage key.

De-identification is optional (13.11.26): with `deid.enabled: false` or `--keep-identifiers` the notes are
copied as they are (original text, real dates) into the same database layout under study IDs, so the later
steps work unchanged; the manifest records `deidentified: false`. A patient never silently switches between
the two ways (`--switch-mode`), because earlier work and exports carry the other dates.

Order per document: header block (parsed, harvested, replaced by a neutral header) → known identifiers
→ pattern layer → NER ensemble (union, allow-list) → date shifting (look-alikes protected) →
overlap resolution → surrogate tokens. Structured tables: identifier columns dropped, dates shifted,
free text through the same layers. Prints counts by category only.
"""

from __future__ import annotations

import json
import platform
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TextIO

from nmosd import __version__
from nmosd.config import load_rules, rules_hash
from nmosd.deid.date_shift import Offsets, find_dates, render_shifted
from nmosd.deid.header import HeaderResult, neutral_header, parse_header
from nmosd.deid.known_identifiers import (
    STAFF_SIGNOFF_RX,
    Hit,
    IdentifierSet,
    KnownMatcher,
    from_harvest,
    load_identifiers_yaml,
)
from nmosd.deid.linkage import empty_linkage, get_passphrase, load_linkage, save_linkage
from nmosd.deid.ner_models import Ensemble, NERUnavailable, build_ensemble
from nmosd.deid.patterns import PatternLayer
from nmosd.deid.study_ids import draw_offset_days, new_doc_id, new_study_id
from nmosd.deid.surrogates import SurrogateMap
from nmosd.ingest.column_map import IDENTIFIER_COLUMNS
from nmosd.safety import ensure_outside_repo, ensure_phi_output, ensure_separate_dirs
from nmosd.store.db import connect, set_meta
from nmosd.textproc.dates import parse_datetime

PRIORITY = {"known": 0, "pattern": 1, "ner": 2}
ALLOWLIST_CHECKED = {"STAFF", "RELATIVE", "HOSPITAL", "ORGANIZATION", "PATIENT", "LOCATION", "OTHER"}
MONTH_OR_DAY_RX = re.compile(r"(?i)\d|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|monday|tuesday|wednesday|"
                             r"thursday|friday|saturday|sunday")


@lru_cache(maxsize=1)
def allowlist() -> frozenset[str]:
    cfg = load_rules("deid/allowlist.yaml")
    return frozenset(t.lower() for group in cfg.values() for t in group)


def report_progress(stdout: TextIO, done: int, total: int, t_start: float, what: str) -> None:
    """A counts-only progress line about every 1% (the guided app shows it as a progress bar)."""
    step = max(1, total // 100)
    if total <= 0 or (done % step and done != total):
        return
    el = time.perf_counter() - t_start
    eta = int(el / done * (total - done)) if done else 0
    print(f"Progress: {done}/{total} {what} · ETA {eta // 3600:02d}:{eta % 3600 // 60:02d}:{eta % 60:02d}",
          file=stdout, flush=True)


def is_allowlisted(span_text: str) -> bool:
    terms = allowlist()
    t = re.sub(r"\s+", " ", span_text).strip(" .,;:()").lower()
    if t in terms:
        return True
    words = [w for w in re.split(r"[\s/,;:()]+", t) if w]
    return bool(words) and all(w.strip(".'’") in terms or re.sub(r"'s$", "", w) in terms for w in words)


@dataclass
class PatientCtx:
    empi: str
    study_id: str
    offsets: Offsets
    ids: IdentifierSet
    matcher: KnownMatcher
    surrogates: SurrogateMap


@dataclass
class DocResult:
    text: str
    spans: list[tuple[int, int, str, str, str]]
    counts: Counter = field(default_factory=Counter)
    flags: Counter = field(default_factory=Counter)
    header: HeaderResult | None = None
    ner_per_model: dict[str, int] = field(default_factory=dict)
    ner_union: int = 0


def _cluster(hits: list[Hit]) -> list[tuple[int, int, Hit]]:
    """Merge overlapping identifier hits into clusters; the representative hit decides the category."""
    hits = sorted(hits, key=lambda h: (h.start, -h.end))
    clusters: list[list[Hit]] = []
    for h in hits:
        if clusters and h.start < max(x.end for x in clusters[-1]):
            clusters[-1].append(h)
        else:
            clusters.append([h])
    out = []
    for c in clusters:
        rep = min(c, key=lambda h: (PRIORITY.get(h.source, 3), -(h.end - h.start)))
        out.append((min(h.start for h in c), max(h.end for h in c), rep))
    return out


def deidentify_text(text: str, pctx: PatientCtx, patterns: PatternLayer, ensemble: Ensemble | None,
                    table: str | None = None, note_type: str = "", site: str = "",
                    report_dt: datetime | None = None, tokenise_hospitals: bool = True) -> DocResult:
    res = DocResult(text="", spans=[])
    out_prefix, body_start = "", 0
    if table:
        h = parse_header(text, table)
        res.header = h
        if h.replace:
            shifted = (report_dt + timedelta(days=pctx.offsets.days)).date().isoformat() if report_dt else None
            out_prefix = text[:h.start] + neutral_header(note_type, site, shifted)
            body_start = h.end
            res.spans.append((h.start, h.end, "HEADER", "header", "[neutral header]"))
        elif h.banner:
            out_prefix = text[:h.banner[0]]
            body_start = h.banner[1]
            res.spans.append((h.banner[0], h.banner[1], "BANNER", "header", ""))
        if not h.matched:
            res.flags["header_unmatched"] += 1
    # --- identifier layers
    hits = pctx.matcher.find(text, body_start) + patterns.find(text, body_start)
    ner_dates: list[tuple[int, int]] = []
    if ensemble is not None:
        er = ensemble.predict(text[body_start:])
        res.ner_per_model, res.ner_union = er.per_model, er.union
        for sp in er.spans:
            s, e = sp.start + body_start, sp.end + body_start
            if sp.label == "DATE":
                ner_dates.append((s, e))
            elif sp.label == "AGE":
                m = re.search(r"\d+", text[s:e])
                if m and int(m.group()) >= 90:
                    hits.append(Hit(s + m.start(), s + m.end(), "AGE", "age90", "ner"))
            elif sp.label == "HOSPITAL" and not tokenise_hospitals:
                continue
            else:
                hits.append(Hit(s, e, sp.label, "ner", "ner"))
    kept: list[Hit] = []
    for h in hits:
        if h.source != "known" and h.category in ALLOWLIST_CHECKED and is_allowlisted(text[h.start:h.end]):
            res.counts["allowlist_override"] += 1
            continue
        if h.source == "known" and h.category in {"PATIENT", "RELATIVE", "TOWN"} and is_allowlisted(text[h.start:h.end]):
            res.flags["known_identifier_is_allowlisted_term"] += 1
        kept.append(h)
    clusters = _cluster(kept)
    # --- dates
    pivot = report_dt.year if report_dt else None
    mentions, skips = find_dates(text[body_start:], pivot)
    for m in mentions:
        m.start += body_start
        m.end += body_start
    for s, e in ner_dates:
        if any(not (e <= m.start or s >= m.end) for m in mentions):
            continue
        sub, _ = find_dates(text[s:e], pivot)
        if sub:
            for m in sub:
                m.start += s
                m.end += s
                mentions.append(m)
        elif MONTH_OR_DAY_RX.search(text[s:e]) and not any(not (e <= c[0] or s >= c[1]) for c in clusters):
            clusters.append((s, e, Hit(s, e, "DATE", "ner_unparsed_date", "ner")))
            res.flags["ner_date_unparsed"] += 1
    final_dates = []
    for m in mentions:
        overlapping = [c for c in clusters if not (c[1] <= m.start or c[0] >= m.end)]
        if not overlapping:
            final_dates.append(m)
        elif all(c[2].single_common and m.start <= c[0] and c[1] <= m.end for c in overlapping):
            clusters = [c for c in clusters if c not in overlapping]
            final_dates.append(m)
    # --- render
    ref_year = report_dt.year if report_dt else None
    repl: list[tuple[int, int, str, str, str]] = []
    for s, e, h in clusters:
        orig = text[s:e]
        token = pctx.surrogates.token(h.category, orig, h.key if h.source == "known" else None)
        repl.append((s, e, token, h.category, h.source))
        res.counts[f"{h.category}"] += 1
        res.counts[f"source_{h.source}"] += 1
    for m in final_dates:
        render_shifted(m, pctx.offsets, ref_year)
        repl.append((m.start, m.end, m.replacement if m.replacement is not None else m.text, "DATE", m.kind))
        res.counts["DATE"] += 1
        if m.flag:
            res.flags[m.flag] += 1
    for sk in skips:
        res.counts[f"lookalike_{sk.reason}"] += 1
    repl.sort()
    parts, pos = [out_prefix], body_start
    for s, e, token, cat, src in repl:
        if s < pos:
            continue
        parts.append(text[pos:s])
        parts.append(token)
        res.spans.append((s, e, cat, src, token))
        pos = e
    parts.append(text[pos:])
    res.text = "".join(parts)
    return res


# ------------------------------------------------------------------------------------------
def _ensure_paths(out: Path, linkage: Path, identifiers: Path | None) -> tuple[Path, Path]:
    out = ensure_phi_output(out, "the de-identified database")
    linkage = ensure_phi_output(linkage, "the linkage key")
    ensure_separate_dirs(linkage, out)
    if identifiers:
        ensure_outside_repo(identifiers, "identifiers.yaml")
    return out, linkage


def _age_cap(age: str) -> str:
    try:
        return "90+" if int(float(age)) >= 90 else str(int(float(age)))
    except (TypeError, ValueError):
        return ""


def deid_enabled(settings) -> bool:
    """De-identification is optional (13.11.26): `deid.enabled: false` in the settings keeps the notes as they are."""
    return bool(settings.get("deid.enabled", True)) if settings else True


def is_deidentified(deid_db: Path) -> bool:
    """Whether this notes database was de-identified (False: prepared with the identifiers kept)."""
    from nmosd.store.db import get_meta

    con = connect(deid_db, readonly=True)
    try:
        return bool(get_meta(con, "manifest").get("deidentified", True))
    finally:
        con.close()


def _dates_shifted(entry: dict) -> bool:
    """How a patient was prepared last time (linkage entries from before 13.11.26 were always de-identified)."""
    return bool(entry.get("dates_shifted", bool(entry.get("offset_days"))))


def run_deid(staging: Path, out: Path, linkage: Path, identifiers: Path | None, settings, use_ner: bool = True,
             debug_doc: int | None = None, ensemble: Ensemble | None = None, passphrase: str | None = None,
             allow_unconfirmed_identifiers: bool = False, keep_identifiers: bool | None = None,
             switch_mode: bool = False, stdout: TextIO | None = None) -> int:
    """De-identify the staged notes, or (keep_identifiers; default from `deid.enabled`) prepare them as they are:
    same database layout and study IDs, so extraction, review, export and re-identification work unchanged, but
    the original text and real dates (offset 0)."""
    stdout = stdout or sys.stdout
    keep = (not deid_enabled(settings)) if keep_identifiers is None else keep_identifiers
    out, linkage = _ensure_paths(out, linkage, identifiers)
    yaml_ids, yaml_staff, yaml_orgs = load_identifiers_yaml(None if keep else identifiers)
    if not keep and not yaml_ids and not allow_unconfirmed_identifiers:
        print("No confirmed identifiers.yaml found. Run `nmosd suggest-identifiers`, confirm the file, then re-run "
              "(or pass --allow-unconfirmed-identifiers for a dry run).", file=stdout)
        return 2
    if keep:
        use_ner, ensemble = False, None
    if use_ner and ensemble is None:
        try:
            ensemble = build_ensemble(settings)
        except NERUnavailable as e:
            print(f"NER layer unavailable: {e}\nRefusing to continue without it (use --no-ner only for testing).",
                  file=stdout)
            return 2
    if not use_ner:
        ensemble = None
    is_new = not Path(linkage).exists()
    passphrase = passphrase or get_passphrase(settings, confirm=is_new)
    link = empty_linkage() if is_new else load_linkage(linkage, passphrase)
    min_d, max_d = settings.get("deid.date_shift_min_days", 30), settings.get("deid.date_shift_max_days", 365)
    tokenise_hospitals = bool(settings.get("deid.tokenise_hospitals", True))
    patterns = PatternLayer(tokenise_hospitals, tuple(settings.get("deid.mrn_digit_lengths", [7, 8, 9, 10])))

    src = connect(staging, readonly=True)
    docs = [dict(r) for r in src.execute("SELECT * FROM documents ORDER BY empi, report_datetime, doc_pk")]
    sources = defaultdict(list)
    for r in src.execute("SELECT doc_pk, report_number FROM document_sources"):
        sources[r["doc_pk"]].append(r["report_number"])
    demo = {r["empi"]: dict(r) for r in src.execute("SELECT * FROM demographics")}
    structured = [dict(r) for r in src.execute("SELECT * FROM structured_rows")]
    src.close()
    empis = sorted({d["empi"] for d in docs} | set(demo))
    by_empi = {v["empi"]: sid for sid, v in link["patients"].items()}
    # A patient prepared the other way before has extraction, review and maybe an export made with the other
    # dates; re-identifying that export after a switch would give wrong dates, so switching must be asked for.
    switching = [e for e in empis if e in by_empi and _dates_shifted(link["patients"][by_empi[e]]) == keep]
    if switching and not switch_mode:
        before, now = ("de-identified", "kept as they are") if keep else ("kept as they are", "de-identified")
        print(f"{len(switching)} of these patients' notes were {before} last time, and would now be {now}. Their "
              "earlier extraction, review decisions and REDCap files used the other dates. Make the final "
              "(re-identified) file from that work first if you need it, then run again with --switch-mode: the "
              "extraction then runs again for them and their dates need reviewing again.", file=stdout)
        return 2

    # pass 1: harvest headers, sign-offs and structured identifier tables
    harvested: dict[str, IdentifierSet] = defaultdict(IdentifierSet)
    staff = set(yaml_staff) | set(link.get("staff", []))
    header_stats: dict[str, Counter] = defaultdict(Counter)
    for d in docs:
        h = parse_header(d["text"], d["source_table"])
        header_stats[d["source_table"]]["docs"] += 1
        header_stats[d["source_table"]]["matched"] += int(h.matched)
        harvested[d["empi"]].merge(from_harvest(h.harvested))
        staff.update(h.harvested.get("provider", []))
        staff.update(m.group(1) for m in STAFF_SIGNOFF_RX.finditer(d["text"]))
    for r in structured:
        if r["table_name"] in {"Mrn", "Contact"}:
            row = json.loads(r["row_json"])
            s = harvested[r["empi"]]
            if row.get("MRN"):
                s.mrns.add(re.sub(r"\D", "", row["MRN"]))
            for k, attr in (("Last_Name", "last"), ("First_Name", "first"), ("City", "towns"), ("Address1", "streets"),
                            ("Home_Phone", "phones"), ("Email_Address", "emails"), ("Zip", "zips")):
                if row.get(k):
                    getattr(s, attr).add(row[k].strip())
    orgs = sorted(set(yaml_orgs) | set(link.get("organizations", [])))
    staff_list = sorted(staff)

    # patients
    existing_sids = {p for p in link["patients"]}
    pctx: dict[str, PatientCtx] = {}
    unconfirmed = 0
    for e in empis:
        ids = IdentifierSet()
        if e in yaml_ids:
            ids.merge(yaml_ids[e])
        elif not keep:
            unconfirmed += 1
        ids.merge(harvested.get(e, IdentifierSet()))
        if demo.get(e, {}).get("dob"):
            ids.dob.add(demo[e]["dob"][:10])
        z = json.loads(demo[e]["raw_json"]).get("Zip_code", "") if e in demo else ""
        if re.fullmatch(r"\d{5}(-\d{4})?", (z or "").strip()):
            ids.zips.add(z.strip())
        sid = by_empi.get(e) or new_study_id(existing_sids)
        existing_sids.add(sid)
        entry = link["patients"].setdefault(sid, {"empi": e, "offset_days": 0 if keep else draw_offset_days(min_d, max_d),
                                                  "surrogates": {}})
        if keep:                       # real dates: re-identification then moves nothing
            entry["offset_days"] = 0
        elif not entry.get("offset_days"):
            entry["offset_days"] = draw_offset_days(min_d, max_d)
        entry["dates_shifted"] = not keep
        entry["mrns"] = sorted(ids.mrns)
        entry["dob"] = sorted(ids.dob)[0] if ids.dob else None
        entry["identifiers"] = ids.to_json()
        pctx[e] = PatientCtx(e, sid, Offsets(int(entry["offset_days"])), ids,
                             KnownMatcher(ids, staff_list, orgs), SurrogateMap(entry["surrogates"]))
    link["staff"], link["organizations"] = staff_list, orgs

    # pass 2: documents
    if out.exists():
        out.unlink()
    con = connect(out, "deid")
    totals, flags, ner_models = Counter(), Counter(), Counter()
    ner_union = 0
    existing_docs = set(link["documents"])
    review_rows = []
    t_start = time.perf_counter()
    for e in empis:
        dm = demo.get(e)
        raw = json.loads(dm["raw_json"]) if dm else {}
        dod = parse_datetime(raw.get("Date_Of_Death") or raw.get("Date_of_Death"))
        con.execute("INSERT INTO patients VALUES (?,?,?,?,?,?,?,?,?,?)", (
            pctx[e].study_id, raw.get("Sex_At_Birth"), raw.get("Gender_Legal_Sex"), raw.get("Race1"),
            raw.get("Race_Group"), raw.get("Ethnic_Group"), raw.get("Language_group"), raw.get("Vital_status"),
            (dod + timedelta(days=pctx[e].offsets.days)).date().isoformat() if dod else None,
            _age_cap(raw.get("Age", ""))))
    for i, d in enumerate(docs):
        p = pctx[d["empi"]]
        key = f"{d['empi']}:{d['text_hash']}"
        doc_id = link["doc_keys"].get(key) or new_doc_id(existing_docs)
        existing_docs.add(doc_id)
        link["doc_keys"][key] = doc_id
        rdt = datetime.fromisoformat(d["report_datetime"]) if d["report_datetime"] else None
        if keep:
            desc, text, layout, spans = d["report_description"] or "", d["text"], None, []
        else:
            desc = deidentify_text(d["report_description"] or "", p, patterns, None).text
            r = deidentify_text(d["text"], p, patterns, ensemble, table=d["source_table"], note_type=desc,
                                site=d["site"], report_dt=rdt, tokenise_hospitals=tokenise_hospitals)
            text, layout, spans = r.text, r.header.layout if r.header and r.header.matched else None, r.spans
            totals.update(r.counts)
            flags.update(r.flags)
            ner_models.update(r.ner_per_model)
            ner_union += r.ner_union
            for reason, n in r.flags.items():
                review_rows.append((doc_id, reason, n))
            for cat, n in Counter(f"{s[2]}|{s[3]}" for s in r.spans).items():
                c, srcname = cat.split("|", 1)
                con.execute("INSERT INTO span_counts VALUES (?,?,?,?)", (doc_id, c, srcname, n))
        con.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            doc_id, p.study_id, d["source_table"],
            (rdt + timedelta(days=p.offsets.days)).date().isoformat() if rdt else None,
            desc, d["report_type"], d["report_status"], d["site"], d["relevance_class"], d["n_sources"],
            layout, text))
        link["documents"][doc_id] = {"study_id": p.study_id, "doc_pk": d["doc_pk"], "text_hash": d["text_hash"],
                                     "source_table": d["source_table"], "report_numbers": sources[d["doc_pk"]],
                                     "spans": spans}
        if debug_doc is not None and i == debug_doc and not keep:
            print("----- ORIGINAL (PHI) -----\n" + d["text"] + "\n----- DE-IDENTIFIED -----\n" + text, file=stdout)
        report_progress(stdout, i + 1, len(docs), t_start, "documents")
    con.executemany("INSERT INTO review_queue VALUES (?,?,?)", review_rows)
    # structured tables (not Demographics, not identifier tables)
    n_struct = 0
    for row in structured:
        if row["table_name"] in {"Mrn", "Contact"}:
            continue
        p = pctx.get(row["empi"])
        if not p:
            continue
        clean = {}
        for k, v in json.loads(row["row_json"]).items():
            if k.lower() in IDENTIFIER_COLUMNS or not v:
                continue
            dt = parse_datetime(v)
            if dt:
                clean[k] = (dt + timedelta(days=p.offsets.days)).isoformat(sep=" ")
            else:
                clean[k] = v if keep else deidentify_text(v, p, patterns, None).text
        con.execute("INSERT INTO structured_rows VALUES (?,?,?)", (row["table_name"], p.study_id, json.dumps(clean)))
        n_struct += 1
    for p in pctx.values():
        link["patients"][p.study_id]["surrogates"] = p.surrogates.data
    manifest = {
        "tool_version": __version__, "python": platform.python_version(), "run_at": datetime.now().isoformat(timespec="seconds"),
        "deidentified": not keep, "patients_switched_mode": len(switching),
        "rules_hash": rules_hash(), "ner_enabled": ensemble is not None,
        "ner_models": [{"name": m.name, "hf_id": getattr(m, "loaded_id", None) or getattr(m, "hf_id", None),
                        "revision": getattr(m, "revision", None)} for m in (ensemble.models if ensemble else [])],
        "ner_span_counts_per_model": dict(ner_models), "ner_span_count_union": ner_union,
        "counts_by_category": dict(totals), "review_flags": dict(flags),
        "header_match": {t: dict(c) for t, c in header_stats.items()}, "documents": len(docs), "patients": len(empis),
        "structured_rows": n_struct, "patients_without_confirmed_identifiers": unconfirmed,
        "date_shift_range_days": [min_d, max_d], "tokenise_hospitals": tokenise_hospitals,
        "signoff_reviewer": "", "signoff_date": "", "signoff_complete": False,
    }
    try:
        import torch  # type: ignore
        import transformers  # type: ignore

        manifest["transformers"], manifest["torch"] = transformers.__version__, torch.__version__
    except ImportError:
        pass
    set_meta(con, "manifest", manifest)
    con.commit()
    con.close()
    save_linkage(linkage, link, passphrase)
    _print_summary(stdout, manifest)
    return 0


def _print_summary(out: TextIO, m: dict) -> None:
    if not m.get("deidentified", True):
        print(f"Prepared {m['documents']} documents for {m['patients']} patients WITHOUT de-identification: names, "
              "numbers and dates are as in the source, under study IDs. They stay in the private folder; there is "
              "nothing to leak-test or sign off.", file=out)
        if m.get("patients_switched_mode"):
            print(f"{m['patients_switched_mode']} patients were de-identified before: their extraction runs again "
                  "and their dates need reviewing again.", file=out)
        print("\nNext: nmosd extract.", file=out)
        return
    print(f"De-identified {m['documents']} documents for {m['patients']} patients "
          f"(NER {'on' if m['ner_enabled'] else 'OFF'}).", file=out)
    if m["patients_without_confirmed_identifiers"]:
        print(f"WARNING: {m['patients_without_confirmed_identifiers']} patients have no confirmed identifiers.yaml "
              "entry (header-harvested identifiers only).", file=out)
    print("\nHeader blocks matched a known layout (per table):", file=out)
    for t, c in sorted(m["header_match"].items()):
        pct = 100 * c.get("matched", 0) / max(1, c.get("docs", 0))
        print(f"  {t}: {c.get('matched', 0)}/{c.get('docs', 0)} ({pct:.0f}%)", file=out)
    print("\nReplacements by category:", file=out)
    for k, v in sorted(m["counts_by_category"].items()):
        print(f"  {k}: {v}", file=out)
    if m["ner_enabled"]:
        print(f"\nNER spans per model: {m['ner_span_counts_per_model']} · union: {m['ner_span_count_union']}", file=out)
    if m["review_flags"]:
        print("\nFlagged for manual review (counts):", file=out)
        for k, v in sorted(m["review_flags"].items()):
            print(f"  {k}: {v}", file=out)
    print("\nNext: nmosd leak-test, then nmosd deid-report for the 100% human review.", file=out)
