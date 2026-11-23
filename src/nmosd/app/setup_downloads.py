"""One-time setup downloads (need internet; no patient data involved).

* `download_ner_models`: the de-identification models, at exactly the versions pinned in the settings, into the
  local Hugging Face cache. Afterwards de-identification runs offline.
* `pull_ollama_model`: a local AI model into Ollama, through the Ollama server on 127.0.0.1.
"""

from __future__ import annotations

import os
import sys

from nmosd.safety import ensure_local_model, ensure_loopback

PATTERNS = ["*.json", "*.txt", "*.model", "vocab*", "merges.txt"]


def _use_system_certificates() -> None:
    """Hospital networks often inspect HTTPS with their own certificate: trust the operating system's store."""
    try:
        import truststore  # type: ignore

        truststore.inject_into_ssl()
    except Exception:
        pass


def download_ner_models(settings, stdout=None) -> int:
    stdout = stdout or sys.stdout
    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        os.environ.pop(k, None)
    _use_system_certificates()
    try:
        from huggingface_hub import HfApi, snapshot_download
    except ImportError:
        print("The de-identification packages are missing. Run the installer again.", file=stdout)
        return 2
    api = HfApi()
    failed = 0
    for m in (settings.get("deid.ner") or {}).get("models", []):
        done = False
        for repo, rev in ((m.get("hf_id"), m.get("revision")), (m.get("fallback_hf_id"), m.get("fallback_revision"))):
            if not repo:
                continue
            try:
                files = [s.rfilename for s in (api.model_info(repo, revision=rev).siblings or [])]
                weights = ["*.safetensors"] if any(f.endswith(".safetensors") for f in files) else ["pytorch_model*.bin"]
                print(f"Downloading {m['name']} ({repo})…", file=stdout, flush=True)
                snapshot_download(repo, revision=rev, allow_patterns=[*PATTERNS, *weights])
                print(f"  {m['name']}: ready ({repo} @ {(rev or 'latest')[:8]})", file=stdout, flush=True)
                done = True
                break
            except Exception as e:
                print(f"  {repo}: {type(e).__name__}", file=stdout, flush=True)
        if not done:
            failed += 1
            print(f"Could not download {m['name']}. Check the internet connection and try again.", file=stdout)
    if failed:
        return 1
    print("De-identification models ready. From now on they work without internet.", file=stdout)
    return 0


def _field(part, key: str):
    return part.get(key) if isinstance(part, dict) else getattr(part, key, None)


def pull_ollama_model(settings, model: str, stdout=None) -> int:
    stdout = stdout or sys.stdout
    ensure_local_model(model, settings.get("llm.allowed_models"))
    host = ensure_loopback(settings.get("llm.host", "http://127.0.0.1:11434"))
    import ollama

    client = ollama.Client(host=host, trust_env=False, follow_redirects=False)
    last, layer = -10, None
    try:
        for part in client.pull(model, stream=True):
            total, done, digest = _field(part, "total"), _field(part, "completed"), _field(part, "digest")
            if digest != layer:                     # each file of the model reports its own progress
                last, layer = -10, digest
            if total and total > 50_000_000 and done is not None:   # report the big files only
                pct = int(100 * done / total)
                if pct >= last + 5 or pct == 100:
                    print(f"Downloading {model}: {pct}%", file=stdout, flush=True)
                    last = pct
    except Exception as e:
        print(f"Could not download {model}: {type(e).__name__}: {str(e)[:200]}. Is Ollama running, and is the "
              "computer online?", file=stdout)
        return 1
    print(f"{model} is ready.", file=stdout)
    return 0
