"""A deterministic stand-in for the LLM, for SYNTHETIC data only.

It answers stage-1 prompts by locating the synthetic generator's annotated quotes in the (de-identified)
excerpt and reading the shifted dates back out of the text. It exists to test the pipeline mechanics
(prefilter, chunking, quote verification, reconciliation, review, export, evaluation) without a model.
`mislabel_plans=True` makes it report plans as starts, like a careless model, to test the guards.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from nmosd.extract.ollama_client import LLMResult, estimate_tokens
from nmosd.redcap.schemas import GROUP_ITEMS, NE
from nmosd.synth.annotations import quote_pattern
from nmosd.textproc.dates import date_and_estimate

EST_FOR = {"date_onset": "date_attack_estimate", "event_date": "event_date_estimate",
           "assay_date": "assay_date_estimate", "date_nmo_diagnosis": "date_nmo_estimate"}


def parse_slot(text: str, pivot: int) -> tuple[str, str]:
    return date_and_estimate(text, pivot) or (NE, NE)


def _defaults(group: str) -> dict:
    out = {}
    for key, kind, _ in GROUP_ITEMS[group]:
        out[key] = [] if kind == "multi" else ("" if kind == "text" else NE)
    return out


class SyntheticOracleClient:
    name = "synthetic-oracle"
    model = "synthetic-oracle"
    num_ctx = 8192

    def __init__(self, annotations: Path | list, mislabel_plans: bool = False):
        data = json.loads(Path(annotations).read_text(encoding="utf-8")) if not isinstance(annotations, list) else annotations
        self.mislabel_plans = mislabel_plans
        self.by_group: dict[str, list[tuple[re.Pattern, list[dict]]]] = {}
        seen = set()
        for note in data:
            for a in note["annotations"]:
                key = (a["group"], json.dumps(a["segments"]), json.dumps(a["items"], sort_keys=True))
                if key in seen:
                    continue
                seen.add(key)
                self.by_group.setdefault(a["group"], []).append((quote_pattern(a["segments"]), a["items"]))
        self.calls = 0

    def generate_json(self, system: str, prompt: str, schema: dict) -> LLMResult:
        self.calls += 1
        group = re.search(r"FIELD GROUP: (\w+)", prompt).group(1)
        dd = re.search(r"DOCUMENT DATE: (\d{4})", prompt)
        pivot = int(dd.group(1)) if dd else 2026
        text = prompt.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0]
        items, used = [], set()
        for rx, ann_items in self.by_group.get(group, []):
            for m in rx.finditer(text):
                if (m.start(), m.end()) in used:
                    continue
                used.add((m.start(), m.end()))
                for ai in ann_items:
                    it = _defaults(group)
                    for k, v in ai.items():
                        if k.endswith("_slot"):
                            if v is None:
                                continue
                            base = k[:-5]
                            iso, est = parse_slot(m.group(v), pivot)
                            it[base] = iso
                            if base in EST_FOR:
                                it[EST_FOR[base]] = est
                        elif v is not None:
                            it[k] = v
                    it["quote"] = m.group(0)
                    it["certainty"] = "high"
                    it["negated"] = False
                    it["hypothetical"] = it.get("event") in ("planned", "considered")
                    if self.mislabel_plans and it.get("event") == "planned":
                        it["event"], it["hypothetical"] = "start", False
                    items.append(it)
        out = {"items": items}
        content = json.dumps(out)
        return LLMResult(content=content, parsed=out, model=self.model, digest=None, options={},
                         prompt_eval_count=estimate_tokens(system + prompt), eval_count=estimate_tokens(content),
                         expected_prompt_tokens=estimate_tokens(system + prompt))
