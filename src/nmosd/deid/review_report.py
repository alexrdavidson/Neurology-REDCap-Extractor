"""`nmosd deid-report`: local HTML for the 100% human de-identification review (PHI location only).

Original and de-identified text side by side, every replacement highlighted, a "reviewed — no residual
PHI" checkbox and a misses box per document. Ticks persist in the browser (localStorage) and are
exported as a JSON file that `nmosd deid-signoff` imports into the manifest. Claude must never open it.
"""

from __future__ import annotations

import html
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import TextIO

from nmosd.deid.date_shift import find_dates
from nmosd.deid.linkage import get_passphrase, load_linkage
from nmosd.deid.surrogates import TOKEN_RX
from nmosd.safety import ensure_phi_output
from nmosd.store.db import connect, get_meta, set_meta

CSS = """
:root{--bg:#fafafa;--fg:#1d1d1f;--card:#fff;--line:#ddd;--mark:#ffe08a;--tok:#c7e6ff;--date:#d9f5d0}
@media (prefers-color-scheme: dark){:root{--bg:#141414;--fg:#eee;--card:#1f1f1f;--line:#333;--mark:#7a5d00;
--tok:#164a6e;--date:#245c1a}}
body{font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:16px}
header{position:sticky;top:0;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line);z-index:2}
.doc{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:16px 0;padding:12px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:900px){.cols{grid-template-columns:1fr}}
pre{white-space:pre-wrap;word-break:break-word;font:12.5px/1.4 ui-monospace,Menlo,Consolas,monospace;margin:0;
max-height:520px;overflow:auto;border:1px solid var(--line);padding:8px;border-radius:6px}
mark{background:var(--mark);color:inherit;border-radius:3px}
.tok{background:var(--tok);border-radius:3px}.dt{background:var(--date);border-radius:3px}
textarea{width:100%;min-height:48px;box-sizing:border-box}
.meta{opacity:.75;font-size:12px}
.chk{outline:2px solid #d33;background:rgba(220,40,40,.18);border-radius:3px;scroll-margin:80px}
.chk:target{outline-width:4px}
.summary{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:12px 0;padding:10px 12px}
.summary a{margin-right:6px}.tocheck{color:#d33;font-weight:600}
"""

JS = """
const KEY='nmosd-deid-review';
function load(){try{return JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){return {}}}
function save(s){try{localStorage.setItem(KEY,JSON.stringify(s))}catch(e){}}
function refresh(){const s=load();let n=0;document.querySelectorAll('.doc').forEach(d=>{const id=d.dataset.id;
 const st=s[id]||{};d.querySelector('input[type=checkbox]').checked=!!st.reviewed;
 d.querySelector('textarea').value=st.misses||'';if(st.reviewed)n++});
 document.getElementById('progress').textContent=n+' / '+document.querySelectorAll('.doc').length+' reviewed';
 document.getElementById('reviewer').value=s.__reviewer||''}
document.addEventListener('change',e=>{const d=e.target.closest('.doc');const s=load();
 if(e.target.id==='reviewer'){s.__reviewer=e.target.value;save(s);return}
 if(!d)return;const id=d.dataset.id;s[id]=s[id]||{};
 if(e.target.type==='checkbox')s[id].reviewed=e.target.checked;else s[id].misses=e.target.value;save(s);refresh()});
function download(){const s=load();const out={reviewer:s.__reviewer||'',exported_at:new Date().toISOString(),documents:{}};
 document.querySelectorAll('.doc').forEach(d=>{const st=s[d.dataset.id]||{};out.documents[d.dataset.id]=
 {reviewed:!!st.reviewed,misses:st.misses||''}});const b=new Blob([JSON.stringify(out,null,1)],{type:'application/json'});
 const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='deid_signoff.json';a.click()}
window.addEventListener('load',refresh);
window.addEventListener('load',()=>{const o=document.getElementById('onlychk');if(!o)return;
 o.addEventListener('change',()=>document.querySelectorAll('.doc').forEach(d=>{
 d.style.display=(o.checked&&!d.dataset.chk)?'none':''}))});
"""


def _highlight_original(text: str, spans: list) -> str:
    out, pos = [], 0
    for s, e, cat, src, repl in sorted(spans, key=lambda x: (x[0], -x[1])):
        if s < pos:
            continue
        out.append(html.escape(text[pos:s]))
        title = html.escape(f"{cat} ({src}) → {repl}")
        out.append(f'<mark title="{title}">{html.escape(text[s:e])}</mark>')
        pos = e
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _highlight_deid(text: str, spots: list[tuple[int, int, str, str, int]] | None = None) -> str:
    """Tokens and dates highlighted; the leak test's spots to check (start, end, category, source, number) in red,
    each with an anchor `chk-<number>`. A spot wins over a token or date it overlaps."""
    chk = [(s, e, "chk", f"Check: possible {cat} ({src})", n) for s, e, cat, src, n in (spots or [])
           if 0 <= s < e <= len(text)]
    marks = list(chk)
    taken = [(s, e) for s, e, *_ in chk]
    for m in TOKEN_RX.finditer(text):
        if not any(m.start() < e and s < m.end() for s, e in taken):
            marks.append((m.start(), m.end(), "tok", "", 0))
    dates, _ = find_dates(text)
    marks += [(m.start, m.end, "dt", "", 0) for m in dates
              if not any(s <= m.start < e for s, e, *_ in marks)]
    out, pos = [], 0
    for s, e, cls, title, n in sorted(marks, key=lambda x: (x[0], -x[1])):
        if s < pos:
            continue
        out.append(html.escape(text[pos:s]))
        extra = f' id="chk-{n}" title="{html.escape(title)}"' if cls == "chk" else ""
        out.append(f'<span class="{cls}"{extra}>{html.escape(text[s:e])}</span>')
        pos = e
    out.append(html.escape(text[pos:]))
    return "".join(out)


def merge_spots(spots: list[tuple[int, int, str, str]]) -> list[tuple[int, int, str, str]]:
    """Overlapping spots (e.g. the same name flagged by the pattern check and by name recognition) become one."""
    out: list[list] = []
    for s, e, cat, src in sorted(spots):
        if out and s < out[-1][1]:
            last = out[-1]
            last[1] = max(last[1], e)
            last[2] = " / ".join(sorted(set(last[2].split(" / ")) | {cat}))
            last[3] = " + ".join(sorted(set(last[3].split(" + ")) | {src}))
        else:
            out.append([s, e, cat, src])
    return [tuple(x) for x in out]  # type: ignore[misc]


def load_spots(path: Path | None, texts: dict[str, str]) -> tuple[dict | None, dict[str, list], int]:
    """(file info, {doc_id: [[field, start, end, category, source], ...]}, stale documents ignored)."""
    if not path or not Path(path).exists():
        return None, {}, 0
    from nmosd.deid.leak_test import text_hash

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out, stale = {}, 0
    for doc_id, d in (data.get("documents") or {}).items():
        if doc_id not in texts:
            continue
        if d.get("text_hash") != text_hash(texts[doc_id]):
            stale += 1          # made before a new de-identification run: positions no longer apply
            continue
        out[doc_id] = d.get("spots", [])
    return data, out, stale


def _summary(info: dict | None, spots: dict[str, list], numbered: list[tuple[int, str]], stale: int) -> str:
    if info is None:
        return ("<div class='summary'><strong>Spots to check:</strong> not available. Run the leak test "
                "(`nmosd leak-test`, or step 5 in the app) and make this report again: it then marks the possible "
                "identifiers that the de-identification did not remove.</div>")
    by_cat: dict[str, list[int]] = {}
    for n, cat in numbered:
        by_cat.setdefault(cat, []).append(n)
    head = (f"<strong>Spots to check: {len(numbered)}</strong> in {len(spots)} notes, marked "
            "<span class='chk'>like this</span>. The leak test's second opinion: possible identifiers that the "
            "de-identification did not remove. Many are false alarms (drug, test or department names); check each "
            "one, and if it is a real identifier, note where it is in the misses box (not what it says).")
    if not info.get("ner"):
        head += " (Made without the name-recognition models: only the pattern checks.)"
    if stale:
        head += f" {stale} notes changed since the leak test: run it again to mark them."
    links = "".join(f"<div>{html.escape(cat)} ({len(ns)}): " + "".join(f"<a href='#chk-{n}'>{n}</a>" for n in ns)
                    + "</div>" for cat, ns in sorted(by_cat.items()))
    return (f"<div class='summary'>{head}<div><label><input type='checkbox' id='onlychk'> Show only notes with "
            f"spots to check</label></div><details><summary>All spots, by kind</summary>{links}</details></div>")


def write_report(staging: Path, deid: Path, linkage: Path, out: Path, settings, passphrase: str | None = None,
                 stdout: TextIO | None = None, candidates: Path | None = None) -> Path:
    stdout = stdout or sys.stdout
    out = ensure_phi_output(out, "the de-identification review report")
    link = load_linkage(linkage, passphrase or get_passphrase(settings))
    st = connect(staging, readonly=True)
    original = {r["doc_pk"]: r["text"] for r in st.execute("SELECT doc_pk, text FROM documents")}
    st.close()
    dc = connect(deid, readonly=True)
    rows = list(dc.execute("SELECT doc_id, study_id, source_table, report_date, report_description, text "
                           "FROM documents ORDER BY study_id, report_date"))
    flags = Counter()
    per_doc_flags: dict[str, list[str]] = {}
    for r in dc.execute("SELECT doc_id, reason FROM review_queue"):
        per_doc_flags.setdefault(r["doc_id"], []).append(r["reason"])
        flags[r["reason"]] += 1
    dc.close()
    from nmosd.deid.leak_test import CANDIDATES_NAME

    info, spots, stale = load_spots(candidates or out.with_name(CANDIDATES_NAME), {r["doc_id"]: r["text"] for r in rows})
    numbered: list[tuple[int, str]] = []
    doc_spots: dict[str, list[tuple[int, int, str, str, int]]] = {}
    doc_titles: dict[str, list[str]] = {}
    for r in rows:
        text_spots = []
        for field, s0, e0, cat, src in spots.get(r["doc_id"], []):
            if field == "title":
                doc_titles.setdefault(r["doc_id"], []).append(cat)
            else:
                text_spots.append((s0, e0, cat, src))
        for s0, e0, cat, src in merge_spots(text_spots):
            numbered.append((len(numbered) + 1, cat))
            doc_spots.setdefault(r["doc_id"], []).append((s0, e0, cat, src, len(numbered)))
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
             f"content='width=device-width,initial-scale=1'><title>De-identification review</title><style>{CSS}</style>"
             f"</head><body><header><strong>De-identification review</strong> · CONTAINS PHI · "
             f"<span id='progress'></span> · Reviewer <input id='reviewer' placeholder='your name'> "
             f"<button onclick='download()'>Download sign-off JSON</button></header>",
             _summary(info, spots, numbered, stale)]
    for r in rows:
        meta = link["documents"].get(r["doc_id"], {})
        orig = original.get(meta.get("doc_pk"), "")
        fl = ", ".join(per_doc_flags.get(r["doc_id"], [])) or "none"
        mine = doc_spots.get(r["doc_id"], [])
        cats = Counter(c for _, _, c, _, _ in mine) + Counter(doc_titles.get(r["doc_id"], []))
        tocheck = (" · <span class='tocheck'>to check: " + ", ".join(f"{html.escape(c)} ×{n}" for c, n in
                                                                   sorted(cats.items()))
                   + (" (one in the title)" if doc_titles.get(r["doc_id"]) else "") + "</span>") if cats else ""
        parts.append(
            f"<section class='doc' data-id='{html.escape(r['doc_id'])}'{' data-chk=1' if cats else ''}>"
            f"<div class='meta'>{html.escape(r['study_id'])} · "
            f"{html.escape(r['doc_id'])} · {html.escape(r['source_table'])} · {html.escape(r['report_description'] or '')}"
            f" · shifted date {html.escape(r['report_date'] or '')} · flags: {html.escape(fl)}{tocheck}</div>"
            f"<div class='cols'><div><div class='meta'>Original (identifiable)</div><pre>"
            f"{_highlight_original(orig, meta.get('spans', []))}</pre></div><div><div class='meta'>De-identified</div>"
            f"<pre>{_highlight_deid(r['text'], mine)}</pre></div></div>"
            f"<label><input type='checkbox'> reviewed — no residual PHI</label>"
            f"<textarea placeholder='Misses (describe the location, not the value, if you will share this)'></textarea>"
            f"</section>")
    parts.append(f"<script>{JS}</script></body></html>")
    out.write_text("".join(parts), encoding="utf-8")
    print(f"Wrote the review report for {len(rows)} documents to the PHI location. Flags: {dict(flags) or 'none'}. "
          + (f"Spots to check marked: {len(numbered)} in {len(spots)} documents." if info is not None else
             "No leak-test positions found: run `nmosd leak-test` to mark the spots to check."), file=stdout)
    return out


def import_signoff(deid: Path, signoff: Path, stdout: TextIO | None = None) -> dict:
    stdout = stdout or sys.stdout
    data = json.loads(Path(signoff).read_text(encoding="utf-8"))
    con = connect(deid, "deid")
    docs = {r[0] for r in con.execute("SELECT doc_id FROM documents")}
    now = datetime.now().isoformat(timespec="seconds")
    reviewer = data.get("reviewer", "")
    rows = [(d, reviewer, int(bool(v.get("reviewed"))), v.get("misses", ""), now)
            for d, v in data.get("documents", {}).items() if d in docs]
    con.execute("DELETE FROM signoff")
    con.executemany("INSERT INTO signoff VALUES (?,?,?,?,?)", rows)
    reviewed = sum(r[2] for r in rows)
    misses = sum(1 for r in rows if r[3].strip())
    m = get_meta(con, "manifest")
    m.update({"signoff_reviewer": reviewer, "signoff_date": now, "signoff_reviewed_docs": reviewed,
              "signoff_total_docs": len(docs), "signoff_docs_with_misses": misses,
              "signoff_complete": reviewed == len(docs) and misses == 0})
    set_meta(con, "manifest", m)
    con.commit()
    con.close()
    print(f"Sign-off imported: {reviewed}/{len(docs)} documents reviewed, {misses} with reported misses. "
          f"Complete: {m['signoff_complete']}.", file=stdout)
    return m
