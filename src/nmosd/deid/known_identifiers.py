"""Known-identifier layer.

Sources per patient (EMPI): the user-confirmed `identifiers.yaml` (PHI location, never printed),
identifiers harvested from note headers, Demographics (DOB, ZIP), and Con/Mrn tables if present.
Global: staff names harvested from headers/sign-offs and listed organisations.

Matching is case- and whitespace-insensitive. Single tokens that are also ordinary English words
(e.g. a relative called "Hope", a town called "Reading") are matched case-sensitively in Title or
UPPER case only. Possessives ("Doe's") are covered because the match ends before the "'s".

`nmosd suggest-identifiers` writes header-harvested candidates to a YAML file for the user to confirm;
it prints counts only.
"""

from __future__ import annotations

import calendar
import contextlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TextIO

import yaml

from nmosd.deid.header import parse_header
from nmosd.safety import ensure_outside_repo, ensure_phi_output

COMMON_WORDS = set(["a", "about", "above", "after", "again", "all", "also", "am", "an", "and", "any", "april", "are", "art", "as", "at", "august", "back", "bad", "be", "bear", "bell", "best", "bill", "black", "bliss", "blue", "bond", "bright", "brook", "brooks", "brown", "bush", "buck", "by", "can", "carter", "case", "chance", "chase", "cherry", "christian", "church", "clay", "cliff", "close", "cook", "cord", "crane", "cross", "dale", "dawn", "day", "dean", "december", "doctor", "dove", "drew", "early", "east", "eve", "faith", "fall", "field", "fields", "fish", "fisher", "ford", "forest", "fox", "frank", "free", "friday", "gay", "gill", "gold", "golden", "grace", "grant", "gray", "green", "grey", "grove", "hale", "hall", "hand", "hardy", "harmony", "hart", "hay", "heart", "hill", "holly", "hope", "house", "hunt", "hunter", "iris", "ivy", "jack", "january", "jay", "jewel", "joy", "july", "june", "just", "king", "knight", "lake", "lamb", "lane", "lee", "light", "lily", "little", "long", "love", "lucky", "major", "march", "mark", "marsh", "mason", "may", "mayo", "means", "mercy", "miles", "mill", "miller", "monday", "moon", "moore", "more", "morning", "moss", "nash", "neat", "new", "newman", "north", "november", "oak", "october", "page", "park", "parker", "patience", "pearl", "penny", "pierce", "pine", "piper", "plain", "pond", "pool", "post", "power", "price", "prince", "rain", "ray", "read", "reading", "reed", "rich", "river", "robin", "rock", "rose", "ross", "ruby", "rush", "sage", "salem", "sandy", "saturday", "september", "shade", "sharp", "shepherd", "short", "silver", "sky", "small", "snow", "south", "spring", "star", "steel", "stone", "storm", "strong", "summer", "sunday", "sunny", "sweet", "swift", "taylor", "temple", "thursday", "tuesday", "turner", "violet", "wade", "walker", "wall", "ward", "warren", "water", "waters", "webb", "wednesday", "well", "wells", "west", "white", "will", "winter", "wise", "wolf", "wood", "woods", "young"])

TITLES = r"(?:Dr|Mr|Mrs|Ms|Miss|Mx|Prof)\.?"
CREDENTIALS = r"(?:MD|M\.D\.|DO|NP|RN|PA-C|PA|PhD|MBBS|FNP|CNP|IBCLC|LICSW|MSW|PharmD|DPT|OTR|CRNA)"
STREET_ABBR = {"street": "st", "road": "rd", "avenue": "ave", "drive": "dr", "lane": "ln", "terrace": "ter",
               "court": "ct", "place": "pl", "boulevard": "blvd", "circle": "cir", "highway": "hwy",
               "parkway": "pkwy", "square": "sq"}
B, E = r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])"


@dataclass
class IdentifierSet:
    first: set[str] = field(default_factory=set)
    last: set[str] = field(default_factory=set)
    middle: set[str] = field(default_factory=set)
    preferred: set[str] = field(default_factory=set)
    mrns: set[str] = field(default_factory=set)
    other_ids: set[str] = field(default_factory=set)
    dob: set[str] = field(default_factory=set)          # ISO dates
    relatives: dict[str, str] = field(default_factory=dict)   # name -> relation
    streets: set[str] = field(default_factory=set)
    towns: set[str] = field(default_factory=set)
    zips: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)

    def merge(self, other: IdentifierSet) -> None:
        for k, v in other.__dict__.items():
            if isinstance(v, (set, dict)):
                getattr(self, k).update(v)

    def to_json(self) -> dict:
        return {k: sorted(v) if isinstance(v, set) else dict(sorted(v.items())) for k, v in self.__dict__.items()}

    @classmethod
    def from_json(cls, d: dict) -> IdentifierSet:
        s = cls()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, dict(v) if isinstance(getattr(s, k), dict) else set(v))
        return s

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.__dict__.items()}


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().strip(",")


def _names_from(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [_clean(value)] if _clean(value) else []
    return [_clean(v) for v in value if v and _clean(v)]


def from_yaml_entry(entry: dict) -> IdentifierSet:
    s = IdentifierSet()
    names = entry.get("names") or {}
    s.first.update(_names_from(names.get("first")))
    s.last.update(_names_from(names.get("last")))
    s.middle.update(n for n in _names_from(names.get("middle")) if len(n.strip(".")) > 1)
    s.preferred.update(_names_from(names.get("preferred")))
    s.preferred.update(_names_from(names.get("nicknames")))
    s.mrns.update(re.sub(r"\D", "", str(m)) for m in entry.get("mrns") or [] if re.sub(r"\D", "", str(m)))
    s.other_ids.update(str(x) for x in entry.get("other_ids") or [])
    if entry.get("dob"):
        s.dob.add(str(entry["dob"]))
    for r in entry.get("relatives") or []:
        if isinstance(r, dict) and r.get("name"):
            s.relatives[_clean(r["name"])] = r.get("relation", "relative")
        elif isinstance(r, str):
            s.relatives[_clean(r)] = "relative"
    addr = entry.get("address") or {}
    s.streets.update(_names_from(addr.get("street")))
    s.towns.update(_names_from(addr.get("town")))
    s.zips.update(str(z) for z in addr.get("zip") or [])
    s.phones.update(str(p) for p in entry.get("phones") or [])
    s.emails.update(str(e) for e in entry.get("emails") or [])
    return s


def from_harvest(h: dict[str, list[str]]) -> IdentifierSet:
    s = IdentifierSet()
    for k in ("lastname",):
        s.last.update(_clean(v).title() if v.isupper() else _clean(v) for v in h.get(k, []))
    for k in ("firstname",):
        s.first.update(_clean(v).title() if v.isupper() else _clean(v) for v in h.get(k, []))
    s.middle.update(m for m in (_clean(v).title() if v.isupper() else _clean(v) for v in h.get("middle", []))
                    if len(m.strip(".")) > 1)
    s.preferred.update(_clean(v) for v in h.get("preferred", []))
    s.mrns.update(re.sub(r"\D", "", v) for v in h.get("mrn", []) if re.sub(r"\D", "", v))
    s.other_ids.update(h.get("account", []) + h.get("accession", []))
    s.towns.update(_clean(v) for v in h.get("town", []))
    s.zips.update(h.get("zip", []))
    for d in h.get("dob", []):
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", d)
        if m:
            with contextlib.suppress(ValueError):
                s.dob.add(date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat())
    return s


def load_identifiers_yaml(path: Path | None) -> tuple[dict[str, IdentifierSet], list[str], list[str]]:
    """Return ({empi: IdentifierSet}, staff names, organisations)."""
    if not path or not Path(path).exists():
        return {}, [], []
    ensure_outside_repo(path, "identifiers.yaml")
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    pts = {str(k): from_yaml_entry(v or {}) for k, v in (data.get("patients") or {}).items()}
    return pts, [_clean(s) for s in data.get("staff") or []], [_clean(o) for o in data.get("organizations") or []]


# ------------------------------------------------------------------------------------------
# variants → regexes
# ------------------------------------------------------------------------------------------
def _ws(tokens: list[str]) -> str:
    return r"[\s,]+".join(re.escape(t) for t in tokens)


def _title_or_upper(token: str) -> str:
    return rf"(?:{re.escape(token)}|{re.escape(token.upper())})"


def _single(token: str) -> tuple[str, bool]:
    """(regex, case_sensitive) for a single-token name/place."""
    t = token.strip(".")
    if t.lower() in COMMON_WORDS or len(t) <= 3:
        return rf"(?:{re.escape(t.title())}|{re.escape(t.upper())})", True
    return re.escape(t), False


def name_variants(first: set[str], last: set[str], middle: set[str], preferred: set[str]) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    firsts = {f for f in first | preferred if f}
    lasts = set()
    for ln in last:
        lasts.add(ln)
        if "-" in ln:
            lasts.update(p for p in ln.split("-") if len(p) > 1)
    for f in firsts:
        for ln in lasts:
            out.append((rf"{re.escape(f)}\s+(?:[A-Z]\.?\s+)?{re.escape(ln)}", False))
            out.append((_ws([ln, f]), False))
            ln_rx, ln_cs = _single(ln)
            out.append((rf"{re.escape(f[0].upper())}\.?[ \t]*{ln_rx if ln_cs else _title_or_upper(ln)}", True))
            for m in middle | {""}:
                if m:
                    out.append((rf"{_ws([f])}\s+{re.escape(m[0])}\.?(?:{re.escape(m[1:])})?\s+{re.escape(ln)}", False))
                    out.append((rf"{_ws([ln])}[\s,]+{re.escape(f)}\s+{re.escape(m[0])}\.?", False))
            out.append((rf"{re.escape(ln)},\s*{re.escape(f)}(?:\s+[A-Z]\.?)?", False))
    for tok in firsts | lasts | {m for m in middle if len(m) > 1}:
        out.append(_single(tok))
    return out


def mrn_regex(mrn: str) -> str:
    digits = re.sub(r"\D", "", mrn).lstrip("0") or "0"
    return r"0*" + r"[-\s.]?".join(digits)


def phone_regex(phone: str) -> str | None:
    d = re.sub(r"\D", "", phone)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10:
        return re.escape(phone) if phone else None
    a, b, c = d[:3], d[3:6], d[6:]
    return rf"(?:\+?1[-.\s]?)?\(?{a}\)?[-.\s]*{b}[-.\s]*{c}"


def dob_regexes(iso: str) -> list[str]:
    y, m, d = (int(x) for x in iso.split("-"))
    yy = f"{y % 100:02d}"
    mon_full, mon_abbr = calendar.month_name[m], calendar.month_abbr[m]
    mon = rf"(?:{mon_full}|{mon_abbr}\.?|Sept\.?)" if m == 9 else rf"(?:{mon_full}|{mon_abbr}\.?)"
    return [
        rf"0?{m}[/\-.]0?{d}[/\-.](?:{y}|{yy})",
        rf"{y}[/\-.]0?{m}[/\-.]0?{d}",
        rf"{mon}\s+0?{d}(?:st|nd|rd|th)?,?\s+(?:{y}|'{yy})",
        rf"0?{d}(?:st|nd|rd|th)?[\s\-]+(?:of\s+)?{mon},?[\s\-]+(?:{y}|{yy})",
        rf"{m:02d}{d:02d}{y}",
    ]


def street_regex(street: str) -> str:
    toks = street.split()
    parts = []
    for t in toks:
        low = t.lower().strip(".")
        if low in STREET_ABBR:
            parts.append(rf"(?:{re.escape(t.strip('.'))}|{STREET_ABBR[low]}\.?)")
        elif low in STREET_ABBR.values():
            full = next(k for k, v in STREET_ABBR.items() if v == low)
            parts.append(rf"(?:{full}|{re.escape(low)}\.?)")
        else:
            parts.append(re.escape(t))
    return r"\s+".join(parts)


def staff_variants(name: str) -> list[tuple[str, bool, str]]:
    """Return (regex, case_sensitive, key) for one clinician."""
    n = re.sub(rf",?\s*{CREDENTIALS}(?:,\s*{CREDENTIALS})*\s*$", "", _clean(name))
    n = re.sub(rf"^{TITLES}\s*", "", n).strip()
    toks = [t for t in re.split(r"\s+", n) if t]
    if not toks:
        return []
    last = toks[-1].strip(".,")
    if len(last) < 2 or not last[0].isupper():
        return []
    key = last.lower()
    last_rx, last_cs = _single(last)
    out = [(rf"{TITLES}\s*{last_rx}", last_cs, key)]
    if len(toks) >= 2:
        first = toks[0]
        out += [(_ws([first, last]), False, key), (_ws(toks), False, key), (rf"{re.escape(last)},\s*{re.escape(first)}", False, key),
                (rf"{re.escape(first[0].upper())}\.?[ \t]*{last_rx if last_cs else _title_or_upper(last)}", True, key)]
    out.append((last_rx, last_cs, key))
    return out


@dataclass
class Rule:
    category: str
    key: str
    rx: re.Pattern
    single_common: bool = False


@dataclass
class Hit:
    start: int
    end: int
    category: str
    key: str
    source: str = "known"
    single_common: bool = False


def _compile(alts: list[tuple[str, bool]], category: str, key: str) -> list[Rule]:
    rules = []
    ci = [a for a, cs in alts if not cs]
    cs = [a for a, c in alts if c]
    if ci:
        ci.sort(key=len, reverse=True)
        rules.append(Rule(category, key, re.compile(rf"{B}(?:{'|'.join(ci)}){E}", re.I)))
    for a in cs:
        rules.append(Rule(category, key, re.compile(rf"{B}{a}{E}"), single_common=True))
    return rules


class KnownMatcher:
    """All identifier rules for one patient plus global staff/organisations."""

    def __init__(self, ids: IdentifierSet, staff: list[str] | None = None, organizations: list[str] | None = None):
        self.rules: list[Rule] = []
        self.rules += _compile(name_variants(ids.first, ids.last, ids.middle, ids.preferred), "PATIENT", "patient")
        for m in ids.mrns:
            self.rules += _compile([(mrn_regex(m), False)], "ID", "mrn")
        for x in ids.other_ids:
            if len(re.sub(r"\W", "", x)) >= 4:
                self.rules += _compile([(re.escape(x), False)], "ID", "other_id")
        for iso in ids.dob:
            self.rules += _compile([(r, False) for r in dob_regexes(iso)], "DOB", "dob")
        for p in ids.phones:
            r = phone_regex(p)
            if r:
                self.rules += _compile([(r, False)], "PHONE", "phone")
        for e in ids.emails:
            self.rules += _compile([(re.escape(e), False)], "EMAIL", "email")
        for s in ids.streets:
            self.rules += _compile([(street_regex(s), False)], "ADDRESS", "street")
            no_number = re.sub(r"^\d+\s+", "", s)
            if no_number != s and len(no_number.split()) >= 2:
                self.rules += _compile([(street_regex(no_number), False)], "ADDRESS", "street")
        for t in ids.towns:
            alt = [(_ws(t.split()), False)] if len(t.split()) > 1 else [_single(t)]
            self.rules += _compile(alt, "TOWN", t.lower())
        for z in ids.zips:
            z5 = re.sub(r"\D", "", z)[:5]
            if len(z5) == 5:
                self.rules += _compile([(rf"{z5}(?:-\d{{4}})?", False)], "ZIP", "zip")
        for name, _rel in ids.relatives.items():
            toks = name.split()
            alts = [(_ws(toks), False)]
            if len(toks) > 1:
                alts.append((rf"{re.escape(toks[-1])},\s*{re.escape(toks[0])}", False))
            for t in toks:
                if t.lower() not in {x.lower() for x in ids.last}:
                    alts.append(_single(t))
            self.rules += _compile(alts, "RELATIVE", name.lower())
        for s in staff or []:
            for rx, cs, key in staff_variants(s):
                self.rules += _compile([(rx, cs)], "STAFF", key)
        for o in organizations or []:
            cat = "HOSPITAL" if re.search(r"hospital|medical cent|infirmary", o, re.I) else "ORGANIZATION"
            self.rules += _compile([(_ws(o.split()), False)], cat, o.lower())

    def find(self, text: str, start: int = 0, end: int | None = None) -> list[Hit]:
        end = len(text) if end is None else end
        hits = []
        for r in self.rules:
            for m in r.rx.finditer(text, start, end):
                if m.end() > m.start():
                    hits.append(Hit(m.start(), m.end(), r.category, r.key, "known", r.single_common))
        return hits


# ------------------------------------------------------------------------------------------
# suggest-identifiers
# ------------------------------------------------------------------------------------------
STAFF_SIGNOFF_RX = re.compile(
    rf"(?:Electronically signed by|Signed by|Dictated by|Attending:|Interpreted by|Pathologist:|cc:)\s*"
    rf"((?:{TITLES}[ \t]*)?[A-Z][A-Za-z'\-]+(?:[ \t]+[A-Z]\.?(?=[ \t]))?(?:[ \t]+[A-Z][A-Za-z'\-]+)?(?:,[ \t]*{CREDENTIALS})?)")


def harvest_from_staging(con) -> tuple[dict[str, IdentifierSet], set[str], dict[str, dict]]:
    """Parse every header; returns per-EMPI identifier sets, staff names, and per-table match stats."""
    per: dict[str, IdentifierSet] = defaultdict(IdentifierSet)
    staff: set[str] = set()
    stats: dict[str, dict] = defaultdict(lambda: {"docs": 0, "matched": 0})
    for r in con.execute("SELECT empi, source_table, text FROM documents"):
        h = parse_header(r["text"], r["source_table"])
        stats[r["source_table"]]["docs"] += 1
        stats[r["source_table"]]["matched"] += int(h.matched)
        per[r["empi"]].merge(from_harvest(h.harvested))
        for p in h.harvested.get("provider", []):
            staff.add(p)
        for m in STAFF_SIGNOFF_RX.finditer(r["text"]):
            staff.add(m.group(1))
    for r in con.execute("SELECT empi, dob FROM demographics"):
        if r["dob"]:
            per[r["empi"]].dob.add(r["dob"][:10])
    for r in con.execute("SELECT empi, raw_json FROM demographics"):
        z = json.loads(r["raw_json"]).get("Zip_code") or ""
        if re.fullmatch(r"\d{5}(-\d{4})?", z.strip()):
            per[r["empi"]].zips.add(z.strip())
    for r in con.execute("SELECT table_name, empi, row_json FROM structured_rows WHERE table_name IN ('Contact','Mrn')"):
        row = json.loads(r["row_json"])
        s = per[r["empi"]]
        if r["table_name"] == "Mrn" and row.get("MRN"):
            s.mrns.add(re.sub(r"\D", "", row["MRN"]))
        if r["table_name"] == "Contact":
            s.last.update(_names_from(row.get("Last_Name")))
            s.first.update(_names_from(row.get("First_Name")))
            s.middle.update(n for n in _names_from(row.get("Middle_Name")) if len(n) > 1)
            s.streets.update(_names_from(row.get("Address1")))
            s.towns.update(_names_from(row.get("City")))
            s.zips.update(_names_from(row.get("Zip")))
            s.phones.update(_names_from(row.get("Home_Phone")))
            s.emails.update(_names_from(row.get("Email_Address")))
    return dict(per), staff, dict(stats)


def suggest_identifiers(staging: Path, out: Path, settings=None, stdout: TextIO | None = None) -> dict:
    from nmosd.store.db import connect

    stdout = stdout or sys.stdout
    out = ensure_phi_output(out, "suggested identifiers")
    con = connect(staging, readonly=True)
    per, staff, stats = harvest_from_staging(con)
    con.close()
    doc = {"patients": {}, "staff": sorted(staff), "organizations": []}
    for empi, s in sorted(per.items()):
        doc["patients"][empi] = {
            "names": {"first": sorted(s.first), "last": sorted(s.last), "middle": sorted(s.middle),
                      "preferred": sorted(s.preferred), "nicknames": []},
            "mrns": sorted(s.mrns), "dob": sorted(s.dob)[0] if s.dob else None,
            "relatives": [], "address": {"street": sorted(s.streets), "town": sorted(s.towns), "zip": sorted(s.zips)},
            "phones": sorted(s.phones), "emails": sorted(s.emails), "other_ids": [],
        }
    header = ("# SUGGESTED identifiers harvested from note headers. PHI: keep in the PHI folder.\n"
              "# Check every entry, add relatives, nicknames, phones, emails and addresses, then save as\n"
              "# identifiers.yaml. Never paste this file anywhere.\n")
    out.write_text(header + yaml.safe_dump(doc, sort_keys=True, allow_unicode=True), encoding="utf-8")
    print(f"Wrote suggestions for {len(per)} patients and {len(staff)} staff names (values not shown).", file=stdout)
    print("Header pattern matches per table (counts only):", file=stdout)
    for t, c in sorted(stats.items()):
        print(f"  {t}: {c['matched']}/{c['docs']}", file=stdout)
    return {"patients": len(per), "staff": len(staff)}
