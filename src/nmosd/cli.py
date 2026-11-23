"""Command-line entry point: `python -m nmosd <command>` (or `nmosd <command>`).

Every command that touches real data prints metadata and aggregates only. Debug options that
would print content require --i-understand-this-prints-phi.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import traceback
from pathlib import Path

from nmosd.config import load_settings
from nmosd.safety import PHI_ACK_FLAG, SafetyError

log = logging.getLogger("nmosd")


def _p(value: str | None) -> Path | None:
    return Path(os.path.expanduser(value)) if value else None


def _need(path: Path | None, what: str) -> Path:
    if path is None:
        raise SystemExit(f"Missing {what}: pass it on the command line or set it in config/settings.yaml")
    return path


def require_phi_ack(args, option: str) -> None:
    if not getattr(args, "i_understand_this_prints_phi", False):
        raise SystemExit(f"{option} prints note content (PHI). Re-run with {PHI_ACK_FLAG} if you really mean it.")


# ------------------------------------------------------------------------------------------
def cmd_inspect(args, settings) -> int:
    from nmosd.ingest.access_reader import BackendUnavailable, open_source
    from nmosd.ingest.inspect_source import inspect

    src = _need(_p(args.source) or settings.path("source"), "--source")
    if args.show_categories:
        print("WARNING: --show-categories prints Report_Description values (note titles). They are usually "
              "not identifying, but read them before sharing.", file=sys.stderr)
    try:
        reader = open_source(src, args.backend)
    except BackendUnavailable as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        inspect(reader, show_categories=args.show_categories, source_label=src.suffix.lstrip(".") + " file")
    finally:
        reader.close()
    return 0


def cmd_benchmark(args, settings) -> int:
    from nmosd.extract.benchmark import run_benchmark

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    sizes = [int(s) for s in args.sizes.split(",")]
    run_benchmark(models, args.num_ctx, sizes, settings)
    return 0


def cmd_stage(args, settings) -> int:
    from nmosd.ingest.load_staging import stage

    empi_list = None if args.all_patients else _need(_p(args.empi_list) or settings.path("empi_list"), "--empi-list")
    stage(source=_need(_p(args.source) or settings.path("source"), "--source"), empi_list=empi_list,
          out=_need(_p(args.out) or settings.path("staging_db"), "--out"),
          settings=settings, backend=args.backend, all_patients=args.all_patients)
    return 0


def cmd_select_pilot(args, settings) -> int:
    from nmosd.ingest.select_pilot import select_pilot

    select_pilot(staging=_need(_p(args.staging) or settings.path("staging_db"), "--staging"),
                 out=_need(_p(args.out) or settings.path("pilot_selection_db"), "--out"), settings=settings)
    return 0


def cmd_suggest_identifiers(args, settings) -> int:
    from nmosd.deid.known_identifiers import suggest_identifiers

    suggest_identifiers(staging=_need(_p(args.staging) or settings.path("staging_db"), "--staging"),
                        out=_need(_p(args.out), "--out"), settings=settings)
    return 0


def cmd_header_shapes(args, settings) -> int:
    from nmosd.deid.header_shapes import header_shapes

    header_shapes(_need(_p(args.staging) or settings.path("staging_db"), "--staging"), lines=args.lines,
                  min_count=args.min_count)
    return 0


def cmd_lab_shapes(args, settings) -> int:
    from nmosd.eval.lab_shapes import run_lab_shapes

    kw = dict(min_notes=args.min_notes, min_patients=args.min_patients, top=args.top,
              everything=args.command == "shapes")
    if args.staged:
        run_lab_shapes(_need(_p(args.db) or settings.path("staging_db"), "--db"), **kw)
    else:
        run_lab_shapes(source=_need(_p(args.source) or settings.path("source"), "--source"), **kw)
    return 0


def cmd_deid(args, settings) -> int:
    if args.debug_doc is not None:
        require_phi_ack(args, "--debug-doc")
    from nmosd.deid.pipeline import run_deid

    return run_deid(staging=_need(_p(args.staging) or settings.path("staging_db"), "--staging"),
                    out=_need(_p(args.out) or settings.path("deid_db"), "--out"),
                    linkage=_need(_p(args.linkage) or settings.path("linkage"), "--linkage"),
                    identifiers=_p(args.identifiers) or settings.path("identifiers"),
                    settings=settings, use_ner=not args.no_ner, debug_doc=args.debug_doc,
                    allow_unconfirmed_identifiers=args.allow_unconfirmed_identifiers,
                    keep_identifiers=True if args.keep_identifiers else None, switch_mode=args.switch_mode)


def _not_deidentified(deid: Path | None) -> bool:
    """Leak test, report and sign-off have nothing to do when the notes were kept as they are."""
    from nmosd.deid.pipeline import is_deidentified

    if deid and deid.exists() and not is_deidentified(deid):
        print("The notes were prepared without de-identification (identifiers kept): there is nothing to "
              "leak-test, report on or sign off.")
        return True
    return False


def cmd_leak_test(args, settings) -> int:
    from nmosd.deid.leak_test import CANDIDATES_NAME, run_leak_test

    report = _p(args.report) or settings.path("deid_report")
    if _not_deidentified(_p(args.deid) or settings.path("deid_db")):
        return 0
    return run_leak_test(deid=_need(_p(args.deid) or settings.path("deid_db"), "--deid"),
                         linkage=_need(_p(args.linkage) or settings.path("linkage"), "--linkage"),
                         settings=settings, use_ner=not args.no_ner,
                         candidates_out=report.with_name(CANDIDATES_NAME) if report else None)


def cmd_deid_report(args, settings) -> int:
    from nmosd.deid.review_report import write_report

    if _not_deidentified(_p(args.deid) or settings.path("deid_db")):
        return 0
    write_report(staging=_need(_p(args.staging) or settings.path("staging_db"), "--staging"),
                 deid=_need(_p(args.deid) or settings.path("deid_db"), "--deid"),
                 linkage=_need(_p(args.linkage) or settings.path("linkage"), "--linkage"),
                 out=_need(_p(args.out) or settings.path("deid_report"), "--out"), settings=settings)
    return 0


def cmd_deid_signoff(args, settings) -> int:
    from nmosd.deid.review_report import import_signoff

    if _not_deidentified(_p(args.deid) or settings.path("deid_db")):
        return 0
    import_signoff(deid=_need(_p(args.deid) or settings.path("deid_db"), "--deid"), signoff=_p(args.file))
    return 0


def cmd_schemas(args, settings) -> int:
    from nmosd.redcap.schemas import write_schemas

    write_schemas(dictionary=_need(_p(args.dictionary), "--dictionary"), out_dir=_need(_p(args.out), "--out"))
    return 0


def cmd_extract(args, settings) -> int:
    from nmosd.extract.run import run_extraction

    llm = args.llm or settings.get("llm.backend") or "ollama"
    annotations = _p(args.annotations) or _p(settings.get("llm.synthetic_annotations"))
    return run_extraction(deid=_need(_p(args.deid) or settings.path("deid_db"), "--deid"),
                          out=_need(_p(args.out) or settings.path("extract_db"), "--out"),
                          settings=settings, groups=args.groups.split(",") if args.groups else None,
                          llm=llm, model=args.model, annotations=annotations,
                          dictionary=_p(args.dictionary), limit_docs=args.limit_docs, dry_run=args.dry_run)


def cmd_reconcile(args, settings) -> int:
    from nmosd.extract.stage2_reconcile import run_reconcile

    return run_reconcile(extract=_need(_p(args.extract) or settings.path("extract_db"), "--extract"),
                         deid=_need(_p(args.deid) or settings.path("deid_db"), "--deid"), settings=settings)


def _reference_args(args, settings, deid):
    """(reference folder, packet key) for `score` and `fast-test`, from the arguments or the usual places."""
    from nmosd.eval.score_reference import default_key, default_reference

    base = settings.path("quality_packet_dir") or Path("~/Documents/NMOSD quality review").expanduser()
    reference = _p(args.reference) or default_reference(base)
    if reference is None:
        raise SystemExit("No reference answers found: give --reference <folder with Patient_*.json>.")
    return reference, _p(args.key) or default_key(reference, deid)


def cmd_score(args, settings) -> int:
    from nmosd.eval.score_reference import fast_db, run_score

    deid = _need(_p(args.deid) or settings.path("deid_db"), "--deid")
    extract = _need(_p(args.extract) or settings.path("extract_db"), "--extract")
    reference, key = _reference_args(args, settings, deid)
    return run_score(extract=fast_db(extract) if args.fast else extract, reference=reference, key=key,
                     dictionary=_p(args.dictionary), min_conf=args.min_confidence, list_fields=args.list,
                     back=args.back)


def cmd_fast_test(args, settings) -> int:
    from nmosd.eval.score_reference import run_fast_test

    deid = _need(_p(args.deid) or settings.path("deid_db"), "--deid")
    reference, key = _reference_args(args, settings, deid)
    llm = args.llm or settings.get("llm.backend") or "ollama"
    return run_fast_test(deid=deid, extract=_need(_p(args.extract) or settings.path("extract_db"), "--extract"),
                         reference=reference, key=key, settings=settings,
                         groups=args.groups.split(",") if args.groups else None, llm=llm, model=args.model,
                         annotations=_p(args.annotations) or _p(settings.get("llm.synthetic_annotations")),
                         dictionary=_p(args.dictionary), min_conf=args.min_confidence, list_fields=args.list)


def cmd_coverage(args, settings) -> int:
    from nmosd.eval.coverage import run_coverage

    return run_coverage(extract=_need(_p(args.extract) or settings.path("extract_db"), "--extract"),
                        deid=_need(_p(args.deid) or settings.path("deid_db"), "--deid"),
                        dictionary=_p(args.dictionary))


def cmd_accuracy(args, settings) -> int:
    from nmosd.eval.accuracy import run_accuracy

    report = settings.path("deid_report")
    out = _p(args.out) or (report.parent / "accuracy" if report else None)
    return run_accuracy(review=_need(_p(args.review_db) or settings.path("review_db"), "--review-db"),
                        extract=_need(_p(args.extract) or settings.path("extract_db"), "--extract"),
                        deid=_need(_p(args.deid) or settings.path("deid_db"), "--deid"),
                        out_dir=_need(out, "--out"), dictionary=_p(args.dictionary), label=args.label or "")


def cmd_timing(args, settings) -> int:
    from nmosd.eval.timing import run_timing

    return run_timing(extract=_need(_p(args.extract) or settings.path("extract_db"), "--extract"),
                      deid=_p(args.deid) or settings.path("deid_db"), cohort=args.cohort)


def cmd_quality_packet(args, settings) -> int:
    from datetime import datetime

    from nmosd.eval.quality_packet import run_quality_packet
    from nmosd.eval.score_reference import default_key, default_reference, fast_db

    quality_dir = settings.path("quality_packet_dir") or Path("~/Documents/NMOSD quality review").expanduser()
    base = _p(args.out) or quality_dir
    out = base / f"packet_{datetime.now().strftime('%Y%m%d_%H%M')}"
    extract = _need(_p(args.extract) or settings.path("extract_db"), "--extract")
    deid = _need(_p(args.deid) or settings.path("deid_db"), "--deid")
    review = _need(_p(args.review_db) or settings.path("review_db"), "--review-db")
    tokens_from = None
    if args.fast:   # the fast test's results, with no review decisions laid over them
        extract, review = fast_db(extract), fast_db(review)
    reference = _p(args.reference) or (default_reference(base) or default_reference(quality_dir) if args.fast else None)
    if reference is not None:
        tokens_from = default_key(reference, deid)
    elif args.fast:
        print("No reference answers found (--reference): the date tokens are numbered afresh.")
    return run_quality_packet(extract=extract, deid=deid, review=review,
                              linkage=_need(_p(args.linkage) or settings.path("linkage"), "--linkage"),
                              out_dir=out, settings=settings, dictionary=_p(args.dictionary), context=args.context,
                              passages=args.passages, use_ner=args.ner, whole_notes=args.whole_notes,
                              tokens_from=tokens_from)


def cmd_review(args, settings) -> int:
    from nmosd.review.launch import launch

    return launch(extract=_need(_p(args.extract) or settings.path("extract_db"), "--extract"),
                  deid=_need(_p(args.deid) or settings.path("deid_db"), "--deid"),
                  review=_need(_p(args.review_db) or settings.path("review_db"), "--review-db"),
                  settings=settings, desktop=args.desktop)


def cmd_app(args, settings) -> int:
    from nmosd.app.launch import launch_app

    return launch_app(settings, settings_path=_p(args.settings), desktop=args.desktop, port=args.port,
                      window=args.window)


def cmd_download_models(args, settings) -> int:
    from nmosd.app.setup_downloads import download_ner_models

    return download_ner_models(settings)


def cmd_pull_model(args, settings) -> int:
    from nmosd.app.setup_downloads import pull_ollama_model

    return pull_ollama_model(settings, args.model or settings.get("llm.model", "qwen3:8b"))


def cmd_export(args, settings) -> int:
    from nmosd.redcap.export import run_export

    template = None if args.no_template else (_p(args.template) or settings.path("redcap_template"))
    if template and not template.exists():
        raise SystemExit(f"REDCap template not found: {template.name} (set paths.redcap_template, or pass "
                         "--no-template)")
    return run_export(extract=_need(_p(args.extract) or settings.path("extract_db"), "--extract"),
                      review=_need(_p(args.review_db) or settings.path("review_db"), "--review-db"),
                      dictionary=_p(args.dictionary), out_dir=_need(_p(args.out) or settings.path("export_dir"), "--out"),
                      deid=_need(_p(args.deid) or settings.path("deid_db"), "--deid"), settings=settings,
                      template=template)


def cmd_profile_redcap_csv(args, settings) -> int:
    from nmosd.extract.run import resolve_dictionary
    from nmosd.redcap.csv_profile import profile_csv

    path = _need(_p(args.file) or settings.path("redcap_template"), "--file")
    dictionary = None if args.no_dictionary else resolve_dictionary(_p(args.dictionary), sys.stdout)
    profile_csv(path, dictionary)
    return 0


def cmd_reidentify(args, settings) -> int:
    from nmosd.redcap.reidentify import run_reidentify

    return run_reidentify(export_csv=_need(_p(args.export), "--export"),
                          linkage=_need(_p(args.linkage) or settings.path("linkage"), "--linkage"),
                          out_dir=_need(_p(args.out) or settings.path("reidentified_dir"), "--out"),
                          record_map=_p(args.record_map), staging=_p(args.staging) or settings.path("staging_db"),
                          settings=settings, add_identifiers=args.add_identifiers)


def cmd_evaluate(args, settings) -> int:
    from nmosd.eval.compare_gold import run_evaluate

    return run_evaluate(gold=_need(_p(args.gold), "--gold"), predicted=_need(_p(args.predicted), "--predicted"),
                        review=_p(args.review_db) or settings.path("review_db"),
                        linkage=_p(args.linkage), record_map=_p(args.record_map), settings=settings,
                        out=_p(args.report), extract=_p(args.extract), label=args.label or "")


# ------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="nmosd", description="Neurology REDCap Extractor: REDCap extraction (local, offline).")
    ap.add_argument("--settings", help="settings YAML (default: config/settings.yaml or $NMOSD_SETTINGS)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("inspect", cmd_inspect, "metadata-only summary of the source export")
    sp.add_argument("--source")
    sp.add_argument("--backend", default="auto", choices=["auto", "sqlite", "mdbtools", "pyodbc"])
    sp.add_argument("--show-categories", action="store_true", help="also count Report_Description values")

    sp = add("benchmark", cmd_benchmark, "benchmark local Ollama models on synthetic notes")
    sp.add_argument("--models", default="qwen3:8b,gemma3:12b")
    sp.add_argument("--num-ctx", type=int, default=8192)
    sp.add_argument("--sizes", default="500,2000,6000")

    sp = add("stage", cmd_stage, "copy the pilot patients' rows into a staging SQLite (PHI location)")
    sp.add_argument("--source")
    sp.add_argument("--empi-list")
    sp.add_argument("--all-patients", action="store_true",
                    help="stage every patient (for select-pilot); use a separate --out from the pilot staging DB")
    sp.add_argument("--out")
    sp.add_argument("--backend", default="auto", choices=["auto", "sqlite", "mdbtools", "pyodbc"])

    sp = add("select-pilot", cmd_select_pilot, "per-patient volume/keyword table to help choose pilot patients")
    sp.add_argument("--staging")
    sp.add_argument("--out")

    sp = add("suggest-identifiers", cmd_suggest_identifiers,
             "harvest candidate names/MRNs from note headers into a YAML for the user to confirm (never printed)")
    sp.add_argument("--staging")
    sp.add_argument("--out", required=True)

    sp = add("header-shapes", cmd_header_shapes,
             "how notes begin, per table: masked line shapes and counts only (when headers are not recognised)")
    sp.add_argument("--staging")
    sp.add_argument("--lines", type=int, default=6, help="how many leading non-blank lines to show (default 6)")
    sp.add_argument("--min-count", type=int, default=2, help="show a shape only when this many notes share it")

    for name, what in (("shapes", "how the structured data the instruments need (lab results, infusion and medication "
                                  "rows, weight and height, diagnosis rows, disability scores, MRI lines, attack lists, "
                                  "admission dates) is written in every patient's notes, and the columns of the "
                                  "export's structured tables: masked shapes and counts only"),):
        sp = add(name, cmd_lab_shapes, what)
        sp.add_argument("--source", help="the export (default: paths.source): every patient")
        sp.add_argument("--staged", action="store_true", help="search the staging database (--db) instead")
        sp.add_argument("--db", help="a staging or de-identified database (with --staged)")
        sp.add_argument("--min-notes", type=int, default=3, help="show a shape only when this many notes share it")
        sp.add_argument("--min-patients", type=int, default=2, help="... from at least this many patients")
        sp.add_argument("--top", type=int, default=10, help="shapes shown per kind")

    sp = add("lab-shapes", cmd_lab_shapes,
             "how lab results (CD19/20, immunoglobulins, lymphocytes, AQP4, MOG) are written in every patient's notes, "
             "and the test names of the export's laboratory table: masked shapes and counts only")
    sp.add_argument("--source", help="the export (default: paths.source): every patient, and its laboratory table")
    sp.add_argument("--staged", action="store_true", help="search the staging database (--db) instead of the export")
    sp.add_argument("--db", help="a staging or de-identified database (with --staged; default: paths.staging_db)")
    sp.add_argument("--min-notes", type=int, default=3, help="show a shape only when this many notes share it")
    sp.add_argument("--min-patients", type=int, default=2, help="... from at least this many patients")
    sp.add_argument("--top", type=int, default=12, help="shapes shown per kind")

    sp = add("deid", cmd_deid, "de-identify the staging DB")
    sp.add_argument("--staging")
    sp.add_argument("--out")
    sp.add_argument("--linkage")
    sp.add_argument("--identifiers")
    sp.add_argument("--no-ner", action="store_true", help="skip the transformer NER layer (testing only)")
    sp.add_argument("--allow-unconfirmed-identifiers", action="store_true",
                    help="run without a confirmed identifiers.yaml (header-harvested identifiers only)")
    sp.add_argument("--debug-doc", type=int, help="print one document before/after (PHI!)")
    sp.add_argument(PHI_ACK_FLAG, dest="i_understand_this_prints_phi", action="store_true")
    sp.add_argument("--keep-identifiers", action="store_true",
                    help="skip de-identification: copy the notes as they are (also `deid.enabled: false`)")
    sp.add_argument("--switch-mode", action="store_true",
                    help="allow patients prepared the other way last time to switch (their earlier work is redone)")

    sp = add("leak-test", cmd_leak_test, "re-scan de-identified output; exit 1 on any known-identifier hit")
    sp.add_argument("--deid")
    sp.add_argument("--linkage")
    sp.add_argument("--no-ner", action="store_true")
    sp.add_argument("--report", help="the review report path: the spots to check are saved next to it "
                                     "(default: paths.deid_report)")

    sp = add("deid-report", cmd_deid_report, "write the side-by-side HTML review report (PHI location)")
    sp.add_argument("--staging")
    sp.add_argument("--deid")
    sp.add_argument("--linkage")
    sp.add_argument("--out")

    sp = add("deid-signoff", cmd_deid_signoff, "import the reviewer's sign-off JSON into the manifest")
    sp.add_argument("--deid")
    sp.add_argument("--file", required=True)

    sp = add("schemas", cmd_schemas, "generate JSON schemas from the REDCap data dictionary CSV")
    sp.add_argument("--dictionary", required=True)
    sp.add_argument("--out", required=True)

    sp = add("extract", cmd_extract, "stage-1 evidence extraction (resumable)")
    sp.add_argument("--deid")
    sp.add_argument("--out")
    sp.add_argument("--groups", help="comma list of field groups (config/fields/pilot_fields.yaml), e.g. "
                    "diagnosis,relapse,treatment; default: every group")
    sp.add_argument("--llm", choices=["ollama", "synthetic-oracle"], help="default: llm.backend in settings (ollama)")
    sp.add_argument("--model")
    sp.add_argument("--annotations", help="synthetic annotations.json (synthetic-oracle only)")
    sp.add_argument("--dictionary")
    sp.add_argument("--limit-docs", type=int)
    sp.add_argument("--dry-run", action="store_true", help="only report selection counts and token estimates")

    sp = add("reconcile", cmd_reconcile, "stage-2 reconciliation into proposals")
    sp.add_argument("--extract")
    sp.add_argument("--deid")

    sp = add("score", cmd_score, "the latest proposals against reference answers (counts and field names only)")
    sp.add_argument("--extract")
    sp.add_argument("--deid")
    sp.add_argument("--reference", help="folder with Patient_*.json (default: the newest packet_*/Claude_reference "
                    "in the quality-review folder)")
    sp.add_argument("--key", help="the packet key (default: quality_packet_key_<packet>.json next to the "
                    "de-identified database)")
    sp.add_argument("--dictionary")
    sp.add_argument("--fast", action="store_true", help="score the fast test's results file instead of the full one")
    sp.add_argument("--back", type=int, default=0,
                    help="score an earlier Combine run: 1 = the one before the latest (before/after a change)")
    sp.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low",
                    help="count wrong or missed fields the reference rates below this apart")
    sp.add_argument("--list", action="store_true", help="also list every field that is not correct (no values)")

    sp = add("fast-test", cmd_fast_test,
             "extract, combine and score on the reference's fast set of notes only (minutes, not hours)")
    sp.add_argument("--extract", help="the full extraction database; the fast test writes <name>_fast beside it")
    sp.add_argument("--deid")
    sp.add_argument("--reference")
    sp.add_argument("--key")
    sp.add_argument("--groups", help="comma list of field groups to run (default: every group)")
    sp.add_argument("--llm", choices=["ollama", "synthetic-oracle"])
    sp.add_argument("--model")
    sp.add_argument("--annotations")
    sp.add_argument("--dictionary")
    sp.add_argument("--min-confidence", choices=["low", "medium", "high"], default="low")
    sp.add_argument("--list", action="store_true", help="also list every field that is not correct (no values)")

    sp = add("coverage", cmd_coverage, "which codebook fields (1-303) the latest reconcile filled (counts only)")
    sp.add_argument("--extract")
    sp.add_argument("--deid")
    sp.add_argument("--dictionary")

    sp = add("accuracy", cmd_accuracy,
             "accuracy tables from the review decisions (kept, corrected, missed), for a paper (counts only)")
    sp.add_argument("--review-db")
    sp.add_argument("--extract")
    sp.add_argument("--deid")
    sp.add_argument("--dictionary")
    sp.add_argument("--out", help="folder for the dated snapshot folders (default: <reports>/accuracy, PHI location)")
    sp.add_argument("--label", help="name for this run, e.g. the model (qwen3:8b)")

    sp = add("timing", cmd_timing, "where the extraction's time went, per field group (counts and times only)")
    sp.add_argument("--extract")
    sp.add_argument("--deid")
    sp.add_argument("--cohort", type=int, help="also estimate the time for this many patients")

    sp = add("quality-packet", cmd_quality_packet,
             "Safe Harbor-style packet of the proposals and their evidence, for an outside quality check")
    sp.add_argument("--extract")
    sp.add_argument("--deid")
    sp.add_argument("--review-db")
    sp.add_argument("--linkage")
    sp.add_argument("--dictionary")
    sp.add_argument("--out", help="folder for the packet (default paths.quality_packet_dir, else "
                    "~/Documents/NMOSD quality review); a dated subfolder is made in it")
    sp.add_argument("--context", type=int, default=250, help="characters of context around each quote")
    sp.add_argument("--passages", type=int, default=2, help="passages per value")
    sp.add_argument("--ner", action="store_true", help="also scrub what the name-recognition models find (slow)")
    sp.add_argument("--whole-notes", action="store_true",
                    help="also write the whole text of every note, scrubbed and checked the same way, so the "
                         "assessment can work out every REDCap field (user's decision 18.11.26)")
    sp.add_argument("--fast", action="store_true",
                    help="the fast test's results file (<name>_fast) instead of the full one, with no review "
                         "decisions laid over the proposals")
    sp.add_argument("--reference",
                    help="folder with the reference answers (Patient_*.json): the dates then carry the same tokens "
                         "as in the packet those answers were written from (with --fast: found by itself)")

    sp = add("review", cmd_review, "launch the Streamlit review app on 127.0.0.1")
    sp.add_argument("--extract")
    sp.add_argument("--deid")
    sp.add_argument("--review-db")
    sp.add_argument("--desktop", action="store_true",
                    help="as started by the desktop shortcut: open the browser when the app is ready, and keep "
                         "the window open if it fails so the message can be read")

    sp = add("app", cmd_app, "open the guided app (every step, on this computer only)")
    sp.add_argument("--window", action="store_true",
                    help="in its own window called Neurology REDCap Extractor, not a browser tab (as the Mac app and "
                         "the Windows shortcut start it); closing the window closes the app")
    sp.add_argument("--desktop", action="store_true",
                    help="as started by older desktop shortcuts: open the browser when ready; if the app is already "
                         "running, just open the browser")
    sp.add_argument("--port", type=int)

    sp = add("download-models", cmd_download_models,
             "one-time setup: download the de-identification models (pinned versions; needs internet, no PHI)")

    sp = add("pull-model", cmd_pull_model, "one-time setup: download a local AI model into Ollama (needs internet)")
    sp.add_argument("--model", help="default: llm.model in settings")

    sp = add("export", cmd_export, "build the REDCap flat import CSV from reviewed values")
    sp.add_argument("--deid")
    sp.add_argument("--extract")
    sp.add_argument("--review-db")
    sp.add_argument("--dictionary")
    sp.add_argument("--out")
    sp.add_argument("--template", help="the project's own REDCap CSV export: copy its header (columns, order) and "
                                       "file style; only the header line is read (default: paths.redcap_template)")
    sp.add_argument("--no-template", action="store_true", help="REDCap's standard column order instead")

    sp = add("profile-redcap-csv", cmd_profile_redcap_csv,
             "metadata-only layout of a REDCap CSV export (columns, counts, value kinds; never values)")
    sp.add_argument("--file", help="REDCap CSV export (default: paths.redcap_template)")
    sp.add_argument("--dictionary", help="data dictionary CSV to compare against (default: config/redcap/...)")
    sp.add_argument("--no-dictionary", action="store_true")

    sp = add("reidentify", cmd_reidentify, "decrypt linkage, map IDs, un-shift dates (approved environment)")
    sp.add_argument("--export", required=True)
    sp.add_argument("--linkage")
    sp.add_argument("--out")
    sp.add_argument("--record-map", help="CSV with empi,record_id[,mrn] (PHI location)")
    sp.add_argument("--add-identifiers", action="store_true", help="fill the mrn and dob fields")
    sp.add_argument("--staging")

    sp = add("evaluate", cmd_evaluate, "aggregate metrics against an existing REDCap export")
    sp.add_argument("--gold", required=True)
    sp.add_argument("--predicted", required=True, help="export CSV from `nmosd export` (study-ID space)")
    sp.add_argument("--review-db")
    sp.add_argument("--linkage", help="de-identify the gold file with this linkage key")
    sp.add_argument("--record-map")
    sp.add_argument("--report", help="write a markdown report here (PHI location)")
    sp.add_argument("--extract", help="extraction DB (for not-established / conflict shares)")
    sp.add_argument("--label", help="name for this run, e.g. the model (qwen3:8b)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_file = None
    if getattr(args, "window", False):    # often no terminal at all: prints go to a log file instead
        from nmosd.app.window import ensure_streams

        log_file = ensure_streams()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stderr)
    if getattr(args, "window", False):
        return _run_windowed(args, log_file)
    if not getattr(args, "desktop", False):
        return _run(args)
    # Started from the desktop shortcut: the console window closes the moment we return, so on failure
    # show what went wrong (as a terminal would) and wait for Enter.
    try:
        rc = _run(args)
    except SystemExit as e:
        if e.code is not None and not isinstance(e.code, int):
            print(e.code, file=sys.stderr)
        rc = e.code if isinstance(e.code, int) else int(e.code is not None)
    except Exception:
        traceback.print_exc()
        rc = 1
    if rc:
        with contextlib.suppress(EOFError):
            input("\nThe app stopped with an error (see above). Press Enter to close this window.")
    return rc


def _run_windowed(args, log_file: Path | None) -> int:
    """`nmosd app --window`, often started with no terminal (the Mac app, pythonw on Windows): a failure is shown in
    a dialog instead of disappearing."""
    from nmosd.app import window as win

    try:
        rc = _run(args)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
        if e.code is not None and not isinstance(e.code, int):
            win.message(str(e.code), error=True)
    except Exception as e:
        traceback.print_exc()
        where = f"\n\nDetails: {log_file or win.log_dir()}" if log_file or not win.has_console() else ""
        win.message(f"{win.APP_NAME} stopped with an error ({type(e).__name__}).{where}", error=True)
        rc = 1
    return rc


def _run(args) -> int:
    settings = load_settings(args.settings)
    try:
        return int(args.fn(args, settings) or 0)
    except SafetyError as e:
        print(f"SAFETY: {e}", file=sys.stderr)
        return 3
    except FileNotFoundError as e:
        print(f"Not found: {e}", file=sys.stderr)
        return 2
