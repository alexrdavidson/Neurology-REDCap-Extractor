#!/usr/bin/env python3
"""One-time setup: download the de-identification transformer weights into the local Hugging Face cache.

No PHI is involved. Afterwards everything runs offline (HF_HUB_OFFLINE=1). The script prints the
commit hash of each model; paste it into `deid.ner.models[].revision` in config/settings.yaml to pin it.

    pip install -e .[ner]
    python scripts/download_models.py
"""

from __future__ import annotations

import sys

MODELS = [
    ("obi_robust_deid", ["obi/deid_roberta_i2b2", "obi/deid_bert_i2b2"]),
    ("stanford_aimi", ["StanfordAIMI/stanford-deidentifier-v2", "StanfordAIMI/stanford-deidentifier-base"]),
]


def main() -> int:
    try:
        from huggingface_hub import HfApi, snapshot_download
    except ImportError:
        print("Install the NER extras first: pip install -e .[ner]", file=sys.stderr)
        return 2
    api = HfApi()
    for name, candidates in MODELS:
        for repo in candidates:
            try:
                info = api.model_info(repo)
                files = [s.rfilename for s in (info.siblings or [])]
                # one copy of the weights only (many repos publish both .safetensors and .bin)
                weights = ["*.safetensors"] if any(f.endswith(".safetensors") for f in files) else ["pytorch_model*.bin"]
                snapshot_download(repo, revision=info.sha,
                                  allow_patterns=["*.json", "*.txt", "*.model", "vocab*", "merges.txt", *weights])
                lic = next((t.split(":", 1)[1] for t in (info.tags or []) if t.startswith("license:")), "unknown")
                print(f"{name}: {repo} @ {info.sha}  (license: {lic})")
                break
            except Exception as e:
                print(f"{name}: {repo} unavailable ({type(e).__name__}); trying the next candidate", file=sys.stderr)
        else:
            print(f"{name}: no candidate could be downloaded", file=sys.stderr)
            return 1
    print("\nPin these hf_id/revision values in config/settings.yaml (deid.ner.models).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
