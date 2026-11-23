"""Streamlit review app (localhost only). Launch with `nmosd review`.

Step 1 for every patient is the diagnosis check (NMOSD confirmed, enough follow-up); the REDCap instruments
unlock only after it, and an excluded patient needs no further review.
Left: patients, review progress, instruments, outstanding conflicts.
Centre: proposed values (REDCap labels shown, codes stored), Accept / Correct / Unresolved, comments,
        add/delete repeating instances.
Right: source evidence with the quote highlighted, other evidence, notes that repeat the passage,
       keyword search with date/type filters and chronological browsing.
Every action is appended to the review DB at once; nothing re-runs the model.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from nmosd.redcap.schemas import (
    NMOSD_RELATED,
    NO_CATEGORY,
    SOURCE_COLUMN_NAMES,
    STRUCTURED_ONLY,
    STRUCTURED_SOURCES,
)
from nmosd.redcap.sources import form_label, not_filled, repeating_forms, source
from nmosd.review import code_changed_since_start
from nmosd.review.store import GATE, INSTRUMENT_FIELDS, ReviewStore, gate_labels, gate_rules

NOTE_HEIGHT = 560


def _args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", required=True)
    ap.add_argument("--deid", required=True)
    ap.add_argument("--review", required=True)
    ap.add_argument("--dictionary")
    return ap.parse_args(sys.argv[1:])


def _fmt(store: ReviewStore, field: str, value: str | None) -> str:
    if value in (None, ""):
        return "—"
    labels = store.labels(field)
    if field in store.dictionary and store.dictionary[field].is_checkbox:
        try:
            codes = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            codes = [value]
        return ", ".join(f"{c} · {labels.get(str(c), '?')}" for c in codes) or "—"
    return f"{value} · {labels[value]}" if value in labels else str(value)


def _highlight(text: str, start: int | None, end: int | None, spans: list | None = None) -> str:
    spans = sorted(spans or ([(start, end)] if start is not None and end is not None else []))
    parts, pos = [], 0
    for n, (a, b) in enumerate(spans):
        if a < pos:
            continue
        parts += [html.escape(text[pos:a]), f"<mark{' id=hit' if n == 0 else ''}>", html.escape(text[a:b]), "</mark>"]
        pos = b
    body = "".join(parts) + html.escape(text[pos:])
    return ("<div style='white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:12.5px;"
            f"max-height:{NOTE_HEIGHT}px;overflow:auto;border:1px solid #8884;border-radius:6px;padding:8px'>" + body
            + "</div>")


def note_html(text: str, start: int | None, end: int | None) -> str:
    """The note in its own scroll box, already scrolled to the highlighted quote (a third of the way down), so
    ◀ / ▶ show the passage at once. Only the box scrolls, never the page."""
    if start is None or end is None:
        body = html.escape(text)
    else:
        body = (html.escape(text[:start]) + "<mark id='hit'>" + html.escape(text[start:end]) + "</mark>"
                + html.escape(text[end:]))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
:root {{ color-scheme: light dark; --fg: #31333f; --border: #d5d6da; --mark: #fff176; --mark-fg: #000; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fg: #fafafa; --border: #4a4c57; --mark: #b59f00; --mark-fg: #000; }} }}
html, body {{ margin: 0; background: transparent; }}
#note {{ position: relative; box-sizing: border-box; height: 100vh; overflow: auto; white-space: pre-wrap;
  font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12.5px; line-height: 1.45; color: var(--fg);
  border: 1px solid var(--border); border-radius: 6px; padding: 8px; }}
mark {{ background: var(--mark); color: var(--mark-fg); }}
</style></head><body><div id="note">{body}</div><script>
const box = document.getElementById("note"), hit = document.getElementById("hit");
if (hit) box.scrollTop = Math.max(0, hit.offsetTop - box.clientHeight / 3);
</script></body></html>"""


def field_title(store: ReviewStore, field: str) -> str:
    """'45. Attack Date (date of symptom onset)': the codebook's number and label (16.11.26)."""
    dic = store.dictionary
    if field not in dic:
        return field
    f = dic[field]
    label = f.clean_label
    if not label:   # an estimate radio has no label of its own: name it after its date
        date_f = next((d for d in dic.fields.values() if field in d.piped), None)
        label = f"Precision of {date_f.clean_label.lower()}" if date_f else field
    return f"{f.number}. {label}"


def field_editor(store: ReviewStore, reviewer: str, fs, key_prefix: str, readonly: bool = False) -> None:
    labels = store.labels(fs.field)
    dic = store.dictionary
    c1, c2 = st.columns([3, 2])
    badge = {"pending": "⏳", "accepted": "✅", "corrected": "✏️", "unresolved": "❓"}[fs.status]
    title = field_title(store, fs.field) if fs.instrument != GATE else fs.field
    c1.markdown(f"**{title}** {badge} — proposed: `{_fmt(store, fs.field, fs.proposed)}`"
                + (f" · current: `{_fmt(store, fs.field, fs.value)}`" if fs.value != fs.proposed else ""))
    if fs.instrument != GATE and fs.field in dic:
        kind, why = source(dic, fs.field, store.hospital_names)
        tags = [f"`{fs.field}`"]
        if dic[fs.field].is_hidden:
            tags.append("hidden on the REDCap form")
        if kind == "derived":
            tags.append(f"worked out from {why}")
        c1.caption(" · ".join(tags))
    passages = store.passage_count(fs.evidence_ids, fs.study_id) if fs.evidence_ids else 0
    if passages and c2.button(f"Evidence ({passages})", key=f"{key_prefix}ev"):
        st.session_state["evidence_ids"] = fs.evidence_ids
        st.session_state["evidence_idx"] = 0
        st.session_state["evidence_desc"] = fs.instrument == GATE   # diagnosis statements: newest first
    elif fs.field == "followup_12m":
        c2.caption("Computed from the diagnosis date and the last contact (see the summary above).")
    elif fs.field in STRUCTURED_ONLY:
        col = SOURCE_COLUMN_NAMES[STRUCTURED_SOURCES[fs.field]]
        c2.caption(f"From the structured Demographics table ({col}; not checked against the notes)."
                   + (" ⚠️ See the note above." if fs.conflict else ""))
    elif not passages and fs.proposed is not None:
        src = "Structured value. " if fs.field in STRUCTURED_SOURCES else ""
        c2.caption(f"⚠️ {src}No note evidence found: search the notes before accepting.")
    if readonly:
        return
    with st.expander("Review", expanded=False):
        f = dic.get(fs.field)
        if f is not None and f.note:
            st.caption(f"Codebook note: {f.note}")
        if f is not None and f.is_checkbox:
            try:
                cur = json.loads(fs.value) if fs.value else []
            except json.JSONDecodeError:
                cur = []
            new = st.multiselect("Codes", list(labels), default=[c for c in cur if c in labels],
                                 format_func=lambda c: f"{c} · {labels.get(c, '')}", key=f"{key_prefix}ms")
            new_value = json.dumps(sorted(new))
        elif labels:
            opts = [*labels] if fs.instrument == GATE else [*labels, "UNK", "NA"]
            idx = opts.index(fs.value) if fs.value in opts else 0
            new_value = st.selectbox("Value", opts, index=idx, format_func=lambda c: f"{c} · {labels.get(c, c)}",
                                     key=f"{key_prefix}sb")
        else:
            new_value = st.text_input("Value (YYYY-MM-DD)" if f is not None and f.is_date else "Value",
                                      value=fs.value or "", key=f"{key_prefix}ti")
        reason = st.text_input("Reason / comment", key=f"{key_prefix}rs")
        a, b, c, d = st.columns(4)
        try:   # the store refuses e.g. excluding a borderline case without a reason
            if a.button("Accept", key=f"{key_prefix}acc"):
                store.record(reviewer, "accept", fs.study_id, fs.instrument, fs.instance_key, fs.field,
                             reason=reason or None)
                st.rerun()
            if b.button("Correct", key=f"{key_prefix}cor"):
                att = st.session_state.get("attach")
                ev = att[1:] if att and att[0] == fs.study_id else None
                store.record(reviewer, "correct", fs.study_id, fs.instrument, fs.instance_key, fs.field,
                             new_value=new_value, reason=reason or None, evidence_doc_id=ev[0] if ev else None,
                             evidence_start=ev[1] if ev else None, evidence_end=ev[2] if ev else None)
                st.rerun()
        except ValueError as e:
            st.error(str(e))
        if c.button("Unresolved", key=f"{key_prefix}unr"):
            store.record(reviewer, "unresolved", fs.study_id, fs.instrument, fs.instance_key, fs.field,
                         reason=reason or None)
            st.rerun()
        if d.button("Comment", key=f"{key_prefix}com") and reason:
            store.record(reviewer, "comment", fs.study_id, fs.instrument, fs.instance_key, fs.field, reason=reason)
            st.rerun()


def evidence_panel(store: ReviewStore, study_id: str) -> None:
    st.subheader("Source evidence")
    ev = store.evidence(st.session_state.get("evidence_ids", []), study_id,
                        newest_first=st.session_state.get("evidence_desc", False))
    if ev:
        i = min(st.session_state.get("evidence_idx", 0), len(ev) - 1)
        nav1, nav2, nav3 = st.columns([1, 3, 1])
        if nav1.button("◀", key="evprev") and i > 0:
            st.session_state["evidence_idx"] = i - 1
            st.rerun()
        nav2.caption(f"Evidence {i + 1} of {len(ev)} · verified: {'yes' if ev[i]['verified'] else 'NO'}")
        if nav3.button("▶", key="evnext") and i < len(ev) - 1:
            st.session_state["evidence_idx"] = i + 1
            st.rerun()
        e = ev[i]
        doc = store.document(e["doc_id"], study_id)
        if doc:
            st.caption(f"{doc['report_date']} · {doc['report_description']} · {doc['source_table']} · {doc['doc_id']}")
            span = store.highlight_span(e, doc["text"])
            if span is None and e.get("quote"):
                st.warning("The quoted text is not in this note as it is now (it may have been de-identified "
                           f"again since extraction): “{e['quote']}”")
            components.html(note_html(doc["text"], *(span or (None, None))), height=NOTE_HEIGHT)
            if st.button("Attach this passage to my next correction", key="attach_btn"):
                st.session_state["attach"] = (study_id, e["doc_id"], e["char_start"], e["char_end"])
            reps = store.repeats_of(e["doc_id"], e["char_start"], e["char_end"], study_id)
            if reps:
                st.caption("The same paragraph is copied into: " + ", ".join(sorted({r["doc_id"] for r in reps})))
    else:
        st.caption("Click an Evidence button to see the supporting text.")
    st.divider()
    st.subheader("Search this patient's notes")
    q = st.text_input("Words or phrase", key="q")
    c1, c2 = st.columns(2)
    d_from = c1.text_input("From (YYYY-MM-DD)", key="dfrom")
    d_to = c2.text_input("To (YYYY-MM-DD)", key="dto")
    types = st.multiselect("Types", ["VisitNote", "ProgressNote", "Radiology", "Pathology", "Discharge_Summaries",
                                     "HistoryAndPhysical"], key="types")
    results = store.search(study_id, q, d_from or None, d_to or None, types or None)
    mode = results[0]["match"] if results else ""
    if q.strip() and not results:
        st.caption("No note contains these words. Try fewer or shorter words (e.g. “infarct” for infarction).")
    elif mode == "all words":
        st.caption(f"No note has these words together; {len(results)} notes contain every word (chronological)")
    else:
        st.caption(f"{len(results)} notes (chronological)")
    for r in results[:50]:
        label = f"{r['report_date']} · {r['report_description']} · {len(r.get('hits', []))} hits"
        with st.expander(label):
            doc = store.document(r["doc_id"], study_id)
            st.markdown(_highlight(doc["text"], None, None, r.get("hits")), unsafe_allow_html=True)


def scope_to_patient(study_id: str) -> None:
    """Selections made while reviewing one patient (evidence shown, passage attached) never carry over, and
    every patient opens on the diagnosis check."""
    if st.session_state.get("shown_patient") != study_id:
        for k in ("evidence_ids", "evidence_idx", "evidence_desc", "attach", "instrument"):
            st.session_state.pop(k, None)
        st.session_state["shown_patient"] = study_id


GATE_BANNER = {"included": st.success, "excluded": st.error, "unresolved": st.warning, "pending": st.info}


def gate_panel(store: ReviewStore, reviewer: str, sid: str) -> None:
    """Step 1: is this NMOSD, with enough follow-up? Proposal, summary of the notes, then the two decisions."""
    rules, labels = gate_rules(), gate_labels()
    g = store.gate(sid)
    st.subheader("Diagnosis check · Does this patient have NMOSD?")
    months = rules.get("min_followup_months", 12)
    text = {"included": "Included: NMOSD confirmed with enough follow-up. The other instruments are unlocked.",
            "excluded": f"Excluded ({g.reason}). This patient will not be exported and needs no further review.",
            "unresolved": "Marked unresolved. The other instruments stay locked until the check is decided.",
            "pending": f"Confirm the diagnosis first. The other instruments unlock once NMOSD is confirmed with at "
                       f"least {months:g} months of follow-up."}[g.status]
    GATE_BANNER[g.status](text)
    borderline = store.gate_borderline(sid)
    if borderline:
        st.warning("**Borderline case: look at the notes yourself before deciding.** Excluding it needs a written "
                   "reason. Tick “Look at the other instruments” on the left to see the rest of the record "
                   "read-only.\n\n" + "\n".join(f"- {b}" for b in borderline))
    with st.expander("Criteria (clinical team, draft)", expanded=g.status == "pending"):
        st.markdown("\n".join(f"- {c}" for c in rules.get("criteria", [])))
    s = store.gate_summary(sid)
    if s:
        cats = labels["dx_category"]
        rows = ["| Diagnosis stated | Documents | First | Latest |", "|---|---|---|---|"]
        rows += [f"| {cats.get(x['category'], x['category'])} | {x['documents']} | {x['first'] or '—'} | "
                 f"{x['last'] or '—'} |" for x in s.get("statements", [])]
        st.markdown(f"**What the notes say** (from {s.get('basis', '—')}; copied-forward text counts at its latest "
                    "note)")
        st.markdown("\n".join(rows) if s.get("statements") else "No diagnosis statement found.")
        if s.get("coexisting"):
            st.caption("Also stated recently, may co-exist with the main diagnosis: "
                       + ", ".join(cats.get(c, c) for c in s["coexisting"]))
        if s.get("ruled_out"):
            st.caption("Notes saying the patient does NOT have: " + ", ".join(cats.get(c, c) for c in s["ruled_out"]))
        if s.get("possible_only"):
            st.caption("Only mentioned as possible/suspected: " + ", ".join(cats.get(c, c) for c in s["possible_only"]))
        ab = s.get("antibodies", {})
        st.caption("Antibody results: " + " · ".join(
            f"{name}-IgG {', '.join(f'{k} {v}' for k, v in sorted(ab.get(key, {}).items())) or 'none found'}"
            for key, name in (("aqp4", "AQP4"), ("mog", "MOG"))))
        fu = s.get("followup", {})
        st.caption(f"Follow-up: {fu.get('from') or '?'} ({fu.get('from_source') or 'no start date'}) to "
                   f"{fu.get('to') or '?'} ({fu.get('to_source') or 'last contact'}) = "
                   f"{fu.get('months') if fu.get('months') is not None else '?'} months; needs {fu.get('min_months', months):g}.")
    for fs in store.fields(sid, GATE, GATE):
        field_editor(store, reviewer, fs, f"{sid}:{GATE}:{fs.field}:")


def instrument_panel(store: ReviewStore, reviewer: str, sid: str, instrument: str, readonly: bool = False) -> None:
    dic = store.dictionary
    st.subheader(form_label(dic, instrument))
    repeating = instrument in repeating_forms(dic)
    if readonly:
        st.info("Read-only preview: decide the diagnosis check to review these values.")
    elif repeating and st.button("➕ Add instance (missed episode)", key="add_inst"):
        store.add_instance(reviewer, sid, instrument, reason="added by reviewer")
        st.rerun()
    skipped = not_filled(dic, instrument, store.hospital_names)
    if skipped:
        with st.expander(f"Fields not proposed here ({len(skipped)}): filled by REDCap or by you"):
            st.markdown("\n".join(f"- **{n}.** `{f}`: {why}" for n, f, why in skipped))
    keys = store.instances(sid, instrument)
    if not keys:
        st.caption("Nothing found in the notes for this instrument." + (" Add an instance if the notes describe one."
                                                                         if repeating and not readonly else ""))
    for n, key in enumerate(keys, 1):
        fields = store.fields(sid, instrument, key)
        shown = store.visible(fields)
        title = fields[0].anchor or key if fields else key
        # demographics fields carry their own notes (sex, race, ...: a fallback column or a missing value)
        conflict = "; ".join(dict.fromkeys(f.conflict for f in fields if f.conflict))
        with st.container(border=True):
            h1, h2 = st.columns([4, 1])
            h1.markdown(f"**#{n}** · {title}" + (f" · ⚠️ {conflict}" if conflict else ""))
            if not readonly and repeating and h2.button("Delete", key=f"del{key}"):
                store.record(reviewer, "delete_instance", sid, instrument, key, reason="deleted by reviewer")
                st.rerun()
            for note in store.info(sid, instrument, key):
                st.caption(f"ℹ️ {note}")
            if len(shown) < len(fields):
                st.caption(f"{len(fields) - len(shown)} questions REDCap does not ask for these answers are hidden.")
            for fs in shown:
                # Widget keys include the patient: demographics has the same instance key for everyone.
                field_editor(store, reviewer, fs, f"{sid}:{key}:{fs.field}:", readonly=readonly)
                if fs.field == "comorbidities":
                    conditions_table(store, sid)


def conditions_table(store: ReviewStore, sid: str) -> None:
    """Every condition the notes list, with the REDCap category it was given; those outside the categories
    are shown too, so nothing in the medical history is silently dropped."""
    conds = store.conditions(sid)
    if not conds:
        return
    cats, autos = store.labels("comorbidities"), store.labels("autoimmune")
    rows = ["| Condition (as written) | REDCap category | Notes | Latest |", "|---|---|---|---|"]
    for c in conds:
        cat = ("not a REDCap category" if c["category"] == NO_CATEGORY
               else "part of NMOSD (not counted)" if c["category"] == NMOSD_RELATED
               else f"{c['category']} · {cats.get(c['category'], '?')}")
        if c.get("autoimmune"):
            cat += f" ({autos.get(c['autoimmune'], c['autoimmune'])})"
        name = c["name"].replace("|", "/")
        rows.append(f"| {name} | {cat} | {c['documents']} | {c.get('last') or '—'} |")
    with st.expander(f"Conditions found in the notes ({len(conds)})"):
        st.markdown("\n".join(rows))


def main() -> None:
    a = _args()
    st.set_page_config(page_title="NMOSD review", layout="wide")
    render(Path(a.extract), Path(a.deid), Path(a.review), Path(a.dictionary) if a.dictionary else None)


def render(extract: Path, deid: Path, review: Path, dictionary: Path | None) -> None:
    """The review screens (also a page of the guided app, `nmosd app`)."""
    if code_changed_since_start():
        st.error("The review code was updated after this app started. Stop it (Ctrl+C in its terminal) and start it "
                 "again before continuing.")
        st.stop()
    if not extract.exists() or not deid.exists():
        st.title("NMOSD extraction review")
        st.info("Nothing to review yet: run the extraction first (step 7 of the guided app, or `nmosd extract` "
                "then `nmosd reconcile`).")
        st.stop()
    if not st.session_state.get("reviewer"):
        st.title("NMOSD extraction review")
        name = st.text_input("Reviewer name", key="login_name")
        if st.button("Start reviewing", key="login") and name.strip():
            st.session_state["reviewer"] = name.strip()
            st.rerun()
        st.stop()
    reviewer = st.session_state["reviewer"]
    store = ReviewStore(review, extract, deid, dictionary)
    try:
        left, centre, right = st.columns([1, 2.2, 2.2])
        with left:
            st.caption(f"Reviewer: {reviewer}")
            if not store.notes_deidentified():
                st.caption("🔒 Original notes (not de-identified): real names and dates. Keep this screen private.")
            pts = store.patients()
            sid = st.selectbox("Patient", pts, key="patient")
            scope_to_patient(sid)
            with st.expander("All patients' progress"):
                for p in pts:
                    d, t = store.progress(p)
                    flag = " · ⚠️ borderline" if store.gate_borderline(p) else ""
                    st.caption(f"{p}: {d}/{t} · diagnosis check {store.gate(p).status}{flag}")
            gate = store.gate(sid)
            preview = False
            if not gate.included and (store.gate_borderline(sid) or gate.status == "unresolved"):
                # a borderline case: the reviewer may look at the whole record before deciding
                preview = st.checkbox("Look at the other instruments (read-only)", key=f"{sid}:preview")
            options = [GATE, *INSTRUMENT_FIELDS] if gate.included or preview else [GATE]
            if st.session_state.get("instrument") not in options:
                st.session_state["instrument"] = GATE
            instrument = st.radio("Instrument", options, key="instrument",
                                  format_func=lambda i: "Diagnosis check" if i == GATE else form_label(store.dictionary, i))
            if not gate.included:
                st.caption("Excluded: nothing else to review." if gate.status == "excluded" else
                           "The REDCap instruments unlock for review once the diagnosis check confirms NMOSD.")
            done, total = store.progress(sid)
            st.progress(done / total if total else 0.0, text=f"{done}/{total} fields reviewed")
            conf = store.conflicts(sid)
            if conf:
                st.markdown("**Outstanding conflicts**")
                for inst, _key, note in conf:
                    st.caption(f"{inst}: {note}")
        with centre:
            if instrument == GATE:
                gate_panel(store, reviewer, sid)
            else:
                instrument_panel(store, reviewer, sid, instrument, readonly=not gate.included)
        with right:
            evidence_panel(store, sid)
    finally:
        store.close()


if __name__ == "__main__":
    main()
