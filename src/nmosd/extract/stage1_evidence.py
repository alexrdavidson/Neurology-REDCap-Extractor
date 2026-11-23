"""Stage 1: evidence per (document chunk, field group).

Builds the prompt, calls the LLM with the group's JSON schema, normalises dates/estimate codes, demotes
"planned" starts the model may mislabel, verifies quotes against the source and turns each item into
evidence rows (one row per field, sharing an item_id).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from importlib import resources

from pydantic import BaseModel, ValidationError

from nmosd.extract.chunking import find_sections, split_view
from nmosd.extract.ollama_client import LLMClient, LLMResult
from nmosd.extract.prefilter import group_specs
from nmosd.extract.quote_verify import verify_quote
from nmosd.redcap.schemas import NE, anchor_keys, date_keys, group_items, is_generic
from nmosd.textproc.dates import written_precision
from nmosd.textproc.view import TextView

PLAN_RX = re.compile(r"(?i)\b(plan(?:ned|ning|s)? to|will (?:start|begin|initiate)|would (?:start|consider)|recommend|"
                     r"consider(?:ed|ing)?|discussed|pending|prior auth|insurance approval|to be started|scheduled to|"
                     r"if (?:she|he|they) relapse|option)")
NEG_RX = re.compile(r"(?i)\b(no|never|denies|without|not)\b")
# A cut-off answer (it ran into num_predict, so the JSON never closed) is asked again on the two halves of the
# excerpt, down to this depth; what is left is salvaged item by item (16.11.26: long problem lists were lost whole).
MAX_SPLIT_DEPTH = 4
MIN_SPLIT_CHARS = 600
SPLIT_OVERLAP = 200


@lru_cache(maxsize=2)
def prompt_texts() -> tuple[str, str]:
    base = resources.files("nmosd.extract") / "prompts"
    return (base / "system.md").read_text(encoding="utf-8"), (base / "group.md").read_text(encoding="utf-8")


def prompt_version() -> str:
    s, g = prompt_texts()
    return hashlib.sha256((s + g).encode()).hexdigest()[:12]


def field_specs_text(group: str) -> str:
    """The group's own instructions (`guidance:`) and per-field definitions. Fields of a `redcap_fields:` group
    need no definition: the schema description of each item carries the codebook's number, label, field note,
    branching logic and codes."""
    spec = group_specs()[group]
    lines = [spec["guidance"].strip()] if spec.get("guidance") else []
    for name, f in spec.get("fields", {}).items():
        parts = [f"- {name}: {f['definition']}" if f.get("definition") else f"- {name}:"]
        for key, title in (("acceptable_evidence", "Acceptable evidence"), ("exclusions", "Exclusions"),
                           ("ambiguity_rule", "If ambiguous")):
            if f.get(key):
                parts.append(f"  {title}: {f[key]}")
        lines.append("\n".join(parts))
    if is_generic(group) and not spec.get("fields"):
        lines.append("Each output key below is a REDCap field: answer it from its codebook label, note and codes.")
    return "\n".join(lines)


def schema_words(schema: dict) -> str:
    props = schema["properties"]["items"]["items"]["properties"]
    out = []
    for k, v in props.items():
        kind = "list of codes" if v.get("type") == "array" else ("one of " + ", ".join(map(str, v["enum"]))
                                                                 if "enum" in v else v.get("type", "string"))
        out.append(f"- {k} ({kind}): {v.get('description', '')}")
    return "\n".join(out)


def build_prompt(group: str, chunk: TextView, schema: dict, doc_type: str, source_table: str, doc_date: str | None,
                 chunk_no: int, n_chunks: int) -> tuple[str, str]:
    system, template = prompt_texts()
    secs = sorted({s.stype for s in find_sections(chunk.original)
                   if any(a < s.end and s.start < b for a, b in chunk.pieces)})
    user = template.format(group=group, instrument=group_specs()[group]["instrument"],
                           field_specs=field_specs_text(group), schema_words=schema_words(schema),
                           doc_type=doc_type or "note", source_table=source_table, doc_date=doc_date or "unknown",
                           chunk_no=chunk_no, n_chunks=n_chunks, sections=", ".join(secs) or "whole note",
                           text=chunk.text)
    return system, user


def normalise_date(value: str | None, estimate: str | None) -> tuple[str, str]:
    """Return (YYYY-MM-DD | not_established, estimate code | not_established)."""
    v = (value or "").strip()
    est = (estimate or "").strip() or NE
    if not v or v in (NE, "UNK"):
        return NE, NE
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat(), est if est != NE else "1"
        except ValueError:
            return NE, NE
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", v)
    if m and 1 <= int(m.group(2)) <= 12:
        return date(int(m.group(1)), int(m.group(2)), 15).isoformat(), "2" if est in (NE, "1") else est
    m = re.fullmatch(r"(\d{4})", v)
    if m:
        return date(int(m.group(1)), 7, 1).isoformat(), "3" if est in (NE, "1", "2") else est
    return NE, NE


@dataclass
class ChunkResult:
    rows: list[dict]
    llm: LLMResult            # the first call, or the first one that failed
    valid: bool
    n_items: int
    demoted_plans: int
    calls: list[LLMResult] | None = None    # every call made for this chunk (more than one when it was split)
    splits: int = 0                         # times a cut-off answer made us split the excerpt
    salvaged: int = 0                       # items rescued from a cut-off answer that could not be split further


def items_to_rows(group: str, items: list[dict], chunk: TextView, *, run_id: str, doc_id: str, chunk_id: str,
                  study_id: str, doc_date: str | None) -> tuple[list[dict], int]:
    rows, demoted = [], 0
    keys = [k for k, kind, _ in group_items(group) if kind != "estimate"]
    dkeys, anchors = date_keys(group), anchor_keys(group)
    pivot = int(doc_date[:4]) if doc_date and doc_date[:4].isdigit() else date.today().year
    for it in items:
        item_id = uuid.uuid4().hex[:16]
        quote = it.get("quote", "")
        hypothetical = bool(it.get("hypothetical"))
        if group == "treatment" and it.get("event") in ("start", "stop") and (hypothetical or PLAN_RX.search(quote or "")):
            it = dict(it, event="planned")
            hypothetical = True
            demoted += 1
        qm = verify_quote(quote, chunk)
        base = {"run_id": run_id, "doc_id": doc_id, "chunk_id": chunk_id, "study_id": study_id, "field_group": group,
                "item_id": item_id, "quote": quote, "char_start": qm.start, "char_end": qm.end,
                "certainty": it.get("certainty"), "negated": int(bool(it.get("negated"))),
                "hypothetical": int(hypothetical), "verified": int(qm.verified), "verify_score": qm.score,
                "doc_date": doc_date}
        event_date, event_est, event_rank = NE, NE, len(anchors)
        for k in keys:
            v = it.get(k)
            if k in dkeys:
                est_key = dkeys[k]
                d, est = normalise_date(v, it.get(est_key) if est_key else None)
                if d != NE:
                    # "since 2019" written as 2019-01-01 with code 1 by the model: the quote decides (16.11.26)
                    wp = written_precision(quote, d, pivot)
                    if wp and (est == NE or wp > est):
                        est = wp
                if k in anchors and anchors.index(k) < event_rank and d != NE:
                    event_date, event_est, event_rank = d, est, anchors.index(k)
                elif k in anchors and event_rank == len(anchors):
                    event_date, event_est = d, est
                if d == NE:
                    continue
                rows.append(base | {"evidence_id": uuid.uuid4().hex, "field": k, "value": d, "event_date": d,
                                    "estimate_code": est if est_key else None})
            else:
                if v in (None, "", NE, []) or (isinstance(v, list) and not v):
                    continue
                val = json.dumps(sorted(set(v))) if isinstance(v, list) else str(v)
                rows.append(base | {"evidence_id": uuid.uuid4().hex, "field": k, "value": val, "event_date": None,
                                    "estimate_code": None})
        for r in rows:
            if r["item_id"] == item_id and r["event_date"] is None and event_date != NE:
                r["event_date"], r["estimate_code"] = event_date, r["estimate_code"] or event_est
    return rows, demoted


def salvage_items(content: str) -> list[dict]:
    """The complete item objects at the start of a cut-off `{"items": [...` answer."""
    i = content.find("[")
    if i < 0:
        return []
    out, depth, start, in_str, esc = [], 0, None, False, False
    for j in range(i + 1, len(content)):
        ch = content[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = j
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                with contextlib.suppress(json.JSONDecodeError):
                    out.append(json.loads(content[start:j + 1]))
                start = None
        elif ch == "]" and depth == 0:
            break
    return out


def _cut_off(res: LLMResult, client: LLMClient) -> bool:
    if res.parsed is not None or res.error is not None or not res.content.strip():
        return False
    limit = getattr(client, "num_predict", None)
    return bool((limit and res.eval_count and res.eval_count >= int(limit) - 4) or not res.content.rstrip().endswith("}"))


def _parse_items(model: type[BaseModel], parsed) -> tuple[list[dict], bool]:
    if parsed is None:
        return [], False
    try:
        return [i.model_dump() for i in model.model_validate(parsed).items], True
    except ValidationError:
        # keep what is individually valid so one bad item does not drop the rest
        item_model = model.model_fields["items"].annotation.__args__[0]
        items = []
        for raw in parsed.get("items", []) if isinstance(parsed, dict) else []:
            try:
                items.append(item_model.model_validate(raw).model_dump())
            except ValidationError:
                continue
        return items, False


def extract_chunk(client: LLMClient, model: type[BaseModel], schema: dict, group: str, chunk: TextView, *, run_id: str,
                  doc_id: str, chunk_id: str, study_id: str, doc_type: str, source_table: str, doc_date: str | None,
                  chunk_no: int, n_chunks: int, _depth: int = 0) -> ChunkResult:
    system, user = build_prompt(group, chunk, schema, doc_type, source_table, doc_date, chunk_no, n_chunks)
    res = client.generate_json(system, user, schema)
    items, valid = _parse_items(model, res.parsed)
    kw = dict(run_id=run_id, doc_id=doc_id, chunk_id=chunk_id, study_id=study_id, doc_type=doc_type,
              source_table=source_table, doc_date=doc_date, chunk_no=chunk_no, n_chunks=n_chunks)
    salvaged = 0
    if _cut_off(res, client):
        halves = split_view(chunk, len(chunk.text) // 2 + 1, SPLIT_OVERLAP) \
            if _depth < MAX_SPLIT_DEPTH and len(chunk.text) >= MIN_SPLIT_CHARS else []
        if len(halves) > 1:
            parts = [extract_chunk(client, model, schema, group, h, _depth=_depth + 1, **kw) for h in halves]
            calls = [res] + [c for p in parts for c in (p.calls or [p.llm])]
            failed = next((c for c in calls if c.error is not None), None)
            return ChunkResult([r for p in parts for r in p.rows], failed or res, all(p.valid for p in parts),
                               sum(p.n_items for p in parts), sum(p.demoted_plans for p in parts), calls,
                               1 + sum(p.splits for p in parts), sum(p.salvaged for p in parts))
        item_model = model.model_fields["items"].annotation.__args__[0]
        for raw in salvage_items(res.content):
            try:
                items.append(item_model.model_validate(raw).model_dump())
                salvaged += 1
            except ValidationError:
                continue
    rows, demoted = items_to_rows(group, items, chunk, run_id=run_id, doc_id=doc_id, chunk_id=chunk_id,
                                  study_id=study_id, doc_date=doc_date)
    return ChunkResult(rows, res, valid, len(items), demoted, [res], 0, salvaged)
