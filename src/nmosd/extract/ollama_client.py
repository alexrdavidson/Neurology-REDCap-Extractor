"""LLM client interface and the local Ollama implementation.

The Ollama client:
* only talks to a loopback host (127.0.0.1 / localhost) and ignores proxy environment variables;
* refuses model names containing "cloud" and models not in `llm.allowed_models`;
* always sets num_ctx, temperature=0, a fixed seed, and think=False (reasoning models);
* requests structured output with `format=<JSON schema>`;
* detects silent context truncation (Ollama truncates without erroring) by comparing
  `prompt_eval_count` with the expected prompt size and with num_ctx;
* records model name, digest, options, token counts and durations for every call.

A Snowflake Cortex implementation can later satisfy the same `LLMClient` protocol.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from nmosd.safety import ensure_local_model, ensure_loopback


def estimate_tokens(text: str) -> int:
    """Cheap, conservative token estimate (≈4 chars/token for English clinical text)."""
    return max(1, int(len(text) / 3.6))


class PromptTooLong(ValueError):
    pass


def _getter(resp):
    """Field accessor for Ollama responses (pydantic objects or plain dicts)."""
    if isinstance(resp, dict):
        return resp.get
    return lambda k: getattr(resp, k, None)


@dataclass
class LLMResult:
    content: str
    parsed: Any | None
    model: str
    digest: str | None
    options: dict
    prompt_eval_count: int | None = None
    eval_count: int | None = None
    prompt_eval_duration_ns: int | None = None
    eval_duration_ns: int | None = None
    load_duration_ns: int | None = None
    total_duration_ns: int | None = None
    wall_s: float = 0.0
    expected_prompt_tokens: int | None = None
    truncation_suspected: bool = False
    truncation_reason: str | None = None
    error: str | None = None
    attempts: int = 1
    extra: dict = field(default_factory=dict)

    def record(self) -> dict:
        d = asdict(self)
        d.pop("content", None)
        d.pop("parsed", None)
        return d


class LLMClient(Protocol):
    name: str
    model: str
    num_ctx: int

    def generate_json(self, system: str, prompt: str, schema: dict) -> LLMResult: ...


class OllamaClient:
    name = "ollama"

    def __init__(self, host: str = "http://127.0.0.1:11434", model: str = "qwen3:8b",
                 allowed_models: list[str] | None = None, num_ctx: int = 8192, num_predict: int = 1024,
                 temperature: float = 0.0, seed: int = 42, timeout_s: float = 900, retries: int = 2,
                 truncation_ratio: float = 0.5, keep_alive: str = "30m", backoff_s: float = 1.0,
                 _client: Any = None):
        self.host = ensure_loopback(host)
        self.model = ensure_local_model(model, allowed_models)
        self.num_ctx, self.num_predict = int(num_ctx), int(num_predict)
        self.temperature, self.seed = float(temperature), int(seed)
        self.timeout_s, self.retries = timeout_s, int(retries)
        self.truncation_ratio = float(truncation_ratio)
        self.keep_alive = keep_alive
        self.backoff_s = backoff_s
        self._digest: str | None = None
        self._injected = _client is not None
        if _client is not None:
            self.client = _client
        else:
            import ollama

            # trust_env=False: never route loopback traffic through a proxy from the environment.
            self.client = ollama.Client(host=self.host, timeout=timeout_s, trust_env=False, follow_redirects=False)

    def clone(self) -> OllamaClient:
        """A copy with a connection of its own, for one of several questions asked at once (`llm.parallel`).
        A test's injected client is shared."""
        c = copy.copy(self)
        if not self._injected:
            import ollama

            c.client = ollama.Client(host=self.host, timeout=self.timeout_s, trust_env=False, follow_redirects=False)
        return c

    @classmethod
    def from_settings(cls, settings, model: str | None = None, num_ctx: int | None = None, **kw) -> OllamaClient:
        g = settings.get
        return cls(host=g("llm.host"), model=model or g("llm.model"), allowed_models=g("llm.allowed_models"),
                   num_ctx=num_ctx or g("llm.num_ctx"), num_predict=g("llm.num_predict"),
                   temperature=g("llm.temperature"), seed=g("llm.seed"), timeout_s=g("llm.timeout_s"),
                   retries=g("llm.retries"), truncation_ratio=g("llm.truncation_ratio"), **kw)

    @property
    def options(self) -> dict:
        return {"num_ctx": self.num_ctx, "num_predict": self.num_predict, "temperature": self.temperature,
                "seed": self.seed, "top_k": 1, "top_p": 1.0}

    def installed_models(self) -> dict[str, str]:
        """{model name: digest} for locally pulled models."""
        resp = self.client.list()
        models = getattr(resp, "models", None) or resp.get("models", [])
        out = {}
        for m in models:
            name = getattr(m, "model", None) or m.get("model") or m.get("name")
            out[name] = getattr(m, "digest", None) or m.get("digest")
        return out

    def digest(self) -> str | None:
        if self._digest is None:
            try:
                self._digest = self.installed_models().get(self.model)
            except Exception:
                self._digest = None
        return self._digest

    def _check_truncation(self, res: LLMResult) -> None:
        pec = res.prompt_eval_count
        if pec is None:
            return
        if pec >= self.num_ctx - 8:
            res.truncation_suspected, res.truncation_reason = True, "prompt_eval_count reached num_ctx"
        elif (res.expected_prompt_tokens or 0) >= 256 and pec < self.truncation_ratio * res.expected_prompt_tokens:
            # Can also be KV-cache reuse of an identical prefix; recorded, and callers re-run uncached.
            res.truncation_suspected, res.truncation_reason = True, "prompt_eval_count far below expected"

    def generate_json(self, system: str, prompt: str, schema: dict) -> LLMResult:
        expected = estimate_tokens(system) + estimate_tokens(prompt) + 32
        if expected > self.num_ctx - min(self.num_predict, self.num_ctx // 4):
            raise PromptTooLong(f"~{expected} prompt tokens do not fit num_ctx={self.num_ctx}; chunk smaller")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        last_err = None
        for attempt in range(1, self.retries + 2):
            t0 = time.perf_counter()
            try:
                kwargs = dict(model=self.model, messages=messages, format=schema, options=self.options,
                              stream=False, keep_alive=self.keep_alive)
                try:
                    resp = self.client.chat(**kwargs, think=False)
                except TypeError:
                    resp = self.client.chat(**kwargs)
                except Exception as e:  # some non-reasoning models reject the think flag
                    if "think" in str(e).lower():
                        resp = self.client.chat(**kwargs)
                    else:
                        raise
                wall = time.perf_counter() - t0
                g = _getter(resp)
                msg = g("message")
                content = (getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")) or ""
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    parsed = None
                res = LLMResult(content=content, parsed=parsed, model=self.model, digest=self.digest(),
                                options=self.options, prompt_eval_count=g("prompt_eval_count"),
                                eval_count=g("eval_count"), prompt_eval_duration_ns=g("prompt_eval_duration"),
                                eval_duration_ns=g("eval_duration"), load_duration_ns=g("load_duration"),
                                total_duration_ns=g("total_duration"), wall_s=wall,
                                expected_prompt_tokens=expected, attempts=attempt)
                self._check_truncation(res)
                # An answer that ran into num_predict comes back the same when asked again (temperature 0, fixed
                # seed): return it at once and let the caller split the excerpt (17.11.26: each cut-off used to
                # cost three identical full-length calls).
                cut = g("done_reason") == "length" or (res.eval_count or 0) >= self.num_predict - 4
                res.extra["cut_off"] = cut
                if parsed is None and attempt <= self.retries and not cut:
                    last_err = "invalid JSON"
                    continue
                return res
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:200]}"
                time.sleep(min(self.backoff_s * 2 ** attempt, 10))
        return LLMResult(content="", parsed=None, model=self.model, digest=self._digest, options=self.options,
                         expected_prompt_tokens=expected, error=last_err, attempts=self.retries + 1)
