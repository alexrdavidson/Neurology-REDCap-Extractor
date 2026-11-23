"""NER layer: an ensemble of clinical de-identification transformers run locally on CPU.

* `NERModel` is a small protocol, so AnonCAT or an MGB-approved service can be added later.
* `HFTokenClassifier` runs a Hugging Face token-classification model over sliding character windows
  with overlap, tracking offsets back to the full note. Weights must already be in the local cache
  (scripts/download_models.py); at runtime HF_HUB_OFFLINE=1 and local_files_only=True.
* `Ensemble` takes the UNION of the models' spans (recall-biased): a span counts as PHI if either
  model flags it above its own (low) threshold. Labels are mapped to the unified scheme.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Protocol

from nmosd.config import load_rules
from nmosd.safety import enforce_offline_hf

UNIFIED = {"PATIENT", "STAFF", "RELATIVE", "AGE", "DATE", "PHONE", "ID", "EMAIL", "URL", "LOCATION", "HOSPITAL",
           "ORGANIZATION", "OTHER"}


@dataclass
class NERSpan:
    start: int
    end: int
    label: str
    score: float
    models: set[str] = field(default_factory=set)


class NERModel(Protocol):
    name: str

    def predict(self, text: str) -> list[NERSpan]: ...


class NERUnavailable(RuntimeError):
    pass


def map_label(raw: str, mapping: dict[str, str]) -> str:
    lab = re.sub(r"^(?:[BILUES]-)", "", raw.upper())
    return mapping.get(lab, "OTHER" if lab not in ("O", "") else "")


def windows(text: str, size: int, overlap: int) -> list[tuple[int, int]]:
    """Character windows that end on whitespace where possible."""
    out, start, n = [], 0, len(text)
    if n <= size:
        return [(0, n)] if n else []
    while start < n:
        end = min(n, start + size)
        if end < n:
            ws = text.rfind(" ", start + size // 2, end)
            nl = text.rfind("\n", start + size // 2, end)
            end = max(ws, nl, start + size // 2) if max(ws, nl) > 0 else end
        out.append((start, end))
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out


class HFTokenClassifier:
    def __init__(self, name: str, hf_id: str, label_map: dict[str, str], threshold: float = 0.3,
                 revision: str | None = None, fallback_hf_id: str | None = None,
                 fallback_revision: str | None = None, window_chars: int = 1800,
                 overlap_chars: int = 300):
        self.name, self.hf_id, self.revision, self.fallback = name, hf_id, revision, fallback_hf_id
        self.fallback_revision = fallback_revision
        self.label_map, self.threshold = label_map, threshold
        self.window_chars, self.overlap_chars = window_chars, overlap_chars
        self._pipe = None
        self.loaded_id: str | None = None

    def _load(self):
        if self._pipe is not None:
            return
        enforce_offline_hf()
        try:
            from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline
        except ImportError as e:
            raise NERUnavailable("transformers/torch are not installed: pip install -e .[ner]") from e
        last = None
        for mid, rev in [(self.hf_id, self.revision), (self.fallback, self.fallback_revision)]:
            if not mid:
                continue
            try:
                tok = AutoTokenizer.from_pretrained(mid, revision=rev, local_files_only=True)
                mdl = AutoModelForTokenClassification.from_pretrained(mid, revision=rev, local_files_only=True)
                self._pipe = pipeline("token-classification", model=mdl, tokenizer=tok,
                                      aggregation_strategy="simple", device=-1)
                self.loaded_id = mid
                return
            except Exception as e:
                last = e
        raise NERUnavailable(f"Model weights for {self.name} not in the local cache; run scripts/download_models.py "
                             f"({type(last).__name__})")

    def _max_tokens(self) -> int:
        tok, cfg = self._pipe.tokenizer, self._pipe.model.config
        lim = getattr(tok, "model_max_length", 512) or 512
        if lim > 100_000:  # tokenizer sentinel for "unset"
            lim = getattr(cfg, "max_position_embeddings", 512)
        return min(int(lim), 512)

    def token_windows(self, text: str) -> list[tuple[int, int]]:
        """Character windows that each fit the model's token limit, with token overlap.

        Character windows alone are unsafe: number-dense text (tables, dates, BP readings) can exceed 512
        tokens in 1,800 characters. Windows never truncate, so every character reaches the model.
        """
        if not text:
            return []
        every = self._pipe.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)["offset_mapping"]
        # Window edges come from tokens with a real span, but the size counts EVERY token: whitespace tokens
        # (CRLF runs, fixed-width padding) can have zero-width offsets and still take up model positions.
        pos = [k for k, o in enumerate(every) if o[1] > o[0]]
        offs = [every[k] for k in pos]
        if not offs:
            return [(0, len(text))]
        size = self._max_tokens() - 2 - 24          # specials + margin for re-tokenisation at window edges
        size = max(32, min(size, int(self.window_chars / 2)))
        overlap = max(8, min(size // 4, int(self.overlap_chars / 4)))
        out, i = [], 0
        while i < len(offs):
            j = bisect.bisect_right(pos, pos[i] + size - 1, lo=i)   # tokens i..j-1 span at most `size` positions
            start = 0 if i == 0 else offs[i][0]
            end = len(text) if j == len(offs) else offs[j - 1][1]
            out.append((start, end))
            if j == len(offs):
                break
            i = max(i + 1, j - overlap)
        return out

    def predict(self, text: str) -> list[NERSpan]:
        self._load()
        spans: list[NERSpan] = []
        for ws, we in self.token_windows(text):
            for ent in self._pipe(text[ws:we]):
                label = map_label(ent.get("entity_group") or ent.get("entity", ""), self.label_map)
                if not label or float(ent["score"]) < self.threshold:
                    continue
                s, e = ws + int(ent["start"]), ws + int(ent["end"])
                while s < e and text[s].isspace():
                    s += 1
                if e > s:
                    spans.append(NERSpan(s, e, label, float(ent["score"]), {self.name}))
        return merge_spans(spans)


def merge_spans(spans: list[NERSpan], join_gap: int = 1) -> list[NERSpan]:
    """Union overlapping spans, and same-label spans at most `join_gap` characters apart (BILOU fragments of one
    entity, e.g. "Rosalind" + "Quillfeather"); keep the label of the highest-scoring member."""
    spans = sorted(spans, key=lambda s: (s.start, -s.end))
    out: list[NERSpan] = []
    for s in spans:
        if out and (s.start <= out[-1].end or (s.label == out[-1].label and s.start - out[-1].end <= join_gap)):
            cur = out[-1]
            if s.score > cur.score:
                cur.label, cur.score = s.label, s.score
            cur.end = max(cur.end, s.end)
            cur.models |= s.models
        else:
            out.append(NERSpan(s.start, s.end, s.label, s.score, set(s.models)))
    return out


@dataclass
class EnsembleResult:
    spans: list[NERSpan]
    per_model: dict[str, int]
    union: int
    agreement: int    # union spans flagged by every model


class Ensemble:
    def __init__(self, models: list[NERModel]):
        self.models = models

    def predict(self, text: str) -> EnsembleResult:
        all_spans: list[NERSpan] = []
        per_model: dict[str, int] = {}
        for m in self.models:
            sp = m.predict(text)
            per_model[m.name] = len(sp)
            all_spans.extend(sp)
        union = merge_spans(all_spans)
        names = {m.name for m in self.models}
        return EnsembleResult(union, per_model, len(union), sum(1 for s in union if s.models >= names))


def build_ensemble(settings) -> Ensemble:
    maps = load_rules("deid/label_maps.yaml")
    cfg = settings.get("deid.ner") or {}
    models = []
    for m in cfg.get("models", []):
        models.append(HFTokenClassifier(
            name=m["name"], hf_id=m["hf_id"], label_map=maps[m.get("label_map", "obi")],
            threshold=float(m.get("threshold", 0.3)), revision=m.get("revision"),
            fallback_hf_id=m.get("fallback_hf_id"), fallback_revision=m.get("fallback_revision"), window_chars=int(cfg.get("window_chars", 1800)),
            overlap_chars=int(cfg.get("overlap_chars", 300))))
    if not models:
        raise NERUnavailable("deid.ner.models is empty in settings")
    for m in models:
        m._load()  # fail closed before processing any note
    return Ensemble(models)


class DictionaryNER:
    """Deterministic stand-in used in tests (and as an example of the NERModel interface)."""

    def __init__(self, name: str, terms: dict[str, str], score: float = 0.9):
        self.name, self.score = name, score
        self.rx = [(re.compile(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])"), lab) for t, lab in terms.items()]

    def predict(self, text: str) -> list[NERSpan]:
        out = [NERSpan(m.start(), m.end(), lab, self.score, {self.name}) for rx, lab in self.rx for m in rx.finditer(text)]
        return merge_spans(out)
