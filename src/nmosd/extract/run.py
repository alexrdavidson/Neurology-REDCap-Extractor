"""`nmosd extract`: resumable stage-1 batch over the de-identified documents.

Per patient (date order): boilerplate stripped and copied-forward paragraphs removed (first-occurrence
map kept) → candidate documents per field group (relevance class + keywords) → relevant sections →
chunks that fit num_ctx → LLM → evidence rows. Each (document, field group, run config) is committed
as soon as it finishes, so an overnight run survives interruption. Logs document IDs and counts only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import queue
import sys
import time
import uuid
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TextIO

from nmosd.config import load_rules
from nmosd.extract.admin_records import admin_rows
from nmosd.extract.chunking import select_view, split_view
from nmosd.extract.dedupe import dedupe_patient, line_keys, repeated_lines
from nmosd.extract.lab_tables import lab_table_rows
from nmosd.extract.note_facts import admission_date_rows, dx_date_rows
from nmosd.extract.ollama_client import LLMClient, OllamaClient, PromptTooLong, estimate_tokens
from nmosd.extract.prefilter import group_specs, is_candidate, keyword_rx
from nmosd.extract.stage1_evidence import (
    extract_chunk,
    field_specs_text,
    prompt_texts,
    prompt_version,
    schema_words,
)
from nmosd.extract.targets import disability_targets
from nmosd.extract.vitals import vitals_rows
from nmosd.redcap.dictionary import FIXTURE_DICTIONARY, REAL_DICTIONARY
from nmosd.redcap.schemas import STRUCTURED_ONLY, schemas_for
from nmosd.redcap.sources import hospital_names_kept
from nmosd.safety import ensure_phi_output
from nmosd.store.db import connect, get_meta

EVIDENCE_COLS = ["evidence_id", "run_id", "doc_id", "chunk_id", "study_id", "field_group", "item_id", "field", "value",
                 "event_date", "estimate_code", "quote", "char_start", "char_end", "certainty", "negated",
                 "hypothetical", "verified", "verify_score", "doc_date"]
# `method: regex` groups: the reader that makes their evidence rows from an excerpt (no model)
REGEX_READERS = {"vitals": vitals_rows, "treatment_admin": admin_rows, "labs_table": lab_table_rows,
                 "dx_dates": dx_date_rows, "admission_dates": admission_date_rows}
# Stop after this many documents in a row get no answer from the model (Ollama quit, the Mac slept, out of memory).
MAX_FAILURES_IN_A_ROW = 3


def resolve_dictionary(dictionary: Path | None, out: TextIO) -> Path:
    if dictionary:
        return Path(dictionary)
    if REAL_DICTIONARY.exists():
        return REAL_DICTIONARY
    print("NOTE: config/redcap/data_dictionary.csv not found; using the codebook-derived pilot fixture.", file=out)
    return FIXTURE_DICTIONARY


def make_client(llm: str, settings, model: str | None, annotations: Path | None) -> LLMClient:
    if llm == "synthetic-oracle":
        from nmosd.extract.synthetic_oracle import SyntheticOracleClient

        if not annotations:
            raise SystemExit("--annotations is required with --llm synthetic-oracle")
        return SyntheticOracleClient(annotations)
    return OllamaClient.from_settings(settings, model=model)


# Until 13.11.26 one hash over all rule files went into every group's run hash, so editing one group's spec (or
# adding a group) re-ran every group. Rules are now hashed per group. A group whose own rules are unchanged since
# then keeps the old whole-file hash, so documents already extracted with it are not run again.
LEGACY_RULES_HASH = "262d74c2c8b72a13"
LEGACY_GROUP_RULES = {"diagnosis": "b41fc9b557849125", "relapse": "c96719a3af3085ed", "treatment": "aa03da37c21a1861",
                      "antibody": "01e05c01ebb1c21b", "followup": "1b2ee1e5d65e971b", "demographics": "ad681d811eeb88c2"}


def group_rules_hash(group: str) -> str:
    """Hash over the rules that shape one group's prompts and chunks: its own spec, the section headings and its
    own section choice, boilerplate and document types. Other groups' specs do not count."""
    specs, sections = load_rules("fields/pilot_fields.yaml"), load_rules("sections.yaml")
    payload = {"spec": specs["groups"][group],
               "headings": {k: sections.get(k) for k in ("heading_line", "inline_heading", "sections")},
               "group_sections": sections.get("field_group_sections", {}).get(group),
               "boilerplate": load_rules("boilerplate.yaml"), "doc_types": load_rules("doc_types.yaml")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def run_config_hash(client: LLMClient, group_schema: dict, settings, group: str, deidentified: bool = True) -> str:
    """deidentified=False (notes kept as they are): a different hash, so evidence found in the de-identified text
    (other dates, [PATIENT] tokens) never counts as done for the original text, or the reverse."""
    digest = client.digest() if hasattr(client, "digest") else None
    rules = group_rules_hash(group)
    if LEGACY_GROUP_RULES.get(group) == rules:
        rules = LEGACY_RULES_HASH
    payload = {"model": client.model, "digest": digest, "num_ctx": client.num_ctx, "prompt": prompt_version(),
               "schema": hashlib.sha256(json.dumps(group_schema, sort_keys=True).encode()).hexdigest()[:12],
               "rules": rules,
               "reserve": settings.get("extraction.reserve_tokens"), "overlap": settings.get("extraction.chunk_overlap_chars"),
               "group": group}
    if not deidentified:
        payload["notes"] = "original"
    num_predict = int(getattr(client, "num_predict", 1024) or 1024)
    if num_predict != 1024:   # it sets the excerpt sizes; left out at the old value so earlier runs stay done
        payload["num_predict"] = num_predict
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def default_groups() -> list[str]:
    """Every field group except one that only checks structured-only fields against the notes: since 16.11.26 the
    `demographics` group, as sex, race, ethnicity and living/deceased come from the Demographics table alone
    (`note_check: false`). `--groups demographics` still runs it."""
    return [g for g, spec in group_specs().items()
            if not (spec.get("fields") and set(spec["fields"]) <= STRUCTURED_ONLY) or spec.get("redcap_fields")]


def group_budgets(client: LLMClient, schemas: dict, groups: list[str], budget_chars: int) -> dict[str, int]:
    """Characters of note text per chunk for each group: the general budget, made smaller for a group whose
    instructions and schema (codebook labels and codes) leave less room in num_ctx."""
    system, template = prompt_texts()
    out_tokens = int(getattr(client, "num_predict", 1024) or 1024)
    budgets = {}
    specs = group_specs()
    for g in groups:
        if g not in schemas:
            budgets[g] = budget_chars
            continue
        overhead = estimate_tokens(system + template + field_specs_text(g) + schema_words(schemas[g][1])) + 64
        budgets[g] = min(budget_chars, max(2000, int((client.num_ctx - out_tokens - overhead) * 3.4)))
        # `max_chunk_chars` (18.11.26): shorter excerpts for a group where a small model lost events in long ones
        # (a 30,000-character excerpt with four attacks in one paragraph gave no attack at all)
        if specs[g].get("max_chunk_chars"):
            budgets[g] = min(budgets[g], max(2000, int(specs[g]["max_chunk_chars"])))
    return budgets


def plan_work(con_d, groups: list[str], budget_chars: int | dict[str, int], overlap: int, skip: set[str],
              targets: dict[str, set[str]] | None = None, only_docs: set[str] | None = None):
    """Return (work items, paragraph repeats, token stats). `targets`: the documents a `select:` group may read
    ({group: doc_ids}); such a group reads nothing without them. `only_docs` (a fast set, 18.11.26): the only
    documents read, apart from a `select:` group's targets; copied-forward text is then judged among these documents
    alone, so a paragraph first written in a note outside the set is kept where the set first has it."""
    budgets = budget_chars if isinstance(budget_chars, dict) else {g: budget_chars for g in groups}
    by_patient = defaultdict(list)
    keep = None if only_docs is None else set(only_docs).union(*(targets or {}).values())
    for r in con_d.execute("SELECT doc_id, study_id, source_table, report_date, report_description, relevance_class, text "
                           "FROM documents ORDER BY study_id, report_date, doc_id"):
        if keep is None or r["doc_id"] in keep:
            by_patient[r["study_id"]].append(dict(r))
    work, repeats = [], []
    stats = Counter()
    specs = group_specs()
    for docs in by_patient.values():
        dd = dedupe_patient([(d["doc_id"], d["text"]) for d in docs])
        seen_lines: dict[str, set[str]] = defaultdict(set)   # per group: lines already sent for this patient
        repeats += dd.repeats
        for k, v in dd.stats.items():
            stats[k] += v
        for d in docs:
            view = dd.views[d["doc_id"]]
            for g in groups:
                if specs[g].get("select") and d["doc_id"] not in (targets or {}).get(g, set()):
                    stats[f"{g}:not_targeted"] += 1
                    continue
                if only_docs is not None and not specs[g].get("select") and d["doc_id"] not in only_docs:
                    continue   # kept only as another group's target
                ok, why = is_candidate(g, d["relevance_class"], view.text, skip, d["report_description"])
                stats[f"{g}:{why}"] += 1
                if not ok:
                    continue
                stats[f"{g}:chars_after_prefilter"] += len(view.text)
                sel = select_view(view, g)
                if sel is not view and not keyword_rx(g).search(sel.text):
                    stats[f"{g}:no_keyword_in_sections"] += 1   # its keywords are only in sections it does not read
                    continue
                if specs[g].get("skip_repeated_lines"):
                    core = select_view(view, g, preamble=False)
                    drop, new, n_lines = repeated_lines(core, seen_lines[g])
                    seen_lines[g] |= line_keys(core)
                    stats[f"{g}:repeated_lines"] += n_lines
                    if not new:
                        stats[f"{g}:only_repeats"] += 1   # nothing this group has not read in an earlier note
                        continue
                    sel = sel.remove(drop)
                stats[f"{g}:chars_after_sections"] += len(sel.text)
                chunks = split_view(sel, budgets.get(g, max(budgets.values(), default=20000)), overlap)
                if chunks:
                    work.append({"doc": d, "group": g, "chunks": chunks})
    stats["tokens_raw"] = sum(estimate_tokens(d["text"]) for ds in by_patient.values() for d in ds)
    return work, repeats, stats


def _print_plan(out: TextIO, stats: Counter, work: list, groups: list[str]) -> None:
    tok = lambda c: int(c / 3.6)  # noqa: E731
    print("# Extraction plan (counts only)\n", file=out)
    print(f"- Tokens, all documents: ~{stats['tokens_raw']:,}", file=out)
    print(f"- After boilerplate removal: ~{tok(stats['chars_after_boilerplate']):,}", file=out)
    print(f"- After copy-forward removal: ~{tok(stats['chars_after_dedupe']):,} "
          f"({stats['repeated_paragraphs']:,} repeated paragraphs)", file=out)
    print("\n| Field group | Documents selected | Skipped (class) | Irrelevant class | No keyword | Chunks | ~Tokens sent |",
          file=out)
    print("|---|---|---|---|---|---|---|", file=out)
    specs = group_specs()
    for g in groups:
        ws = [w for w in work if w["group"] == g]
        ch = sum(len(w["chunks"]) for w in ws)
        sent = sum(len(c.text) for w in ws for c in w["chunks"])
        note = " (no model: text patterns)" if specs[g].get("method") == "regex" else ""
        print(f"| {g}{note} | {stats[f'{g}:selected'] - stats[f'{g}:no_keyword_in_sections'] - stats[f'{g}:only_repeats']} | "
              f"{stats[f'{g}:skipped_class']} | "
              f"{stats[f'{g}:irrelevant_class']} | {stats[f'{g}:no_keyword'] + stats[f'{g}:no_keyword_in_sections'] + stats[f'{g}:only_repeats']} | "
              f"{ch} | ~{tok(sent):,} |", file=out)
    skipping = [g for g in groups if specs[g].get("skip_repeated_lines")]
    if skipping:
        print(f"\nRepeated lines left out ({', '.join(skipping)}): "
              f"{sum(stats[f'{g}:repeated_lines'] for g in skipping):,} lines already read in an earlier note of the "
              f"same patient; {sum(stats[f'{g}:only_repeats'] for g in skipping):,} notes had nothing new for these "
              "groups and are not read (counted under No keyword).", file=out)
    waiting = [g for g in groups if specs[g].get("select") and not stats[f"{g}:selected"]]
    if waiting:
        print(f"\n{', '.join(waiting)}: read only the notes at each treatment start and stop and at the last follow-up, "
              "which the first Combine finds. Run extract again after reconcile (the guided app does this).", file=out)
    print(file=out)


def _print_remaining(out: Path, client: LLMClient, schemas: dict, settings, groups: list[str], work: list,
                     deidentified: bool, stdout: TextIO) -> None:
    """Dry run: how much of the plan an earlier run already did with the same model, prompts and rules (counts
    only), so the guided app's preview says whether the model needs to run again at all."""
    if not Path(out).exists():
        print("Nothing has been extracted yet: all of the above will run.", file=stdout)
        return
    if hasattr(client, "digest") and client.digest() is None:
        print("Could not tell how much is already done: the model is not available (is Ollama running?).",
              file=stdout)
        return
    hashes = {g: run_config_hash(client, schemas[g][1] if g in schemas else {"method": "regex"}, settings, g,
                                 deidentified) for g in groups}
    con = connect(out, readonly=True)
    try:
        done = {(r[0], r[1], r[2]) for r in con.execute("SELECT run_config_hash, doc_id, field_group FROM done")}
    finally:
        con.close()
    print("| Field group | Already done | To run |\n|---|---|---|", file=stdout)
    total = 0
    for g in groups:
        ws = [w for w in work if w["group"] == g]
        left = sum((hashes[g], w["doc"]["doc_id"], g) not in done for w in ws)
        total += left
        print(f"| {g} | {len(ws) - left} | {left} |", file=stdout)
    print(f"\n**{total} (document, group) pairs to run.**"
          + (" The model does not need to run: Continue only combines the results (a minute or two)." if not total
             else ""), file=stdout)


def _ask_pair(clients: queue.Queue, w: dict, schemas: dict, h: str) -> list[tuple]:
    """Ask the model about one (document, field group) pair, excerpt by excerpt, with a client of its own from
    `clients`. No database access: it may run in a worker thread. Returns [(chunk_id, chunk number, chunk, kind,
    payload)], kind "regex" (payload: evidence rows), "too_long" or "model" (payload: the ChunkResult); it stops after
    an excerpt the model did not answer, like the one-at-a-time run did."""
    d, g = w["doc"], w["group"]
    model_cls, schema = schemas.get(g, (None, None))
    out: list[tuple] = []
    client = clients.get()
    try:
        for ci, chunk in enumerate(w["chunks"], 1):
            chunk_id = f"{d['doc_id']}:{g}:{ci}"
            if model_cls is None:   # `method: regex` group: text patterns, no model
                out.append((chunk_id, ci, chunk, "regex", REGEX_READERS[g](chunk, run_id=h, doc_id=d["doc_id"],
                                                                           chunk_id=chunk_id, study_id=d["study_id"],
                                                                           doc_date=d["report_date"])))
                continue
            try:
                res = extract_chunk(client, model_cls, schema, g, chunk, run_id=h, doc_id=d["doc_id"], chunk_id=chunk_id,
                                    study_id=d["study_id"], doc_type=d["report_description"], source_table=d["source_table"],
                                    doc_date=d["report_date"], chunk_no=ci, n_chunks=len(w["chunks"]))
            except PromptTooLong:
                out.append((chunk_id, ci, chunk, "too_long", None))
                continue
            out.append((chunk_id, ci, chunk, "model", res))
            if res.llm.error is not None:
                break
    finally:
        clients.put(client)
    return out


def _record_pair(con, w: dict, h: str, results: list[tuple], totals: Counter, parallel: int = 1) -> bool:
    """Write one pair's excerpts, evidence and model calls. Returns True if the model did not answer (not done)."""
    d, g = w["doc"], w["group"]
    ins_ev = f"INSERT INTO evidence ({','.join(EVIDENCE_COLS)}) VALUES ({','.join('?' * len(EVIDENCE_COLS))})"
    for chunk_id, ci, chunk, kind, payload in results:
        con.execute("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?,?,?,?)",
                    (chunk_id, d["doc_id"], d["study_id"], g, ci, json.dumps(chunk.pieces), len(chunk.text),
                     estimate_tokens(chunk.text)))
        if kind == "regex":
            con.execute("DELETE FROM evidence WHERE chunk_id = ? AND run_id = ?", (chunk_id, h))
            con.executemany(ins_ev, [[r.get(c) for c in EVIDENCE_COLS] for r in payload])
            totals["chunks"] += 1
            totals["rows"] += len(payload)
            continue
        if kind == "too_long":
            totals["prompt_too_long"] += 1
            continue
        res = payload
        con.execute("DELETE FROM evidence WHERE chunk_id = ? AND run_id = ?", (chunk_id, h))
        con.executemany(ins_ev, [[r.get(c) for c in EVIDENCE_COLS] for r in res.rows])
        for call in res.calls or [res.llm]:
            rec = call.record()
            if parallel > 1:
                rec["parallel"] = parallel   # its wall time overlaps with others' (nmosd timing divides by this)
            con.execute("INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, h, chunk_id, f"stage1:{g}", json.dumps(rec),
                         hashlib.sha256(chunk.text.encode()).hexdigest()[:16], call.content,
                         datetime.now().isoformat(timespec="seconds")))
        totals["chunks"] += 1
        totals["items"] += res.n_items
        totals["rows"] += len(res.rows)
        totals["invalid_json"] += int(not res.valid and not res.splits)
        totals["truncation_suspected"] += int(res.llm.truncation_suspected)
        totals["llm_errors"] += int(res.llm.error is not None)
        totals["plans_demoted"] += res.demoted_plans
        totals["split"] += res.splits
        totals["salvaged"] += res.salvaged
        if res.llm.error is not None:
            return True
    return False


def _in_order(todo: list[dict], job, parallel: int):
    """Yield (work item, job(work item)) in the order of `todo`. With parallel > 1, `parallel` items are worked on at
    once (a few more wait in line), but the answers are still handed back, and so written to the database, in the
    planned order: the database ends up as after a one-at-a-time run. Closing the generator cancels what has not
    started and waits for the questions in progress."""
    if parallel <= 1:
        for w in todo:
            yield w, job(w)
        return
    ex = ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="nmosd-llm")
    window: deque = deque()
    it = iter(todo)
    try:
        for w in it:
            window.append((w, ex.submit(job, w)))
            if len(window) >= 2 * parallel:
                break
        while window:
            w, fut = window.popleft()
            res = fut.result()
            nxt = next(it, None)
            if nxt is not None:
                window.append((nxt, ex.submit(job, nxt)))
            yield w, res
    finally:
        ex.shutdown(wait=True, cancel_futures=True)


def run_extraction(deid: Path, out: Path, settings, groups: list[str] | None = None, llm: str = "ollama",
                   model: str | None = None, annotations: Path | None = None, dictionary: Path | None = None,
                   limit_docs: int | None = None, dry_run: bool = False, client: LLMClient | None = None,
                   stdout: TextIO | None = None, only_docs: set[str] | None = None) -> int:
    """only_docs: read these documents only (a fast set, see `nmosd fast-test`); give it an `out` of its own."""
    stdout = stdout or sys.stdout
    out = ensure_phi_output(out, "the extraction database")
    groups = groups or default_groups()
    dic_path = resolve_dictionary(dictionary, stdout)
    schemas = schemas_for(dic_path)
    client = client or make_client(llm, settings, model, annotations)
    reserve = int(settings.get("extraction.reserve_tokens", 2200))
    overlap = int(settings.get("extraction.chunk_overlap_chars", 400))
    budget_chars = int((client.num_ctx - reserve) * 3.4)
    budgets = group_budgets(client, schemas, groups, budget_chars)
    skip = set(settings.get("extraction.skip_relevance", ["low_value"]))
    con_d = connect(deid, readonly=True)
    manifest = get_meta(con_d, "manifest")
    deidentified = bool(manifest.get("deidentified", True))
    # 18.11.26: a group that reads hospital names (`needs_hospital_names`) runs only on notes that still name them
    if not hospital_names_kept(manifest):
        no_names = [g for g in groups if group_specs()[g].get("needs_hospital_names")]
        if no_names:
            groups = [g for g in groups if g not in no_names]
            print(f"Not reading {', '.join(no_names)}: de-identification replaced the hospital names in these notes. "
                  "The reviewer enters hx_opcare, nr_hosp and nr_hosp_2 in REDCap.\n", file=stdout)
    selecting = [g for g in groups if group_specs()[g].get("select")]
    targets = {g: disability_targets(Path(out), con_d) for g in selecting} if selecting else None
    work, repeats, stats = plan_work(con_d, groups, budgets, overlap, skip, targets, only_docs)
    con_d.close()
    if only_docs is not None:
        print(f"Fast set: reading {len(only_docs)} chosen notes only (plus the examination notes the disability "
              "groups pick).\n", file=stdout)
    _print_plan(stdout, stats, work, groups)
    if dry_run:
        _print_remaining(out, client, schemas, settings, groups, work, deidentified, stdout)
        return 0
    con = connect(out, "extract")
    doc_ids = {r.doc_id for r in repeats}
    con.executemany("DELETE FROM paragraph_repeats WHERE doc_id = ?", [(d,) for d in doc_ids])
    con.executemany("INSERT INTO paragraph_repeats VALUES (?,?,?,?,?,?)",
                    [(r.doc_id, r.start, r.end, r.first_doc_id, r.first_start, r.first_end) for r in repeats])
    hashes = {g: run_config_hash(client, schemas[g][1] if g in schemas else {"method": "regex"}, settings, g,
                                 deidentified) for g in groups}
    for g, h in hashes.items():
        con.execute("INSERT OR IGNORE INTO runs VALUES (?,?,?,?)",
                    (h, datetime.now().isoformat(timespec="seconds"),
                     json.dumps({"group": g, "model": client.model, "num_ctx": client.num_ctx, "prompt": prompt_version()}), h))
    con.commit()
    done = {(r[0], r[1], r[2]) for r in con.execute("SELECT run_config_hash, doc_id, field_group FROM done")}
    todo = [w for w in work if (hashes[w["group"]], w["doc"]["doc_id"], w["group"]) not in done]
    if limit_docs is not None:
        todo = todo[:limit_docs]
    print(f"{len(work) - len([w for w in work if (hashes[w['group']], w['doc']['doc_id'], w['group']) not in done])} "
          f"(document, group) pairs already done; {len(todo)} to run.", file=stdout)
    parallel = max(1, int(settings.get("llm.parallel", 1) or 1)) if hasattr(client, "clone") else 1
    parallel = min(parallel, max(1, len(todo)))
    clients: queue.Queue = queue.Queue()
    for c in [client] + [client.clone() for _ in range(parallel - 1)]:
        clients.put(c)
    if parallel > 1:
        print(f"Asking the model {parallel} questions at a time (llm.parallel; Ollama needs OLLAMA_NUM_PARALLEL={parallel} "
              "or more to answer them together).", file=stdout)

    def job(w: dict) -> list:
        return _ask_pair(clients, w, schemas, hashes[w["group"]])

    t0, totals = time.perf_counter(), Counter()
    failed_in_a_row, stopped = 0, False
    with contextlib.closing(_in_order(todo, job, parallel)) as answers:
        for i, (w, results) in enumerate(answers, 1):
            d, g = w["doc"], w["group"]
            h = hashes[g]
            if _record_pair(con, w, h, results, totals, parallel):
                # Not marked done, so the next run retries it. Stop if Ollama has gone away (quit, sleep, out of
                # memory) rather than burning through every remaining document with errors.
                con.commit()
                totals["not_done"] += 1
                failed_in_a_row += 1
                print(f"[{i}/{len(todo)}] {d['doc_id']} {g}: no answer from the model; left for the next run", file=stdout)
                if failed_in_a_row >= MAX_FAILURES_IN_A_ROW:
                    stopped = True
                    print(f"\nStopped: the model did not answer {failed_in_a_row} documents in a row. Is Ollama running? "
                          "Nothing is lost: start Ollama and run the same command again to carry on.", file=stdout)
                    break
                continue
            failed_in_a_row = 0
            con.execute("INSERT OR REPLACE INTO done VALUES (?,?,?,?)", (h, d["doc_id"], g, datetime.now().isoformat(timespec="seconds")))
            con.commit()
            el = time.perf_counter() - t0
            eta = el / i * (len(todo) - i)
            print(f"[{i}/{len(todo)}] {d['doc_id']} {g}: {len(w['chunks'])} chunk(s) · ETA {int(eta // 3600):02d}:"
                  f"{int(eta % 3600 // 60):02d}:{int(eta % 60):02d}", file=stdout)
    elapsed = time.perf_counter() - t0
    ver = con.execute("SELECT COUNT(DISTINCT item_id), SUM(verified) FROM (SELECT item_id, MAX(verified) AS verified "
                      "FROM evidence GROUP BY item_id)").fetchone()
    con.close()
    n_items, n_ver = ver[0] or 0, ver[1] or 0
    print(f"\nDone: {totals['chunks']} chunks, {totals['items']} items, {totals['rows']} evidence rows; "
          f"quotes verified {n_ver}/{n_items} ({100 * n_ver / max(1, n_items):.0f}%); invalid JSON {totals['invalid_json']}; "
          f"truncation suspected {totals['truncation_suspected']}; LLM errors {totals['llm_errors']}; "
          f"plans demoted from start/stop {totals['plans_demoted']}; prompts too long {totals['prompt_too_long']}; "
          f"long answers split and asked again {totals['split']}; items rescued from cut-off answers {totals['salvaged']}; "
          f"left for the next run {totals['not_done']}.",
          file=stdout)
    if todo:
        print(f"Time: {elapsed / 3600:.1f} h for {len(todo)} (document, group) pairs"
              f"{f' ({parallel} at a time)' if parallel > 1 else ''}.", file=stdout)
    if totals["not_done"]:
        print(f"{totals['not_done']} (document, group) pairs got no answer from the model. Run the same command again "
              "to retry them.", file=stdout)
    return 1 if stopped or totals["not_done"] else 0
