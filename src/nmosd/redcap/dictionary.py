"""Parse a REDCap data dictionary CSV (metadata only, no PHI).

Choices are always keyed by CODE (never label or list position): REDCap codes can be non-numeric
(`type_test`: mog/aqp4), out of order (`adverse_events`: … 6 before 5) or share labels
(`rx_before_nmo`: Methotrexate at 5 and 11).
"""

from __future__ import annotations

import csv
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

COLUMN_ALIASES = {
    "name": ["Variable / Field Name", "field_name"],
    "form": ["Form Name", "form_name"],
    "section": ["Section Header", "section_header"],
    "type": ["Field Type", "field_type"],
    "label": ["Field Label", "field_label"],
    "choices": ["Choices, Calculations, OR Slider Labels", "select_choices_or_calculations"],
    "note": ["Field Note", "field_note"],
    "validation": ["Text Validation Type OR Show Slider Number", "text_validation_type_or_show_slider_number"],
    "vmin": ["Text Validation Min", "text_validation_min"],
    "vmax": ["Text Validation Max", "text_validation_max"],
    "identifier": ["Identifier?", "identifier"],
    "branching": ["Branching Logic (Show field only if...)", "branching_logic"],
    "required": ["Required Field?", "required_field"],
    "annotation": ["Field Annotation", "field_annotation"],
}
# The project's Missing Data Codes (Project Setup). They are not in the dictionary CSV, but REDCap adds a
# `field___unk` / `field___na` column for them to every checkbox field (confirmed by the project's export
# header, 10.11.26), and a non-checkbox field stores the code itself.
MISSING_DATA_CODES = ("UNK", "NA")
REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_DICTIONARY = REPO_ROOT / "config" / "redcap" / "data_dictionary.csv"
FIXTURE_DICTIONARY = REPO_ROOT / "tests" / "fixtures" / "redcap" / "data_dictionary_pilot.csv"
IMPLICIT_CHOICES = {"yesno": OrderedDict([("1", "Yes"), ("0", "No")]),
                    "truefalse": OrderedDict([("1", "True"), ("0", "False")])}
DATE_VALIDATIONS = {"date_mdy", "date_dmy", "date_ymd"}


@dataclass
class Field:
    name: str
    form: str
    ftype: str
    label: str
    choices: OrderedDict = field(default_factory=OrderedDict)
    calculation: str = ""
    note: str = ""
    validation: str = ""
    vmin: str = ""
    vmax: str = ""
    identifier: bool = False
    branching: str = ""
    required: bool = False
    annotation: str = ""
    section: str = ""
    number: int = 0          # the codebook's field number: the row's position in the dictionary (1-based)

    @property
    def piped(self) -> list[str]:
        """Fields embedded in the label with REDCap's {field} syntax (a date's estimate radio)."""
        return re.findall(r"\{([a-z0-9_]+)(?::[a-z]+)?\}", self.label)

    @property
    def clean_label(self) -> str:
        """The label without embedded {field} placeholders."""
        return re.sub(r"\s*\{[a-z0-9_]+(?::[a-z]+)?\}", "", self.label).strip()

    @property
    def is_calc(self) -> bool:
        return self.ftype == "calc" or "@CALCTEXT" in self.annotation or "@CALCDATE" in self.annotation

    @property
    def is_descriptive(self) -> bool:
        return self.ftype == "descriptive"

    @property
    def is_hidden(self) -> bool:
        return "@HIDDEN" in self.annotation

    @property
    def is_readonly(self) -> bool:
        return "@READONLY" in self.annotation

    @property
    def is_checkbox(self) -> bool:
        return self.ftype == "checkbox"

    @property
    def is_coded(self) -> bool:
        return self.ftype in {"radio", "dropdown", "checkbox", "yesno", "truefalse"}

    @property
    def is_date(self) -> bool:
        return self.validation in DATE_VALIDATIONS or self.validation.startswith("datetime")

    @property
    def is_form_status(self) -> bool:
        """`<form>_complete`: the codebook lists it as a field; REDCap's dictionary download leaves it out. Exports
        write it once, after the form's fields."""
        return self.name == f"{self.form}_complete"

    @property
    def is_exportable(self) -> bool:
        """Fields we may ever populate (never calc/descriptive/form status; identifiers only at re-identification)."""
        return not (self.is_calc or self.is_descriptive or self.is_form_status)

    @property
    def none_of_the_above(self) -> str | None:
        m = re.search(r"@?NONEOFTHEABOVE\s*=\s*['\"]?([^'\"\s]+)", self.annotation)
        return m.group(1) if m else None

    def checkbox_columns(self, missing: tuple[str, ...] = ()) -> dict[str, str]:
        """code -> export column name (`field___code`); pass MISSING_DATA_CODES to add `___unk`/`___na`."""
        codes = [*self.choices, *[m for m in missing if m not in self.choices]]
        return {c: f"{self.name}___{re.sub(r'[^a-z0-9_]', '_', c.lower()).replace('-', '_')}" for c in codes}

    def duplicate_labels(self) -> dict[str, list[str]]:
        seen: dict[str, list[str]] = {}
        for c, lab in self.choices.items():
            seen.setdefault(lab.strip().lower(), []).append(c)
        return {lab: codes for lab, codes in seen.items() if len(codes) > 1}


def parse_choices(raw: str) -> OrderedDict:
    out: OrderedDict = OrderedDict()
    if not raw or not raw.strip():
        return out
    for part in raw.split("|"):
        part = part.strip()
        if not part:
            continue
        code, _, label = part.partition(",")
        out[code.strip()] = label.strip()
    return out


@dataclass
class Dictionary:
    fields: OrderedDict
    forms: OrderedDict

    def __getitem__(self, name: str) -> Field:
        return self.fields[name]

    def __contains__(self, name: str) -> bool:
        return name in self.fields

    def get(self, name: str, default: Field | None = None) -> Field | None:
        return self.fields.get(name, default)

    def form_fields(self, form: str) -> list[Field]:
        return [self.fields[n] for n in self.forms.get(form, [])]

    def estimate_for(self, date_field: str) -> str | None:
        """The estimate radio paired with a date field (embedded in its label, e.g. `{date_attack_estimate}`)."""
        if date_field not in self.fields:
            return None
        return next((n for n in self.fields[date_field].piped if n in self.fields
                     and self.fields[n].ftype == "radio" and n != date_field), None)

    def quirks(self) -> dict[str, list[str]]:
        """Report choice lists whose codes are out of order or labels duplicated."""
        q: dict[str, list[str]] = {}
        for f in self.fields.values():
            if not f.choices or f.ftype in IMPLICIT_CHOICES:
                continue
            if f.duplicate_labels():
                q.setdefault(f.name, []).append(f"duplicate labels: {f.duplicate_labels()}")
            codes = list(f.choices)
            if all(c.lstrip("-").isdigit() for c in codes) and [int(c) for c in codes] != sorted(int(c) for c in codes):
                q.setdefault(f.name, []).append(f"codes out of order: {codes}")
        return q


def _col(row: dict, key: str) -> str:
    for alias in COLUMN_ALIASES[key]:
        if alias in row:
            return (row[alias] or "").strip()
    return ""


def load_dictionary(path: Path) -> Dictionary:
    fields: OrderedDict = OrderedDict()
    forms: OrderedDict = OrderedDict()
    number = 0
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = _col(row, "name")
            if not name:
                continue
            number += 1
            ftype = _col(row, "type")
            raw = _col(row, "choices")
            choices = IMPLICIT_CHOICES.get(ftype, OrderedDict()).copy() if ftype in IMPLICIT_CHOICES else (
                parse_choices(raw) if ftype in {"radio", "dropdown", "checkbox"} else OrderedDict())
            fld = Field(name=name, form=_col(row, "form"), ftype=ftype, label=_col(row, "label"), choices=choices,
                        calculation=raw if ftype == "calc" else "", note=_col(row, "note"),
                        validation=_col(row, "validation"), vmin=_col(row, "vmin"), vmax=_col(row, "vmax"),
                        identifier=_col(row, "identifier").lower() == "y", branching=_col(row, "branching"),
                        required=_col(row, "required").lower() == "y", annotation=_col(row, "annotation"),
                        section=_col(row, "section"), number=number)
            fields[name] = fld
            forms.setdefault(fld.form, []).append(name)
    return Dictionary(fields, forms)


# ---- branching logic (subset: [f] = 'x', [f] <> 'x', [f(code)] = '1', numeric <, >, and/or, parentheses) ------
def eval_branching(expr: str, row: dict) -> bool | None:
    """Evaluate REDCap branching logic against `row` ({field: value, field___code: '1'/'0'}). An empty parent
    counts as not matching, like REDCap; None when the syntax is not supported."""
    if not expr.strip():
        return True

    def term(m: re.Match) -> str:
        name, code, op, val = m.group(1), m.group(2), m.group(3), m.group(4)
        actual = row.get(f"{name}___{code}") if code else row.get(name)
        if actual in (None, ""):
            return "None"
        a = str(actual)
        if op in ("=", "=="):
            return str(a == val)
        if op in ("<>", "!="):
            return str(a != val)
        try:
            return str(eval(f"{float(a)}{op}{float(val)}"))
        except ValueError:
            return "None"

    py = re.sub(r"\[([a-z0-9_]+)(?:\(([^)]+)\))?\]\s*(<>|!=|==|=|<=|>=|<|>)\s*['\"]?([^'\"\s)]*)['\"]?", term, expr,
                flags=re.I)
    py = re.sub(r"\band\b", " and ", re.sub(r"\bor\b", " or ", py, flags=re.I), flags=re.I)
    if re.search(r"\[[^\]]+\]", py) or not re.fullmatch(r"[\sA-Za-z()]*", py):
        return None  # unsupported syntax: cannot evaluate
    try:
        val = eval(py.replace("None", "False"), {"__builtins__": {}}, {})
    except SyntaxError:
        return None
    return bool(val)


def branching_row(dic: Dictionary, values: dict) -> dict:
    """{field: value} (checkbox values as JSON lists or lists) → the flat row eval_branching reads."""
    import json

    row: dict = {}
    for name, v in values.items():
        if name in dic and dic[name].is_checkbox:
            try:
                codes = set(json.loads(v)) if isinstance(v, str) else set(v or [])
            except json.JSONDecodeError:
                codes = {v}
            for c in dic[name].choices:
                row[f"{name}___{c}"] = "1" if c in codes else "0"
        elif v not in (None, ""):
            row[name] = str(v)
    return row


def default_dictionary_path() -> Path:
    """config/redcap/data_dictionary.csv (built from the codebook PDF, 16.11.26; or REDCap's own download), else the
    test fixture (the same codebook)."""
    return REAL_DICTIONARY if REAL_DICTIONARY.exists() else FIXTURE_DICTIONARY
