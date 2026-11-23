"""`nmosd quality-packet`: a HIPAA Safe Harbor-style review packet of the proposals and their evidence, for an
outside quality assessment (Claude), 16.11.26.

The user approved Claude assessing the quality of the proposals, on condition that it is HIPAA compliant. Whether a
disclosure is compliant is decided by the covered entity (MGB's privacy office / the IRB), not by this code. What
the code does is build the packet to the Safe Harbor standard (45 CFR 164.514(b)(2)) on top of the de-identification
the user already signed off, and refuse to write anything it cannot check:

* only de-identified notes (never a patient processed with `deid --keep-identifiers`), and only after the human
  sign-off of the de-identification (`signoff_complete` in the manifest);
* by default never whole notes: for each proposed value, the evidence passages with a little context, and for an
  empty field a few passages from notes its field group read that contain its keywords;
* `--whole-notes` (the user's decision of 18.11.26, so Claude can work out what every REDCap field should have been):
  also the whole text of every note of each patient, in date order, in `Patient_A_notes_01.md`, ... It goes through
  the same scrubbing and the same final checks as the passages, with the same date tokens and note numbers;
* dates: every element but the year is removed. A date becomes a token such as `[[2019 D05 month-year]]`; the
  same date is always the same token within one patient, so a value can still be checked against its quote, but no
  day or month is left. Ages over 89 become "90+";
* identifiers: patients become "Patient A", "Patient B", ... and notes "Note 1", "Note 2", ... (Safe Harbor does
  not allow codes derived from the patient's identifiers, as the study IDs are). The key back to study IDs and
  document IDs is written to the PHI location, never to the packet;
* every passage is scrubbed by the de-identification's pattern layer (phone, email, URL, ID numbers, ...), any
  number of 7 or more digits, and optionally (`--ner`) the name-recognition models; then the known identifiers of every
  patient (the linkage key) are searched in the whole packet. Any hit, or any date left, and nothing is written;
* stdout: counts only.
"""

from __future__ import annotations

import json
import re
import string
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

from nmosd.deid.date_shift import find_dates
from nmosd.deid.known_identifiers import IdentifierSet, KnownMatcher
from nmosd.deid.linkage import get_passphrase, load_linkage
from nmosd.deid.patterns import PatternLayer
from nmosd.deid.pipeline import is_allowlisted
from nmosd.deid.study_ids import ALPHABET as ID_ALPHABET
from nmosd.deid.surrogates import TOKEN_RX
from nmosd.extract.prefilter import field_keyword_rx, group_specs, is_candidate, keyword_rx
from nmosd.redcap.dictionary import default_dictionary_path
from nmosd.redcap.sources import _note_fields, form_label, repeating_forms, source
from nmosd.review.store import GATE, INSTRUMENT_FIELDS, ReviewStore
from nmosd.safety import ensure_outside_repo, ensure_phi_output
from nmosd.store.db import connect, get_meta
from nmosd.textproc.dates import _mention_iso

PACKET_TOKEN_RX = re.compile(r"\[\[[^\]]*\]\]")
AGE_RX = re.compile(r"(?i)\b(9\d|1[0-2]\d)(\s*(?:-|\s)?\s*(?:years?|yrs?|y/?o|yo)\b(?:[- ]old)?|\s*(?:M|F)\b)")
AGE_WORD_RX = re.compile(r"(?i)\b(age[ds]?:?\s*)(9\d|1[0-2]\d)\b")
LONG_NUMBER_RX = re.compile(r"(?<![\w.])\d[\d\- ]{4,}\d(?![\w.])")
DATE_KIND_LABEL = {"full": "", "md": "", "my": "month-year", "short_ambiguous": "month-year", "season": "season",
                   "holiday": "holiday"}
NOTES_PART_CHARS = 150_000      # whole-notes files are split at note boundaries near this size, so each can be read
FENCE = "~~~~"
# Anything shaped exactly like a study or document ID (deid/study_ids.py: NMO- + 6, DOC- + 10 from its alphabet).
# The real IDs are also searched for one by one. Clinical words such as "NMO-SPECTRUM" or "NMO-ANTIBODY" (all-caps
# notes) are not IDs: the alphabet has no S, O, I, L, B or Z (18.11.26; the looser `NMO-[A-Z0-9]{4,}` blocked a packet).
ID_SHAPE_RX = re.compile(rf"(?<![A-Za-z0-9])(?:NMO-[{ID_ALPHABET}]{{6}}|DOC-[{ID_ALPHABET}]{{10}})(?![A-Za-z0-9])")


def _mask(s: str) -> str:
    """The shape of a hit, for the refusal message: letters → A, digits → 9; never its value."""
    prefix, _, rest = s.partition("-")
    return f"{prefix}-" + re.sub(r"\d", "9", re.sub(r"[A-Za-z]", "A", rest))


WITHHELD = "[withheld: the label of this answer is on the known-identifier list]"


def _kind_of(line: str) -> str | None:
    m = re.match(r"- \*\*#\S+ (\w+)\*\*", line) or re.match(r"- (\w+): proposed", line)
    if m:
        return f"the value line of {m.group(1)}"
    for start, kind in (("How derived:", "a how-derived line"), ("Conflict noted:", "a conflict line"),
                        ("#", "a heading"), ("- possible source", "a passage shown for an empty field")):
        if line.startswith(start):
            return kind
    if re.match(r"- \[P\d+\]", line):
        return "an evidence passage"
    if re.match(r"- Note \d+", line):
        return "a passage of a note that no instance cites"
    if re.match(r"- .*: category \S+, \d+ notes$", line):
        return "a condition name"
    return None


def _line_kind(text: str, pos: int) -> str:
    """Where position `pos` is, for the refusal message: the kind of line (the field name for a value line), whether
    it is a later line of a passage, and the section heading. Never the text of the line."""
    start = text.rfind("\n", 0, pos) + 1
    head = text.rfind("\n## ", 0, pos)
    section = text[head + 4:text.find("\n", head + 4)].split(" (")[0] if head >= 0 else "the top of the file"
    kind, later = None, False
    for _ in range(200):
        end = text.find("\n", start)
        kind = _kind_of(text[start:end if end >= 0 else len(text)].strip())
        if kind or not start:
            break
        later = True
        start = text.rfind("\n", 0, start - 1) + 1
    return f"{kind or 'another line'}{' (a later line of it)' if kind and later else ''}, under '{section}'"


class PacketRefused(Exception):
    """A check failed: nothing was written."""


class DateTokens:
    """One patient's dates → tokens. The same calendar date is always the same token; tokens are numbered in date
    order, so the order of events stays visible; only the year is shown.

    seed: {date: id} from an earlier packet's key. Those dates keep that packet's numbers, so answers written from
    that packet can be laid next to this one. A date the earlier packet did not have gets the number of the last
    seeded date before it and a letter: D05a falls after D05 and before D06 (D00a: before every seeded date)."""

    def __init__(self, dates: list[str], seed: dict[str, str] | None = None):
        if not seed:
            self._seeded = None
            self.ids = {d: f"{n:02d}" for n, d in enumerate(sorted(set(dates)), 1)}
            return
        self.ids = dict(seed)
        self._seeded = sorted((d, _id_number(i)) for d, i in seed.items())
        for d in sorted({x[:10] for x in dates} - set(self.ids)):
            self._add(d)

    def _add(self, iso: str) -> str:
        if self._seeded is None:
            ident = f"{len(self.ids) + 1:02d}"
        else:
            base = max((n for d, n in self._seeded if d < iso), default=0)
            used, k = set(self.ids.values()), 0
            while f"{base:02d}{_letters(k).lower()}" in used:
                k += 1
            ident = f"{base:02d}{_letters(k).lower()}"
        self.ids[iso] = ident
        return ident

    def token(self, iso: str, label: str = "") -> str:
        ident = self.ids.get(iso[:10]) or self._add(iso[:10])
        return f"[[{iso[:4]} D{ident}{' ' + label if label else ''}]]"

    def key(self) -> dict[str, str]:
        """{"D05": date}: for the packet key (PHI folder only)."""
        return {f"D{i}": iso for iso, i in sorted(self.ids.items(), key=lambda x: (_id_number(x[1]), x[1]))}


def _id_number(ident: str) -> int:
    m = re.match(r"\d+", ident)
    return int(m.group(0)) if m else 0


def seeds_from_key(key: dict) -> dict[str, dict[str, str]]:
    """{study ID: {date: token id}} from an earlier packet's key, to number this packet's dates the same way."""
    return {e["study_id"]: {iso: tok.lstrip("D") for tok, iso in (e.get("dates") or {}).items()}
            for e in (key.get("patients") or {}).values() if e.get("study_id")}


def _date_mentions(text: str, pivot: int):
    for m in find_dates(text or "", pivot)[0]:
        if m.kind in ("year", "year_range"):
            continue        # a year alone is allowed
        try:
            got = _mention_iso(m, pivot)
        except (AttributeError, KeyError, TypeError, ValueError):
            got = None
        yield m, got


def collect_dates(texts: list[str], pivot: int) -> list[str]:
    return [got[0] for t in texts for _, got in _date_mentions(t, pivot) if got]


class Scrubber:
    def __init__(self, settings, use_ner: bool = False):
        self.patterns = PatternLayer(bool(settings.get("deid.tokenise_hospitals", True)),
                                     tuple(settings.get("deid.mrn_digit_lengths", [7, 8, 9, 10])))
        self.ensemble = None
        if use_ner:
            from nmosd.deid.ner_models import build_ensemble

            self.ensemble = build_ensemble(settings)
        self.counts: dict[str, int] = defaultdict(int)
        self.known: list[KnownMatcher] = []      # set by build_packet: every patient's list

    def _replacements(self, text: str, dates: DateTokens, pivot: int) -> list[tuple[int, int, str]]:
        """[(start, end, replacement)] in order, none overlapping."""
        spans: list[tuple[int, int, str]] = []
        for m, got in _date_mentions(text, pivot):
            if got:
                spans.append((m.start, m.end, dates.token(got[0], DATE_KIND_LABEL.get(m.kind, ""))))
            else:
                spans.append((m.start, m.end, "[[date]]"))
            self.counts["dates"] += 1
        keep = [(m.start(), m.end()) for m in TOKEN_RX.finditer(text)]
        for h in self.patterns.find(text):
            frag = text[h.start:h.end]
            if not is_allowlisted(frag) and not any(a <= h.start < b for a, b in keep):
                spans.append((h.start, h.end, f"[REDACTED {h.category}]"))
                self.counts["pattern residuals"] += 1
        if self.ensemble is not None:
            for sp in self.ensemble.predict(text).spans:
                frag = text[sp.start:sp.end]
                if sp.label in ("DATE", "AGE") or is_allowlisted(frag) or any(a <= sp.start < b for a, b in keep):
                    continue
                spans.append((sp.start, sp.end, f"[REDACTED {sp.label}]"))
                self.counts["name-recognition residuals"] += 1
        for m in LONG_NUMBER_RX.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            # phone, SSN, account-like numbers; not exam tables ("5 5 5 5 4") or ordinary lab values
            if len(digits) >= 7 and re.search(r"\d{3}", m.group(0)):
                spans.append((m.start(), m.end(), "[REDACTED number]"))
                self.counts["long numbers"] += 1
        for m in AGE_RX.finditer(text):
            spans.append((m.start(1), m.end(1), "90+"))
            self.counts["ages over 89"] += 1
        for m in AGE_WORD_RX.finditer(text):
            spans.append((m.start(2), m.end(2), "90+"))
            self.counts["ages over 89"] += 1
        chosen, pos = [], 0
        for a, b, rep in sorted(spans, key=lambda s: (s[0], -(s[1] - s[0]))):
            if a < pos:
                continue
            chosen.append((a, b, rep))
            pos = b
        return chosen

    def own_text(self, text: str, dates: DateTokens, pivot: int) -> str:
        """`scrub` for a short text the tool or the model wrote (a free-text value, a condition name, a how-derived
        or conflict line): a word that is also on the known-identifier list is replaced too, and counted by kind.
        The model can write a word the way a listed name is written (an eponym with its capital letter) where the
        note does not. Note passages are NOT treated this way: a listed name in a passage still stops the packet."""
        out = self.scrub(text, dates, pivot)
        keep = [(m.start(), m.end()) for m in BRACKET_RX.finditer(out)]
        hits = sorted({(h.start, h.end, h.category) for m in self.known for h in m.find(out)
                       if not any(a < h.end and h.start < b for a, b in keep)}, key=lambda h: (h[0], -h[1]))
        parts, pos = [], 0
        for a, b, category in hits:
            if a < pos:
                continue
            parts += [out[pos:a], f"[REDACTED {category}]"]
            pos = b
            self.counts[f"listed names removed from the tool's own text ({category.lower()})"] += 1
        return "".join(parts) + out[pos:]

    def scrub(self, text: str, dates: DateTokens, pivot: int) -> str:
        """Dates → tokens, ages over 89 → 90+, pattern/NER residuals and long numbers → [REDACTED]."""
        return self.scrub_mapped(text, dates, pivot)[0] if text else ""

    def scrub_mapped(self, text: str, dates: DateTokens, pivot: int) -> tuple[str, list[tuple[int, int, int, int]]]:
        """(scrubbed text, [(start, end, new start, new end)] of every replacement): a passage can then be cut from
        the scrubbed note, so a passage is scrubbed exactly as the whole note is."""
        out, pos, n, applied = [], 0, 0, []
        for a, b, rep in self._replacements(text or "", dates, pivot):
            out += [text[pos:a], rep]
            n += a - pos
            applied.append((a, b, n, n + len(rep)))
            n += len(rep)
            pos = b
        return "".join(out) + (text or "")[pos:], applied


def _letters(n: int) -> str:
    s = ""
    while True:
        s = string.ascii_uppercase[n % 26] + s
        n = n // 26 - 1
        if n < 0:
            return s


BRACKET_RX = re.compile(r"\[\[[^\]\n]*\]\]|\[[^\[\]\n]{1,60}\]")     # a token or a redaction: never cut through


def _mapped(applied: list[tuple[int, int, int, int]], starts: list[int], p: int, end: bool) -> int:
    """Position p of the note → its position in the scrubbed note (inside a replacement: its start, or its end)."""
    i = bisect_right(starts, p) - 1
    if i < 0:
        return p
    a, b, x, y = applied[i]
    if p >= b:
        return y + (p - b)
    return x if p == a or not end else y


def _passage(out: str, applied: list[tuple[int, int, int, int]], starts: list[int], a: int | None, b: int | None,
             context: int) -> str:
    """The passage a..b (positions in the note) with `context` characters around it, cut from the SCRUBBED note
    `out` and marked «...». Never cut through a word, a token or a redaction: half a word can look like a name to
    the final check, and a name whose "Dr." was cut off would not be redacted (19.11.26: a packet was refused)."""
    if a is None or b is None:
        return ""
    qa = _mapped(applied, starts, a, False)
    qb = max(qa, _mapped(applied, starts, b, True))
    s, e = max(0, qa - context), min(len(out), qb + context)
    for m in BRACKET_RX.finditer(out, max(0, s - 80), min(len(out), e + 80)):
        if m.start() < s < m.end():
            s = m.start()
        if m.start() < e < m.end():
            e = m.end()
    while 0 < s < len(out) and out[s - 1].isalnum() and out[s].isalnum():
        s -= 1
    while 0 < e < len(out) and out[e - 1].isalnum() and out[e].isalnum():
        e += 1
    body = out[s:qa] + "«" + out[qa:qb] + "»" + out[qb:e]
    return ("…" if s else "") + re.sub(r"[ \t]+", " ", body).strip() + ("…" if e < len(out) else "")


def _tidy(text: str) -> str:
    """A whole note for the packet: trailing spaces off, at most one blank line in a row, no fence of our own."""
    text = re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip().replace(FENCE, "~ ~ ~ ~")


def build_packet(extract: Path, deid: Path, review: Path, linkage: Path, out_dir: Path, settings, *,
                 passphrase: str | None = None, dictionary: Path | None = None, context: int = 250,
                 passages: int = 2, use_ner: bool = False, whole_notes: bool = False,
                 stdout: TextIO | None = None, tokens_from: Path | None = None) -> dict:
    """tokens_from: the key of an earlier packet; this packet's dates then carry that packet's token numbers."""
    stdout = stdout or sys.stdout
    seeds: dict[str, dict[str, str]] = {}
    if tokens_from is not None:
        if not Path(tokens_from).exists():
            raise PacketRefused("The key of the earlier packet (for the same date tokens) was not found.")
        seeds = seeds_from_key(json.loads(Path(tokens_from).read_text(encoding="utf-8")))
    con_d = connect(deid, readonly=True)
    try:
        manifest = get_meta(con_d, "manifest")
        # every study and document ID there is, for the exact check at the end
        real_ids = {x for r in con_d.execute("SELECT DISTINCT study_id, doc_id FROM documents") for x in r if x}
    finally:
        con_d.close()
    if not manifest.get("deidentified", True):
        raise PacketRefused("These notes were kept as they are (not de-identified): no packet can be made from them.")
    if not manifest.get("signoff_complete"):
        raise PacketRefused("The de-identification has not been signed off yet (step 6): finish the sign-off first.")
    link = load_linkage(linkage, passphrase or get_passphrase(settings))
    staff, orgs = link.get("staff", []), link.get("organizations", [])
    matchers = [KnownMatcher(IdentifierSet.from_json(p.get("identifiers", {})), staff, orgs)
                for p in link["patients"].values()]
    store = ReviewStore(review, extract, deid, dictionary or default_dictionary_path())
    scrub = Scrubber(settings, use_ner)
    scrub.known = matchers
    dic = store.dictionary
    specs = group_specs()
    repeating = repeating_forms(dic)
    files: dict[str, str] = {}
    key: dict = {"made": datetime.now().isoformat(timespec="seconds"), "patients": {}}
    totals = defaultdict(int)
    label_named: dict[str, bool] = {}

    def coded(code, text: str) -> str:
        """`code = label`. A codebook label that names an organisation or person on the known-identifier list (a
        laboratory or a hospital among the choices) is left out together with its code: the answer is withheld."""
        if text not in label_named:
            label_named[text] = any(m.find(text) for m in matchers)
        if label_named[text]:
            totals["answers withheld (the label is on the known-identifier list)"] += 1
            return WITHHELD
        return f"{code} = {text}"

    try:
        for pn, sid in enumerate(store.patients()):
            label = f"Patient {_letters(pn)}"
            docs = {r["doc_id"]: dict(r) for r in store.deid.execute(
                "SELECT doc_id, report_date, report_description, relevance_class, text FROM documents WHERE study_id=? "
                "ORDER BY report_date, doc_id", (sid,))}
            note_no = {d: n for n, d in enumerate(docs, 1)}
            scrubbed: dict[str, tuple[str, list[tuple[int, int, int, int]], list[int]]] = {}
            key["patients"][label] = {"study_id": sid, "notes": {f"Note {n}": d for d, n in note_no.items()}}
            years = [int(d["report_date"][:4]) for d in docs.values() if (d["report_date"] or "")[:4].isdigit()]
            pivot = max(years) if years else date.today().year
            # every date of this patient first, so the D numbers follow date order
            raw_dates = [d["report_date"][:10] for d in docs.values() if d["report_date"]]
            raw_dates += collect_dates([d["text"] for d in docs.values()], pivot)
            props = [f"{r[0] or ''} {r[1] or ''}" for r in store.extract.execute(
                "SELECT value, conflict_note FROM proposals WHERE run_id=? AND study_id=?", (store.run_id, sid))]
            raw_dates += [m for x in props for m in re.findall(r"\d{4}-\d{2}-\d{2}", x)]
            raw_dates += collect_dates(props, pivot)
            dt = DateTokens(raw_dates, seeds.get(sid))

            def scrubbed_note(doc_id, docs=docs, scrubbed=scrubbed, dt=dt, pivot=pivot):
                if doc_id not in scrubbed:
                    out, applied = scrub.scrub_mapped((docs.get(doc_id) or {}).get("text") or "", dt, pivot)
                    scrubbed[doc_id] = (out, applied, [t[0] for t in applied])
                return scrubbed[doc_id]

            def passage(doc_id, a, b):
                return _passage(*scrubbed_note(doc_id), a, b, context)

            def note_ref(doc_id, docs=docs, note_no=note_no, dt=dt, pivot=pivot):
                d = docs.get(doc_id) or {}
                when = dt.token(d["report_date"]) if d.get("report_date") else "undated"
                return f"Note {note_no.get(doc_id, '?')} ({scrub.scrub(d.get('report_description') or 'note', dt, pivot)}, {when})"

            def val(field, v, dt=dt, pivot=pivot):
                if v in (None, "", "[]"):
                    return "—"
                f = dic.get(field)
                if f is not None and f.is_date and re.match(r"\d{4}-\d{2}-\d{2}", str(v)):
                    return dt.token(str(v))
                labels = store.labels(field)
                try:
                    codes = json.loads(v) if f is not None and f.is_checkbox else None
                except (json.JSONDecodeError, TypeError):
                    codes = None
                if codes is not None:
                    return ", ".join(coded(c, labels.get(str(c), "?")) for c in codes) or "—"
                if str(v) in labels:
                    return coded(v, labels[str(v)])
                return scrub.own_text(str(v), dt, pivot)

            lines = [f"# {label}", ""]
            gate = store.fields(sid, GATE, GATE)
            lines += ["## Diagnosis check", ""]
            for fs in gate:
                lines.append(f"- {fs.field}: proposed {val(fs.field, fs.proposed)}; review: {fs.status}"
                             + (f" ({val(fs.field, fs.value)})" if fs.status == "corrected" else ""))
            cited: dict[str, set[str]] = defaultdict(set)     # group -> doc_ids already cited
            for inst in INSTRUMENT_FIELDS:
                keys = store.instances(sid, inst)
                lines += ["", f"## {form_label(dic, inst)}", ""]
                if not keys:
                    lines.append("No instance proposed.")
                for n, k in enumerate(keys, 1):
                    fields = store.visible(store.fields(sid, inst, k))
                    shown_passages: dict[tuple[str, int | None], str] = {}   # each passage once per instance
                    lines += ["", f"### {form_label(dic, inst).split(' (')[0]} #{n}" if inst in repeating else ""]
                    conflict = "; ".join(dict.fromkeys(f.conflict for f in fields if f.conflict))
                    if conflict:
                        lines.append(f"Conflict noted: {scrub.own_text(conflict, dt, pivot)}")
                    for note in store.info(sid, inst, k):
                        lines.append(f"How derived: {scrub.own_text(note, dt, pivot)}")
                    for fs in fields:
                        f = dic.get(fs.field)
                        kind, why = source(dic, fs.field, store.hospital_names)
                        head = f"- **#{f.number if f else '?'} {fs.field}**: proposed {val(fs.field, fs.proposed)}"
                        if fs.status != "pending":
                            head += f"; review: {fs.status}" + (f" → {val(fs.field, fs.value)}" if fs.status == "corrected" else "")
                        if kind != "notes":
                            head += f" ({kind}: {why})"
                        lines.append(head)
                        ev = store.evidence(fs.evidence_ids, sid)
                        ev = sorted(ev, key=lambda e: (not e.get("verified"), e.get("doc_date") or ""))
                        seen_docs: set[str] = set()
                        for e in ev:
                            if len(seen_docs) >= passages or e["doc_id"] in seen_docs:
                                continue
                            seen_docs.add(e["doc_id"])
                            grp = _note_fields().get(fs.field)
                            if grp:
                                cited[grp].add(e["doc_id"])
                            text = docs.get(e["doc_id"], {}).get("text") or ""
                            span = store.highlight_span(e, text)
                            where = (e["doc_id"], span[0] if span else None)
                            if where in shown_passages:
                                lines.append(f"  - same passage as {shown_passages[where]}")
                                continue
                            shown_passages[where] = f"P{len(shown_passages) + 1}"
                            snip = passage(e["doc_id"], *span) if span else scrub.scrub(e.get("quote") or "", dt, pivot)
                            lines.append(f"  - [{shown_passages[where]}] {note_ref(e['doc_id'])}: {snip}")
                            totals["evidence passages"] += 1
                        if fs.proposed in (None, "", "[]") and kind == "notes" and inst not in repeating:
                            grp = _note_fields().get(fs.field)
                            rx = field_keyword_rx(grp, fs.field) if grp in specs else None
                            rx = rx or (keyword_rx(grp) if grp in specs else None)
                            hits = 0
                            for d in docs.values():
                                if hits >= passages or rx is None:
                                    break
                                if not is_candidate(grp, d["relevance_class"], d["text"], {"low_value"},
                                                    d["report_description"])[0]:
                                    continue
                                m = rx.search(d["text"])
                                if m:
                                    lines.append(f"  - possible source, {note_ref(d['doc_id'])}: "
                                                 f"{passage(d['doc_id'], m.start(), m.end())}")
                                    hits += 1
                                    totals["passages for empty fields"] += 1
                # repeating instruments: notes the groups read that no instance cites (possible missed instances)
                if inst in repeating:
                    groups = sorted({g for g, s in specs.items() if s.get("instrument") == inst and g in specs})
                    shown = 0
                    for g in groups:
                        rx = keyword_rx(g)
                        for d in docs.values():
                            if shown >= passages * 2:
                                break
                            if d["doc_id"] in cited[g] or not is_candidate(
                                    g, d["relevance_class"], d["text"], {"low_value"}, d["report_description"])[0]:
                                continue
                            m = rx.search(d["text"])
                            if m:
                                if not shown:
                                    lines += ["", "Notes read for this instrument that no instance cites "
                                                  "(possible missed instances):"]
                                lines.append(f"  - {note_ref(d['doc_id'])}: {passage(d['doc_id'], m.start(), m.end())}")
                                shown += 1
                                totals["passages for missed instances"] += 1
            conds = store.conditions(sid)
            if conds:
                lines += ["", "## Conditions found in the notes", ""]
                lines += [f"- {scrub.own_text(c['name'], dt, pivot)}: category {c['category']}, {c['documents']} notes"
                          for c in conds]
            stem = label.replace(" ", "_")
            if whole_notes:
                parts: list[list[str]] = [[]]
                size = 0
                index = ["", "## Notes (whole text)", "",
                         f"Every note of {label} in date order, in {stem}_notes_01.md, {stem}_notes_02.md, ...", ""]
                for doc_id, d in docs.items():
                    body = _tidy(scrubbed_note(doc_id)[0])
                    chunk = "\n".join([f"## {note_ref(doc_id)}", f"Class: {d['relevance_class'] or 'unclassified'}",
                                       "", FENCE, body, FENCE, "", ""])
                    if size and size + len(chunk) > NOTES_PART_CHARS:
                        parts.append([])
                        size = 0
                    parts[-1].append(chunk)
                    size += len(chunk)
                    index.append(f"- {note_ref(doc_id)}, class {d['relevance_class'] or 'unclassified'}: "
                                 f"{len(body):,} characters, in {stem}_notes_{len(parts):02d}.md")
                    totals["whole notes"] += 1
                for i, p in enumerate(parts, 1):
                    files[f"{stem}_notes_{i:02d}.md"] = f"# {label}: notes, part {i} of {len(parts)}\n\n" + "".join(p)
                    totals["note files"] += 1
                lines += index
            files[f"{stem}.md"] = "\n".join(lines) + "\n"
            # date tokens back to the (shifted) dates, so answers given as tokens can be mapped back; PHI folder only
            key["patients"][label]["dates"] = dt.key()
            totals["patients"] += 1
    finally:
        store.close()
    # final checks on exactly what would be written
    problems = defaultdict(int)
    shapes: set[str] = set()
    places: dict[str, int] = defaultdict(int)     # kind of identifier and kind of line: never the text
    for name, text in files.items():
        where = " (whole notes)" if "_notes_" in name else ""
        # without the «» marks: a mark inside a word would leave two half-words, and half a word can match a name
        bare = PACKET_TOKEN_RX.sub(" ", text).replace("«", "").replace("»", "")
        found = {(h.start, h.end, h.category) for m in matchers for h in m.find(bare)}   # each place once
        for start, _end, category in found:
            problems["known identifiers" + where] += 1
            places[f"{category} in {_line_kind(bare, start)}"] += 1
        for _m, _got in _date_mentions(bare, date.today().year):
            problems["dates left" + where] += 1
        hits = [s for s in real_ids if s in bare] + [m.group(0) for m in ID_SHAPE_RX.finditer(bare)]
        if hits:
            problems["study or document IDs" + where] += len(set(hits))
            shapes.update(_mask(h) for h in hits)
    if problems:
        raise PacketRefused("Checks failed, nothing written: " + ", ".join(f"{k} {v}" for k, v in problems.items())
                            + (f" (shapes: {', '.join(sorted(shapes))})" if shapes else "")
                            + ("\nWhere the known identifiers are (kind and line only): "
                               + "; ".join(f"{k}: {v}" for k, v in sorted(places.items())) if places else ""))
    out_dir = ensure_outside_repo(out_dir, "the quality packet")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8")
    n_patients = totals["patients"]
    (out_dir / "README.md").write_text(
        README.format(n=n_patients, context=context, passages=passages, ner="on" if use_ner else "off")
        + (README_WHOLE_NOTES.format(part=NOTES_PART_CHARS) if whole_notes else "")
        + (README_SAME_TOKENS if seeds else ""), encoding="utf-8")
    key_path = ensure_phi_output(Path(deid).parent / f"quality_packet_key_{out_dir.name}.json", "the packet key")
    key_path.write_text(json.dumps(key, indent=1), encoding="utf-8")
    notes_msg = f" and {totals['note files']} files of whole notes" if whole_notes else ""
    print(f"# Quality packet\n\nWrote {n_patients} patient files{notes_msg} to the packet folder. Checks: known "
          "identifiers 0, dates left 0, study/document IDs 0.", file=stdout)
    for k, v in sorted({**totals, **{f"scrubbed: {k}": v for k, v in scrub.counts.items()}}.items()):
        print(f"- {k}: {v}", file=stdout)
    print("The key from Patient A/B/..., Note numbers and date tokens back to study IDs, document IDs and dates is in "
          "the de-identified folder (not in the packet).", file=stdout)
    return {"files": n_patients, "note_files": totals["note files"], "totals": dict(totals),
            "scrubbed": dict(scrub.counts), "key": key_path}


README = """# NMOSD extraction: quality packet

Made by `nmosd quality-packet` for an outside assessment of the extraction's proposals. {n} patient files.

For each patient: the diagnosis check, then every REDCap instrument with each proposed value (codebook number,
field, code = label), the reviewer's decision when there is one, how derived values were worked out, and the
evidence: up to {passages} passages per value with {context} characters of context, the quoted words in «».
For an empty field read from the notes: up to {passages} passages from notes its field group read that contain its
keywords. For a repeating instrument: passages from notes the groups read that no instance cites.

De-identification (HIPAA Safe Harbor, 45 CFR 164.514(b)(2)), on top of the signed-off de-identification of the notes:
- no names, addresses, contact details or ID numbers (de-identification tokens such as [PATIENT], [CLINICIAN_01]);
- no date element but the year: `[[2019 D05]]` is a date in 2019; the same date is always D05 for this patient, and
  D numbers follow date order. "month-year" or "season" says how precisely the note wrote it;
- ages over 89 as 90+; residual patterns and numbers of 7 or more digits as [REDACTED ...]; name recognition: {ner};
- patients as letters and notes as numbers; the key back to study IDs stays on the user's computer;
- every patient's known identifiers were searched in this packet: 0 found.

Whether sharing this packet is permitted is decided by MGB (privacy office / IRB), not by this tool.
"""

README_WHOLE_NOTES = """
## Whole notes (`--whole-notes`)

This packet also holds the whole text of every note, so that the assessment can work out what each REDCap field
should have been, not only check the proposals. For each patient, `Patient_A_notes_01.md`, `Patient_A_notes_02.md`,
... hold every note in date order (files of about {part:,} characters, split between notes). Each note starts with
its heading (`## Note 7 (description, [[2019 D05]])`) and its class, and its text sits between `~~~~` lines. The
note numbers and date tokens are the same as in `Patient_A.md`, whose last section lists every note and its file.
The whole notes went through the same scrubbing and the same checks (known identifiers, dates, IDs) as the passages.

Answers about dates can be given as tokens (`[[2019 D05]]`); the key on the user's computer maps them back.
"""

README_SAME_TOKENS = """
## Date tokens as in an earlier packet

The dates in this packet carry the same tokens as in the earlier packet the reference answers were written from, so
the two can be laid side by side. A date that packet did not have carries the number of the last earlier date and a
letter: `D05a` falls after D05 and before D06 (`D00a`: before every date of the earlier packet).
"""


def run_quality_packet(extract: Path, deid: Path, review: Path, linkage: Path, out_dir: Path, settings,
                       dictionary: Path | None = None, context: int = 250, passages: int = 2, use_ner: bool = False,
                       whole_notes: bool = False, stdout: TextIO | None = None,
                       tokens_from: Path | None = None) -> int:
    stdout = stdout or sys.stdout
    try:
        build_packet(extract, deid, review, linkage, out_dir, settings, dictionary=dictionary, context=context,
                     passages=passages, use_ner=use_ner, whole_notes=whole_notes, stdout=stdout,
                     tokens_from=tokens_from)
    except PacketRefused as e:
        print(f"No packet written: {e}", file=stdout)
        return 1
    return 0
