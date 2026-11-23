"""Pattern layer: generic PHI regexes (config/deid/patterns.yaml) plus contextual rules for clinician
names, relatives, titled names, hospitals/organisations and ages ≥ 90."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from nmosd.config import load_rules
from nmosd.deid.known_identifiers import COMMON_WORDS, CREDENTIALS, TITLES, Hit

NAME = r"[A-Z][a-z'’\-]+(?:-[A-Z][a-z'’]+)?"
SP = r"[ \t]+"
UPPER_NAME = r"[A-Z][A-Za-z'’\-]+"
RELATIONS = (r"mother|father|mom|dad|husband|wife|spouse|partner|son|daughter|sister|brother|aunt|uncle|"
             r"grandmother|grandfather|grandson|granddaughter|niece|nephew|cousin|fianc[eé]e?|boyfriend|girlfriend|"
             r"daughter-in-law|son-in-law|mother-in-law|father-in-law|sister-in-law|brother-in-law|friend|"
             r"caregiver|guardian|stepmother|stepfather")
NOT_NAMES = COMMON_WORDS | {
    "she", "he", "they", "her", "his", "their", "is", "was", "has", "had", "who", "which", "that", "the", "and",
    "reports", "states", "notes", "says", "called", "visited", "helps", "lives", "present", "presents", "at",
    "with", "in", "on", "for", "to", "of", "by", "are", "were", "will", "also", "accompanied",
    "patient", "pt", "neurology", "clinic", "department", "hospital", "center", "medical", "md", "np", "rn",
}

STAFF_CONTEXT_RX = re.compile(
    rf"(?:Electronically signed by|Signed by|Dictated by|Transcribed by|Attending(?: physician)?:?|Resident:|Fellow:|"
    rf"Interpreted by|Read by|Pathologist:|Seen (?:with|by)|Referred by|Referring(?: provider| physician)?:?|"
    rf"PCP:?|cc:|Discussed with|Supervising(?: physician)?:?|Author:|Provider:)\s*"
    rf"(?P<name>(?:{TITLES}[ \t]*)?{UPPER_NAME}(?:{SP}[A-Z]\.?(?=[ \t]))?(?:{SP}{UPPER_NAME})?)"
)
DR_RX = re.compile(rf"\b(?:Dr\.?|Doctor)[ \t]+(?P<name>{NAME}(?:{SP}{NAME})?)")
CRED_RX = re.compile(rf"(?P<name>\b{NAME}(?:{SP}[A-Z]\.)?{SP}{NAME}),?[ \t]+{CREDENTIALS}\b")
TITLED_RX = re.compile(rf"\b(?:Mr|Mrs|Ms|Miss|Mx)\.?[ \t]+(?P<name>{NAME}(?:{SP}{NAME})?)")
RELATIVE_RX = re.compile(
    rf"\b(?i:{RELATIONS})\b,?[ \t]+(?:\(|\")?(?P<name>{NAME}(?:{SP}{NAME})?)")
HOSPITAL_RX = re.compile(
    rf"(?P<name>(?:St\.?{SP}|Saint{SP})?(?:{NAME}{SP}){{1,4}}(?:Hospital|Medical Cent(?:er|re)|Health Cent(?:er|re)|"
    rf"Infirmary|Rehabilitation (?:Hospital|Cent(?:er|re))|Health System|Healthcare|Medical Group))\b")
ORG_RX = re.compile(rf"(?P<name>(?:{NAME}{SP}){{1,4}}(?:Family Practice|Primary Care|Pediatrics|Associates|Clinic|"
                    rf"Urgent Care|Health Partners|Physicians Group))\b")
AGE_RX = re.compile(r"\b(?P<n>9\d|1[0-4]\d)(?=\s*(?:-|\s)?(?:years?|yrs?|y)[\s-]*(?:old|o\.?|/o)?\b|\s*yo\b|\s*y\.o\.)",
                    re.I)
AGE_WORD_RX = re.compile(r"\b(?:age|aged)\s+(?P<n>9\d|1[0-4]\d)\b", re.I)


@dataclass
class CompiledPattern:
    name: str
    category: str
    rx: re.Pattern
    context: re.Pattern | None


@lru_cache(maxsize=4)
def _generic(mrn_lengths: tuple[int, ...]) -> tuple[CompiledPattern, ...]:
    cfg = load_rules("deid/patterns.yaml")
    out = []
    for p in cfg["patterns"]:
        if p["name"] in {"age_over_89"}:
            continue
        rx = p["regex"]
        if p["name"] == "mrn_like":
            lo, hi = min(mrn_lengths), max(mrn_lengths)
            rx = rx.replace(r"\d{7,10}", rf"\d{{{lo},{hi}}}")
        out.append(CompiledPattern(p["name"], p["category"], re.compile(rx),
                                   re.compile(p["requires_context"]) if p.get("requires_context") else None))
    return tuple(out)


def _plausible_name(name: str) -> bool:
    toks = [t.strip(".,").lower() for t in name.split()]
    return bool(toks) and not any(t in NOT_NAMES for t in toks) and all(len(t) > 1 for t in toks)


class PatternLayer:
    def __init__(self, tokenise_hospitals: bool = True, mrn_lengths: tuple[int, ...] = (7, 8, 9, 10)):
        self.tokenise_hospitals = tokenise_hospitals
        self.generic = _generic(tuple(mrn_lengths))

    def find(self, text: str, start: int = 0, end: int | None = None) -> list[Hit]:
        end = len(text) if end is None else end
        hits: list[Hit] = []
        for p in self.generic:
            for m in p.rx.finditer(text, start, end):
                if p.context and not p.context.search(text[max(0, m.start() - 40):m.end() + 10]):
                    continue
                cat = "ADDRESS" if p.name == "street_address" else ("ZIP" if p.name.startswith("zip") else p.category)
                hits.append(Hit(m.start(), m.end(), cat, p.name, "pattern"))
        for rx, cat in ((STAFF_CONTEXT_RX, "STAFF"), (DR_RX, "STAFF"), (CRED_RX, "STAFF")):
            for m in rx.finditer(text, start, end):
                name = m.group("name")
                core = re.sub(rf"^{TITLES}\s*", "", name)
                if _plausible_name(core):
                    s0 = m.start("name") + name.index(core)
                    hits.append(Hit(s0, s0 + len(core), cat, core.split()[-1].lower(), "pattern"))
        for m in TITLED_RX.finditer(text, start, end):
            if _plausible_name(m.group("name")):
                hits.append(Hit(m.start("name"), m.end("name"), "RELATIVE", m.group("name").lower(), "pattern"))
        for m in RELATIVE_RX.finditer(text, start, end):
            name = m.group("name")
            if _plausible_name(name):
                hits.append(Hit(m.start("name"), m.end("name"), "RELATIVE", name.lower(), "pattern"))
        if self.tokenise_hospitals:
            for m in HOSPITAL_RX.finditer(text, start, end):
                hits.append(Hit(m.start("name"), m.end("name"), "HOSPITAL", m.group("name").lower(), "pattern"))
        for m in ORG_RX.finditer(text, start, end):
            hits.append(Hit(m.start("name"), m.end("name"), "ORGANIZATION", m.group("name").lower(), "pattern"))
        for rx in (AGE_RX, AGE_WORD_RX):
            for m in rx.finditer(text, start, end):
                hits.append(Hit(m.start("n"), m.end("n"), "AGE", "age90", "pattern"))
        return hits
