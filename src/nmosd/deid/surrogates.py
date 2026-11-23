"""Consistent surrogate tokens per patient. The token ↔ original mapping lives only in the linkage key."""

from __future__ import annotations

import re

TOKENS = {
    "PATIENT": "[PATIENT]",
    "RELATIVE": "[RELATIVE_{n:02d}]",
    "STAFF": "[CLINICIAN_{n:02d}]",
    "HOSPITAL": "[HOSPITAL_{n:02d}]",
    "ORGANIZATION": "[ORGANIZATION_{n:02d}]",
    "TOWN": "[TOWN_{n:02d}]",
    "LOCATION": "[LOCATION_{n:02d}]",
    "ADDRESS": "[ADDRESS]",
    "ZIP": "[ZIP]",
    "ID": "[ID]",
    "PHONE": "[PHONE]",
    "EMAIL": "[EMAIL]",
    "URL": "[URL]",
    "DOB": "[DOB]",
    "AGE": "90+",
    "DATE": "[DATE]",
    "OTHER": "[REDACTED]",
}
NUMBERED = {k for k, v in TOKENS.items() if "{n" in v}
TOKEN_RX = re.compile(r"\[(?:PATIENT|RELATIVE_\d+|CLINICIAN_\d+|HOSPITAL_\d+|ORGANIZATION_\d+|TOWN_\d+|LOCATION_\d+|"
                      r"ADDRESS|ZIP|ID|PHONE|EMAIL|URL|DOB|DATE|REDACTED|HOLIDAY|AGE 90\+)\]")
_TITLE = re.compile(r"^(?:dr|mr|mrs|ms|miss|mx|prof|doctor)\.?\s+", re.I)
_CRED = re.compile(r",?\s*(?:md|m\.d\.|do|np|rn|pa-c|pa|phd|mbbs|fnp|cnp|ibclc|licsw|msw)\.?$", re.I)


def normalise_key(category: str, text: str, key: str | None = None) -> str:
    if key and key not in {"patient", "pattern", "ner"}:
        return key.lower()
    t = re.sub(r"\s+", " ", text).strip().strip(",.;:").lower()
    t = _CRED.sub("", _TITLE.sub("", t)).strip()
    t = re.sub(r"'s$", "", t)
    if category == "STAFF":
        return t.split()[-1] if t else t
    return t


class SurrogateMap:
    def __init__(self, data: dict | None = None):
        self.data: dict[str, dict[str, dict]] = data or {}

    def token(self, category: str, original: str, key: str | None = None) -> str:
        if category not in TOKENS:
            category = "OTHER"
        if category not in NUMBERED:
            return TOKENS[category]
        k = normalise_key(category, original, key)
        bucket = self.data.setdefault(category, {})
        if k not in bucket and category == "RELATIVE":
            # a bare first or last name of a known relative gets that relative's number
            for existing in bucket:
                parts = existing.split()
                if k in parts or (len(k.split()) > 1 and k.split()[0] in parts):
                    k = existing
                    break
        if k not in bucket:
            bucket[k] = {"n": len(bucket) + 1, "originals": []}
        entry = bucket[k]
        if original not in entry["originals"]:
            entry["originals"].append(original)
        return TOKENS[category].format(n=entry["n"])
